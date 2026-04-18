from __future__ import annotations

from wuying.application.device_pool import DeviceTarget
from wuying.application.platform_registry import build_workflow
from wuying.application.worker_manager import WorkerManager
from wuying.config import AppSettings
from wuying.models import PlatformRunResult


def pick_default_instance(settings: AppSettings) -> str:
    if not settings.instance_ids:
        raise ValueError("No instance configured. Set WUYING_INSTANCE_IDS or pass --instance-id.")
    return settings.instance_ids[0]


def run_platform_once(
    *,
    settings: AppSettings,
    platform_name: str,
    prompt: str,
    instance_id: str | None = None,
    device: DeviceTarget | None = None,
    save_result: bool = True,
) -> PlatformRunResult:
    workflow = build_workflow(settings, platform_name)
    resolved_instance_id = device.instance_id if device is not None else (instance_id or pick_default_instance(settings))
    adb_endpoint = device.adb_endpoint if device is not None else None
    device_id = device.device_id if device is not None else None
    return workflow.run_once(
        instance_id=resolved_instance_id,
        prompt=prompt,
        device_id=device_id,
        adb_endpoint=adb_endpoint,
        save_result=save_result,
    )


def run_platform_once_with_timeout(
    *,
    settings: AppSettings,
    platform_name: str,
    prompt: str,
    device: DeviceTarget,
    timeout_seconds: int,
    save_result: bool = False,
) -> PlatformRunResult:
    manager = WorkerManager(settings)
    try:
        manager.start_all([device])
        result = manager.run_on_device(
            device=device,
            platform=platform_name,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            save_result=save_result,
        )
        return _dict_to_result(result)
    finally:
        manager.stop_all()


def _dict_to_result(data: dict[str, object]) -> PlatformRunResult:
    return PlatformRunResult.build(
        platform=str(data["platform"]),
        instance_id=str(data["instance_id"]),
        device_id=data.get("device_id"),
        prompt=str(data["prompt"]),
        response=str(data["response"]),
        adb_serial=str(data["adb_serial"]),
        output_path=str(data.get("output_path") or ""),
        started_at=_parse_dt(str(data["started_at"])),
        finished_at=_parse_dt(str(data["finished_at"])),
        extra={
            "references": dict(data.get("references") or {}),
            **dict(data.get("platform_extra") or {}),
        },
    )


def _parse_dt(raw: str):
    from datetime import datetime

    return datetime.fromisoformat(raw)


__all__ = ["pick_default_instance", "run_platform_once", "run_platform_once_with_timeout"]
