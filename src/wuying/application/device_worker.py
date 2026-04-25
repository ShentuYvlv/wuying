from __future__ import annotations

import multiprocessing as mp
import traceback
from datetime import UTC, datetime
from typing import Any

from wuying.application.device_pool import DeviceTarget
from wuying.application.device_session import DeviceSession
from wuying.application.platform_registry import build_workflow
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging


def device_worker_main(
    command_queue: mp.Queue,
    result_queue: mp.Queue,
    settings: AppSettings,
    device: DeviceTarget,
) -> None:
    configure_logging()
    try:
        session = DeviceSession(
            settings=settings,
            instance_id=device.instance_id,
            device_id=device.device_id,
            adb_endpoint=device.adb_endpoint,
        )
        result_queue.put(
            {
                "type": "worker_ready",
                "device_id": device.device_id,
                "started_at": _utc_now(),
                "connection_state": "not_connected",
                "driver_ready": False,
            }
        )
    except Exception:
        result_queue.put(
            {
                "type": "worker_failed",
                "device_id": device.device_id,
                "error": traceback.format_exc(),
                "finished_at": _utc_now(),
            }
        )
        return

    while True:
        message = command_queue.get()
        if not isinstance(message, dict):
            continue

        command_type = str(message.get("type") or "")
        if command_type == "shutdown":
            break

        if command_type != "run":
            continue

        request_id = str(message["request_id"])
        platform = str(message["platform"])
        prompt = str(message["prompt"])
        save_result = bool(message.get("save_result", False))

        try:
            session.ensure_driver()
            workflow = build_workflow(settings, platform)
            result = workflow.run_once_with_session(
                session=session,
                prompt=prompt,
                save_result=save_result,
            ).to_dict()
            result_queue.put(
                {
                    "type": "task_result",
                    "device_id": device.device_id,
                    "request_id": request_id,
                    "status": "succeeded",
                    "result": result,
                    "connection_state": "driver_ready",
                    "driver_ready": True,
                    "finished_at": _utc_now(),
                }
            )
        except Exception:
            error = traceback.format_exc()
            try:
                session.reset_driver()
            except Exception:
                error = f"{error}\n[reset_driver_failed]\n{traceback.format_exc()}"
            result_queue.put(
                {
                    "type": "task_result",
                    "device_id": device.device_id,
                    "request_id": request_id,
                    "status": "failed",
                    "error": error,
                    "connection_state": "failed",
                    "driver_ready": False,
                    "finished_at": _utc_now(),
                }
            )

    session.close()


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


__all__ = ["device_worker_main"]
