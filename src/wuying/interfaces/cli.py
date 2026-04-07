from __future__ import annotations

import argparse
import json
import sys

from wuying.application.platform_registry import PLATFORM_REGISTRY, available_platform_names
from wuying.application.runner import pick_default_instance, run_platform_once
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one prompt on a selected mobile platform workflow.")
    parser.add_argument("--platform", required=True, choices=available_platform_names(), help="Platform name")
    parser.add_argument("--instance-id", help="Override one instance ID from .env")
    parser.add_argument("--prompt", required=True, help="Prompt sent to the selected app")
    return parser


def run_from_cli(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging()
    settings = AppSettings.from_env()
    instance_id = args.instance_id or pick_default_instance(settings)
    print(
        json.dumps(
            {
                "platform": args.platform,
                "description": PLATFORM_REGISTRY[args.platform].description,
                "instance_id": instance_id,
                "manual_adb_endpoint": settings.device.manual_adb_endpoint,
                "adb_path": settings.device.adb_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    result = run_platform_once(
        settings=settings,
        platform_name=args.platform,
        prompt=args.prompt,
        instance_id=instance_id,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0
