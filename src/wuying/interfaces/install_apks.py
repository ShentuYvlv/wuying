from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from wuying.application.batch_models import BatchTaskRequest
from wuying.application.batch_runner import resolve_batch_devices
from wuying.application.device_lease import DeviceLeaseError, DeviceLeaseManager
from wuying.application.device_pool import DeviceTarget
from wuying.config import AppSettings
from wuying.device.adb import AdbClient
from wuying.invokers.aliyun import WuyingApiClient
from wuying.logging_utils import configure_logging
from wuying.models import AdbEndpoint

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install APK files to selected Wuying devices.")
    parser.add_argument("--devices", help="Comma-separated device IDs from config/device_pool.json")
    parser.add_argument("--apk-dir", default="apk", help="Directory that contains APK files. Default: apk")
    parser.add_argument(
        "--apk",
        help="Optional comma-separated APK file names or paths. Default installs every .apk under --apk-dir",
    )
    parser.add_argument(
        "--grant-permissions",
        action="store_true",
        help="Pass adb install -g",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Per-APK install timeout in seconds. Default: 900",
    )
    return parser


def run_install_from_cli(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        selected_device_ids = _parse_devices(args.devices)
        apk_paths = _resolve_apk_paths(apk_dir=Path(args.apk_dir), raw_apk=args.apk)
    except Exception as exc:
        parser.error(str(exc))

    configure_logging()
    settings = AppSettings.from_env()
    batch_request = BatchTaskRequest(
        platforms=[],
        prompts=[],
        repeat=1,
        save_name=None,
        env={},
        device_ids=selected_device_ids,
        legacy_instance_id=None,
        default_to_all_pool_devices=True,
    )
    devices = resolve_batch_devices(settings, batch_request)
    lease_manager = DeviceLeaseManager(
        settings.device.device_lease_dir,
        stale_after_seconds=settings.device.device_lease_ttl_seconds,
    )
    lease_owner = f"apk-install-{os.getpid()}"
    try:
        lease_manager.acquire_many([device.device_id for device in devices], owner=lease_owner)
    except DeviceLeaseError as exc:
        parser.error(str(exc))

    print(
        json.dumps(
            {
                "device_ids": [device.device_id for device in devices],
                "selected_devices": [device.to_dict() for device in devices],
                "apk_files": [str(path) for path in apk_paths],
                "adb_path": settings.device.adb_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    try:
        results = _install_to_devices(
            settings=settings,
            devices=devices,
            apk_paths=apk_paths,
            grant_permissions=args.grant_permissions,
            timeout_seconds=args.timeout_seconds,
        )
    finally:
        try:
            lease_manager.release_many([device.device_id for device in devices], owner=lease_owner)
        except Exception as exc:
            logger.warning("Failed to release APK install device leases: owner=%s error=%s", lease_owner, exc)

    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "succeeded" for item in results) else 1


def _install_to_devices(
    *,
    settings: AppSettings,
    devices: list[DeviceTarget],
    apk_paths: list[Path],
    grant_permissions: bool,
    timeout_seconds: int,
) -> list[dict[str, object]]:
    adb = AdbClient(settings.device)
    api = WuyingApiClient(settings.aliyun)
    adb.ensure_server()
    max_workers = max(1, min(len(devices), settings.batch_max_workers))
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="apk-install") as executor:
        future_map = {
            executor.submit(
                _install_to_one_device,
                adb=adb,
                api=api,
                device=device,
                apk_paths=apk_paths,
                grant_permissions=grant_permissions,
                timeout_seconds=timeout_seconds,
            ): device
            for device in devices
        }
        for future in as_completed(future_map):
            results.append(future.result())
    results.sort(key=lambda item: str(item["device_id"]))
    return results


def _install_to_one_device(
    *,
    adb: AdbClient,
    api: WuyingApiClient,
    device: DeviceTarget,
    apk_paths: list[Path],
    grant_permissions: bool,
    timeout_seconds: int,
) -> dict[str, object]:
    installs: list[dict[str, object]] = []
    status = "succeeded"
    error: str | None = None
    try:
        endpoint = _resolve_endpoint(
            api=api,
            device=device,
            adb_ready_timeout_seconds=adb.settings.adb_ready_timeout_seconds,
        )
        serial = adb.connect(endpoint)
        adb.wait_for_device(serial, timeout_seconds=adb.settings.adb_ready_timeout_seconds)

        for apk_path in apk_paths:
            try:
                output = adb.install_apk(
                    serial,
                    apk_path,
                    grant_permissions=grant_permissions,
                    timeout_seconds=timeout_seconds,
                )
                installs.append(
                    {
                        "apk": str(apk_path),
                        "status": "succeeded",
                        "output": output,
                    }
                )
            except Exception as exc:
                status = "failed"
                error = str(exc)
                installs.append(
                    {
                        "apk": str(apk_path),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                break
    except Exception as exc:
        status = "failed"
        error = str(exc)

    return {
        "device_id": device.device_id,
        "instance_id": device.instance_id,
        "adb_endpoint": device.adb_endpoint,
        "status": status,
        "error": error,
        "installs": installs,
    }


def _resolve_endpoint(
    *,
    api: WuyingApiClient,
    device: DeviceTarget,
    adb_ready_timeout_seconds: int,
) -> AdbEndpoint:
    if device.adb_endpoint:
        host, port_text = device.adb_endpoint.rsplit(":", 1)
        return AdbEndpoint(
            instance_id=device.instance_id,
            host=host.strip(),
            port=int(port_text),
            source="manual",
        )
    return api.ensure_adb_ready(device.instance_id, timeout_seconds=adb_ready_timeout_seconds)


def _resolve_apk_paths(*, apk_dir: Path, raw_apk: str | None) -> list[Path]:
    if raw_apk:
        resolved: list[Path] = []
        for item in [part.strip() for part in raw_apk.split(",") if part.strip()]:
            candidate = Path(item)
            if not candidate.exists() and not candidate.is_absolute():
                candidate = apk_dir / candidate
            if not candidate.exists():
                raise ValueError(f"APK not found: {candidate}")
            if candidate.is_dir():
                dir_apks = sorted(path.resolve() for path in candidate.glob("*.apk") if path.is_file())
                if not dir_apks:
                    raise ValueError(f"No APK files found in directory: {candidate}")
                resolved.extend(dir_apks)
                continue
            if candidate.suffix.lower() != ".apk":
                raise ValueError(f"Not an APK file: {candidate}")
            resolved.append(candidate.resolve())
        if not resolved:
            raise ValueError("No APK files configured.")
        return sorted({path.resolve() for path in resolved})

    if not apk_dir.exists():
        raise ValueError(f"APK directory does not exist: {apk_dir}")
    apk_files = sorted(path.resolve() for path in apk_dir.glob("*.apk") if path.is_file())
    if not apk_files:
        raise ValueError(f"No APK files found in directory: {apk_dir}")
    return apk_files


def _parse_devices(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    device_ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not device_ids:
        raise ValueError("No devices configured.")
    return device_ids


__all__ = ["build_parser", "run_install_from_cli"]
