from __future__ import annotations

from typing import Callable

from wuying.application.batch_models import BatchTaskRequest
from wuying.application.device_pool import DeviceTarget, resolve_execution_devices
from wuying.application.task_scheduler import run_batch_job_with_workers
from wuying.application.worker_manager import WorkerManager
from wuying.config import AppSettings

ProgressCallback = Callable[[dict[str, object]], None]
CancellationChecker = Callable[[], bool]


def resolve_batch_devices(
    settings: AppSettings,
    request: BatchTaskRequest,
) -> list[DeviceTarget]:
    return resolve_execution_devices(
        settings,
        requested_device_ids=request.device_ids,
        legacy_instance_id=request.legacy_instance_id,
        default_to_all_pool_devices=request.default_to_all_pool_devices,
    )


def run_batch_job(
    *,
    settings: AppSettings,
    task_id: str,
    request: BatchTaskRequest,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
    batch_timeout_seconds: int | None,
    progress_callback: ProgressCallback | None = None,
    cancellation_checker: CancellationChecker | None = None,
    worker_manager: WorkerManager | None = None,
) -> dict[str, object]:
    owns_manager = worker_manager is None
    manager = worker_manager or WorkerManager(settings)
    try:
        manager.start_all(devices, strict=False)
        return run_batch_job_with_workers(
            settings=settings,
            worker_manager=manager,
            task_id=task_id,
            request=request,
            devices=devices,
            record_timeout_seconds=record_timeout_seconds,
            batch_timeout_seconds=batch_timeout_seconds,
            progress_callback=progress_callback,
            cancellation_checker=cancellation_checker,
        )
    finally:
        if owns_manager:
            manager.stop_all()


__all__ = ["resolve_batch_devices", "run_batch_job"]
