from __future__ import annotations

import json
import logging
import multiprocessing as mp
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from wuying.application.device_pool import DeviceTarget
from wuying.application.platform_registry import build_workflow
from wuying.config import AppSettings
from wuying.models import PlatformRunResult, ReferenceData

logger = logging.getLogger(__name__)


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
    )


def run_platform_once_with_timeout(
    *,
    settings: AppSettings,
    platform_name: str,
    prompt: str,
    device: DeviceTarget,
    timeout_seconds: int,
) -> PlatformRunResult:
    context = mp.get_context("spawn")
    result_path = _ipc_result_path(platform_name)
    process = context.Process(
        target=_run_platform_once_child,
        args=(str(result_path), platform_name, prompt, device.instance_id, device.device_id, device.adb_endpoint),
        name=f"wuying-{platform_name}-{device.device_id}",
    )
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        try:
            result_path.unlink()
        except OSError:
            pass
        raise TimeoutError(
            f"Crawler record timed out after {timeout_seconds}s: "
            f"platform={platform_name}, device_id={device.device_id}, instance_id={device.instance_id}"
        )

    if not result_path.exists():
        raise RuntimeError(
            f"Crawler record exited without result: platform={platform_name}, "
            f"device_id={device.device_id}, exitcode={process.exitcode}"
        )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    try:
        result_path.unlink()
    except OSError:
        pass
    if payload.get("ok"):
        return _DictBackedResult(payload["result"]).to_result()

    raise RuntimeError(str(payload.get("error") or "Crawler record failed in child process"))


def _run_platform_once_child(
    result_path: str,
    platform_name: str,
    prompt: str,
    instance_id: str,
    device_id: str,
    adb_endpoint: str | None,
) -> None:
    try:
        from wuying.logging_utils import configure_logging

        configure_logging()
        settings = AppSettings.from_env()
        result = run_platform_once(
            settings=settings,
            platform_name=platform_name,
            prompt=prompt,
            device=DeviceTarget(
                device_id=device_id,
                instance_id=instance_id,
                adb_endpoint=adb_endpoint,
                enabled=True,
            ),
        ).to_dict()
        _write_child_result(result_path, {"ok": True, "result": result})
    except Exception:
        _write_child_result(result_path, {"ok": False, "error": traceback.format_exc()})


def _ipc_result_path(platform_name: str) -> Path:
    root_dir = Path("data/task_ipc")
    root_dir.mkdir(parents=True, exist_ok=True)
    return root_dir / f"{platform_name}_{uuid4().hex}.json"


def _write_child_result(result_path: str, payload: dict[str, Any]) -> None:
    Path(result_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _DictBackedResult:
    data: dict[str, Any]

    def to_result(self) -> PlatformRunResult:
        references_raw = self.data.get("references") or {}
        return PlatformRunResult(
            platform=str(self.data["platform"]),
            instance_id=str(self.data["instance_id"]),
            device_id=self.data.get("device_id"),
            prompt=str(self.data["prompt"]),
            response=str(self.data["response"]),
            adb_serial=str(self.data["adb_serial"]),
            output_path=str(self.data["output_path"]),
            started_at=str(self.data["started_at"]),
            finished_at=str(self.data["finished_at"]),
            references=ReferenceData(
                summary=references_raw.get("summary"),
                keywords=list(references_raw.get("keywords") or []),
                items=PlatformRunResult._normalize_extra({"references": references_raw})[0].items,
            ),
            platform_extra=dict(self.data.get("platform_extra") or {}),
        )


__all__ = ["pick_default_instance", "run_platform_once", "run_platform_once_with_timeout"]
