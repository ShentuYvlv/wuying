from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wuying.application.batch_models import BatchTaskRequest
from wuying.application.batch_runner import resolve_batch_devices, run_batch_job
from wuying.application.device_lease import DeviceLeaseError, DeviceLeaseManager
from wuying.application.platform_registry import PLATFORM_REGISTRY, available_platform_names, get_platform_definition
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run prompts on selected mobile platform workflows.")
    parser.add_argument(
        "-platform",
        "--platform",
        required=True,
        help=f"Platform name, or comma-separated names. Available: {', '.join(available_platform_names())}",
    )
    parser.add_argument("-instance-id", "--instance-id", help="Override one instance ID from .env")
    parser.add_argument("-devices", "--devices", help="Comma-separated device IDs from config/device_pool.json")
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("-prompt", "--prompt", help="Prompt sent to the selected app")
    prompt_source.add_argument("-file", "--file", help="UTF-8 text file. One non-empty line is one prompt.")
    return parser


def _parse_platforms(raw: str) -> list[str]:
    platforms = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not platforms:
        raise ValueError("No platform configured.")

    for platform in platforms:
        get_platform_definition(platform)
    return platforms


def _load_prompts(args: argparse.Namespace) -> list[str]:
    if args.prompt is not None:
        prompt = args.prompt.strip()
        if not prompt:
            raise ValueError("--prompt cannot be empty.")
        return [prompt]

    prompt_file = Path(args.file)
    prompts = [
        line.strip()
        for line in prompt_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not prompts:
        raise ValueError(f"No prompt found in file: {prompt_file}")
    return prompts


def _parse_devices(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    devices = [item.strip() for item in raw.split(",") if item.strip()]
    if not devices:
        raise ValueError("No devices configured.")
    return devices


def _build_cli_task_id() -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    return f"cli-{stamp}-{uuid4().hex[:8]}"


def _get_record_timeout_seconds() -> int:
    raw = os.getenv("CRAWLER_RECORD_TIMEOUT_SECONDS", "300").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"CRAWLER_RECORD_TIMEOUT_SECONDS must be an integer, got {raw!r}") from exc


def run_from_cli(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        platforms = _parse_platforms(args.platform)
        prompts = _load_prompts(args)
        device_ids = _parse_devices(args.devices)
    except Exception as exc:
        parser.error(str(exc))

    configure_logging()
    settings = AppSettings.from_env()
    batch_request = BatchTaskRequest(
        platforms=platforms,
        prompts=prompts,
        repeat=1,
        save_name=None,
        env={},
        device_ids=device_ids,
        legacy_instance_id=args.instance_id,
        default_to_all_pool_devices=True,
    )
    devices = resolve_batch_devices(settings, batch_request)
    task_id = _build_cli_task_id()
    lease_manager = DeviceLeaseManager(
        settings.device.device_lease_dir,
        stale_after_seconds=settings.device.device_lease_ttl_seconds,
    )
    try:
        lease_manager.acquire_many([device.device_id for device in devices], owner=task_id)
    except DeviceLeaseError as exc:
        parser.error(str(exc))

    print(
        json.dumps(
            {
                "platforms": platforms,
                "descriptions": {
                    platform: PLATFORM_REGISTRY[platform].description
                    for platform in platforms
                },
                "instance_id": args.instance_id,
                "device_ids": [device.device_id for device in devices],
                "selected_devices": [device.to_dict() for device in devices],
                "manual_adb_endpoint": settings.device.manual_adb_endpoint,
                "adb_path": settings.device.adb_path,
                "prompt_count": len(prompts),
                "task_id": task_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    try:
        result = run_batch_job(
            settings=settings,
            task_id=task_id,
            request=batch_request,
            devices=devices,
            record_timeout_seconds=_get_record_timeout_seconds(),
            batch_timeout_seconds=settings.batch_timeout_seconds,
        )
    finally:
        try:
            lease_manager.release_many([device.device_id for device in devices], owner=task_id)
        except Exception as exc:
            logger.warning("Failed to release CLI device leases: owner=%s error=%s", task_id, exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
