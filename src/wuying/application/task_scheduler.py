from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from wuying.application.batch_models import BatchTaskRequest, DeviceRunRecord
from wuying.application.device_pool import DeviceTarget
from wuying.application.worker_manager import WorkerManager
from wuying.config import AppSettings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, object]], None]
CancellationChecker = Callable[[], bool]


class BatchDeadline:
    def __init__(self, timeout_seconds: int | None) -> None:
        self.timeout_seconds = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        self.expires_at = (
            datetime.now(tz=UTC).timestamp() + self.timeout_seconds
            if self.timeout_seconds is not None
            else None
        )

    def expired(self) -> bool:
        return self.remaining_seconds() == 0

    def remaining_seconds(self) -> int | None:
        if self.expires_at is None:
            return None
        remaining = self.expires_at - datetime.now(tz=UTC).timestamp()
        if remaining <= 0:
            return 0
        return max(1, int(remaining))

    def record_timeout(self, configured_timeout_seconds: int) -> int:
        remaining = self.remaining_seconds()
        if remaining is None:
            return max(1, configured_timeout_seconds)
        return max(0, min(configured_timeout_seconds, remaining))

    def message(self) -> str:
        return f"Batch task timed out after {self.timeout_seconds}s"


def run_batch_job_with_workers(
    *,
    settings: AppSettings,
    worker_manager: WorkerManager,
    task_id: str,
    request: BatchTaskRequest,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
    batch_timeout_seconds: int | None,
    progress_callback: ProgressCallback | None = None,
    cancellation_checker: CancellationChecker | None = None,
) -> dict[str, object]:
    started_at = _utc_now()
    deadline = BatchDeadline(batch_timeout_seconds)

    task_root_dir = _task_root_dir(settings, task_id)
    raw_dir = task_root_dir / "raw"
    prompt_dir = task_root_dir / "prompts"
    file_stamp = datetime.now().strftime("%Y-%m-%d-%H")
    task_root_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    worker_manager.start_all(devices)
    metrics_runtime = _create_prompt_metrics_runtime(request.env)
    failed_record_retry_count = _get_failed_record_retry_count(request.env)
    failed_record_retry_delay_seconds = _get_failed_record_retry_delay_seconds(request.env)
    if progress_callback is not None:
        progress_callback(
            {
                "event_type": "batch_started",
                "message": "Wuying batch started",
                "status": "running",
                "started_at": started_at,
            }
        )

    records: list[dict[str, object]] = []
    prompt_files: list[dict[str, object]] = []
    finished_records = 0
    failed_records = 0
    finished_batches = 0
    failed_batches = 0
    last_error: str | None = None
    stopped_reason: str | None = None
    cancelled = False

    for platform in request.platforms:
        if _is_cancelled(cancellation_checker):
            cancelled = True
            stopped_reason = "cancelled by GEO"
            last_error = stopped_reason
            break
        platform_started_at = _utc_now()
        if progress_callback is not None:
            progress_callback(
                {
                    "event_type": "platform_started",
                    "message": f"Platform started: {platform}",
                    "status": "running",
                    "current_platform": platform,
                    "started_at": platform_started_at,
                }
            )
        for repeat_index in range(1, request.repeat + 1):
            for prompt_index, prompt in enumerate(request.prompts, start=1):
                if _is_cancelled(cancellation_checker):
                    cancelled = True
                    stopped_reason = "cancelled by GEO"
                    last_error = stopped_reason
                    logger.info(
                        "Batch task cancelled before platform=%s repeat_index=%s prompt_index=%s",
                        platform,
                        repeat_index,
                        prompt_index,
                    )
                    break
                if deadline.expired():
                    stopped_reason = (
                        f"{deadline.message()} before "
                        f"platform={platform}, repeat_index={repeat_index}, prompt_index={prompt_index}"
                    )
                    last_error = stopped_reason
                    logger.warning(stopped_reason)
                    break

                logger.info(
                    "Starting platform batch: task=%s platform=%s repeat=%s prompt_index=%s devices=%s",
                    task_id,
                    platform,
                    repeat_index,
                    prompt_index,
                    ",".join(device.device_id for device in devices),
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "event_type": "prompt_started",
                            "message": f"Prompt started: platform={platform}, prompt_index={prompt_index}",
                            "current_platform": platform,
                            "current_repeat_index": repeat_index,
                            "current_prompt_index": prompt_index,
                            "current_prompt": prompt,
                            "status": "running",
                            "started_at": _utc_now(),
                        }
                    )

                prompt_started_at = _utc_now()
                device_results = _run_platform_prompt_on_devices(
                    settings=settings,
                    worker_manager=worker_manager,
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    devices=devices,
                    record_timeout_seconds=record_timeout_seconds,
                    deadline=deadline,
                    attempt_index=1,
                    cancellation_checker=cancellation_checker,
                    progress_callback=progress_callback,
                )
                if _is_cancelled(cancellation_checker):
                    _mark_records_cancelled(device_results)
                device_results, attempt_results = _backfill_failed_device_results(
                    settings=settings,
                    worker_manager=worker_manager,
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    devices=devices,
                    initial_results=device_results,
                    record_timeout_seconds=record_timeout_seconds,
                    deadline=deadline,
                    retry_count=failed_record_retry_count,
                    retry_delay_seconds=failed_record_retry_delay_seconds,
                    cancellation_checker=cancellation_checker,
                    progress_callback=progress_callback,
                )
                if _is_cancelled(cancellation_checker):
                    cancelled = True
                    stopped_reason = "cancelled by GEO"
                    last_error = stopped_reason
                    _mark_records_cancelled(device_results)
                    _mark_records_cancelled(attempt_results)
                batch_status = _aggregate_device_status(device_results)
                finished_batches += 1
                if batch_status != "succeeded":
                    failed_batches += 1

                final_keys = {(item.device_id, item.attempt_index) for item in device_results}
                for attempt_result in attempt_results:
                    attempt_result.is_final_attempt = (
                        attempt_result.device_id,
                        attempt_result.attempt_index,
                    ) in final_keys
                    output_record = _build_output_record(
                        attempt_result,
                        raw_dir=raw_dir,
                    )
                    if not attempt_result.is_final_attempt:
                        continue
                    records.append(output_record)
                    finished_records += 1
                    if attempt_result.status != "succeeded":
                        failed_records += 1
                        last_error = attempt_result.error or last_error

                prompt_files = _write_prompt_result_files(
                    prompt_dir,
                    records,
                    file_stamp=file_stamp,
                    metrics_runtime=metrics_runtime,
                    changed_keys={(platform, prompt_index, prompt)},
                )
                prompt_finished_at = _utc_now()
                current_prompt_file = _find_prompt_file(
                    prompt_files=prompt_files,
                    platform=platform,
                    prompt_index=prompt_index,
                    prompt=prompt,
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "event_type": "prompt_finished",
                            "message": f"Prompt finished: platform={platform}, prompt_index={prompt_index}",
                            "records_path": None,
                            "output_file": str(prompt_dir),
                            "prompt_files": prompt_files,
                            "finished_records": finished_records,
                            "failed_records": failed_records,
                            "finished_batches": finished_batches,
                            "failed_batches": failed_batches,
                            "error": last_error,
                            "current_platform": platform,
                            "current_repeat_index": repeat_index,
                            "current_prompt_index": prompt_index,
                            "current_prompt": prompt,
                            "platform_batches": [
                                {
                                    "platform_id": f"wuying-{platform}",
                                    "platform": platform,
                                    "prompt_index": prompt_index,
                                    "repeat_index": repeat_index,
                                    "prompt": prompt,
                                    "device_ids": [device.device_id for device in devices],
                                    "status": batch_status,
                                    "started_at": prompt_started_at,
                                    "finished_at": prompt_finished_at,
                                    "output_path": current_prompt_file,
                                }
                            ],
                        }
                    )

                if deadline.expired():
                    stopped_reason = (
                        f"{deadline.message()} after platform={platform}, "
                        f"repeat_index={repeat_index}, prompt_index={prompt_index}"
                    )
                    last_error = stopped_reason
                    logger.warning(stopped_reason)
                    break
                if cancelled:
                    break

            if stopped_reason is not None:
                break
        if stopped_reason is not None:
            break
        if progress_callback is not None:
            progress_callback(
                {
                    "event_type": "platform_finished",
                    "message": f"Platform finished: {platform}",
                    "status": "running",
                    "current_platform": platform,
                    "started_at": platform_started_at,
                    "finished_at": _utc_now(),
                    "finished_batches": finished_batches,
                    "failed_batches": failed_batches,
                }
            )

    finished_at = _utc_now()
    overall_status = _aggregate_overall_status(
        total_batches=finished_batches,
        failed_batches=failed_batches,
        stopped_reason=stopped_reason,
        cancelled=cancelled,
    )
    prompt_files = _write_prompt_result_files(
        prompt_dir,
        records,
        file_stamp=file_stamp,
        metrics_runtime=metrics_runtime,
        changed_keys=set(),
    )

    return {
        "task_id": task_id,
        "status": overall_status,
        "started_at": started_at,
        "finished_at": finished_at,
        "records": records,
        "records_path": None,
        "output_file": str(prompt_dir),
        "prompt_files": prompt_files,
        "finished_records": finished_records,
        "failed_records": failed_records,
        "finished_batches": finished_batches,
        "failed_batches": failed_batches,
        "error": last_error,
        "current_platform": None,
        "current_repeat_index": None,
        "current_prompt_index": None,
        "current_prompt": None,
        "selected_devices": [device.to_dict() for device in devices],
    }


def _run_platform_prompt_on_devices(
    *,
    settings: AppSettings,
    worker_manager: WorkerManager,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
    deadline: BatchDeadline,
    attempt_index: int,
    cancellation_checker: CancellationChecker | None = None,
    progress_callback: ProgressCallback | None = None,
) -> list[DeviceRunRecord]:
    max_workers = max(1, min(len(devices), settings.batch_max_workers))
    results: list[DeviceRunRecord] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"{platform}-devices") as executor:
        if progress_callback is not None:
            for device in devices:
                progress_callback(
                    {
                        "event_type": "device_started",
                        "message": f"Device started: {device.device_id}",
                        "status": "running",
                        "current_platform": platform,
                        "current_repeat_index": repeat_index,
                        "current_prompt_index": prompt_index,
                        "current_prompt": prompt,
                        "record": {
                            "platform_id": f"wuying-{platform}",
                            "platform": platform,
                            "device_id": device.device_id,
                            "instance_id": device.instance_id,
                            "adb_endpoint": device.adb_endpoint,
                            "prompt_index": prompt_index,
                            "repeat_index": repeat_index,
                            "query": prompt,
                            "prompt": prompt,
                            "status": "running",
                            "attempt_index": attempt_index,
                            "started_at": _utc_now(),
                            "finished_at": None,
                            "result_path": None,
                            "error": None,
                        },
                    }
                )
        future_map = {
            executor.submit(
                _run_device,
                worker_manager=worker_manager,
                platform=platform,
                prompt=prompt,
                prompt_index=prompt_index,
                repeat_index=repeat_index,
                device=device,
                record_timeout_seconds=record_timeout_seconds,
                deadline=deadline,
                attempt_index=attempt_index,
                cancellation_checker=cancellation_checker,
            ): device
            for device in devices
        }
        for future in as_completed(future_map):
            result = future.result()
            results.append(result)
            if progress_callback is not None:
                progress_callback(
                    {
                        "event_type": "device_finished",
                        "message": f"Device finished: {result.device_id}",
                        "status": "running",
                        "current_platform": platform,
                        "current_repeat_index": repeat_index,
                        "current_prompt_index": prompt_index,
                        "current_prompt": prompt,
                        "record": _device_progress_record(result),
                    }
                )

    results.sort(key=lambda item: item.device_id)
    return results


def _backfill_failed_device_results(
    *,
    settings: AppSettings,
    worker_manager: WorkerManager,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    devices: list[DeviceTarget],
    initial_results: list[DeviceRunRecord],
    record_timeout_seconds: int,
    deadline: BatchDeadline,
    retry_count: int,
    retry_delay_seconds: float,
    cancellation_checker: CancellationChecker | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[DeviceRunRecord], list[DeviceRunRecord]]:
    attempt_results = list(initial_results)
    if retry_count <= 0:
        return initial_results, attempt_results

    results_by_device = {result.device_id: result for result in initial_results}
    devices_by_id = {device.device_id: device for device in devices}
    previous_failures: dict[str, list[dict[str, object]]] = {}

    for attempt in range(1, retry_count + 1):
        if _is_cancelled(cancellation_checker):
            break
        if deadline.expired():
            break
        failed_results = [
            result
            for result in results_by_device.values()
            if _needs_failed_record_backfill(result)
        ]
        retry_devices = [
            devices_by_id[result.device_id]
            for result in failed_results
            if result.device_id in devices_by_id
        ]
        if not retry_devices:
            break

        for failed_result in failed_results:
            previous_failures.setdefault(failed_result.device_id, []).append(
                {
                    "attempt": attempt - 1,
                    "status": failed_result.status,
                    "error": failed_result.error,
                    "started_at": failed_result.started_at,
                    "finished_at": failed_result.finished_at,
                }
            )

        logger.warning(
            "Backfilling failed device records: platform=%s repeat=%s prompt_index=%s attempt=%s/%s devices=%s",
            platform,
            repeat_index,
            prompt_index,
            attempt,
            retry_count,
            ",".join(device.device_id for device in retry_devices),
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "event_type": "backfill_started",
                    "message": (
                        f"Backfill started: platform={platform}, "
                        f"prompt_index={prompt_index}, attempt={attempt}/{retry_count}"
                    ),
                    "status": "running",
                    "current_platform": platform,
                    "current_repeat_index": repeat_index,
                    "current_prompt_index": prompt_index,
                    "current_prompt": prompt,
                    "records": [_device_progress_record(result) for result in failed_results],
                }
            )

        if retry_delay_seconds > 0:
            remaining = deadline.remaining_seconds()
            if remaining == 0:
                break
            time.sleep(min(retry_delay_seconds, remaining) if remaining is not None else retry_delay_seconds)

        retry_results = _run_platform_prompt_on_devices(
            settings=settings,
            worker_manager=worker_manager,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            devices=retry_devices,
            record_timeout_seconds=record_timeout_seconds,
            deadline=deadline,
            attempt_index=attempt + 1,
            cancellation_checker=cancellation_checker,
            progress_callback=progress_callback,
        )
        attempt_results.extend(retry_results)
        for retry_result in retry_results:
            _attach_backfill_metadata(
                retry_result,
                attempt=attempt,
                previous_failures=previous_failures.get(retry_result.device_id, []),
            )
            results_by_device[retry_result.device_id] = retry_result

        if progress_callback is not None:
            progress_callback(
                {
                    "event_type": "backfill_finished",
                    "message": (
                        f"Backfill finished: platform={platform}, "
                        f"prompt_index={prompt_index}, attempt={attempt}/{retry_count}"
                    ),
                    "status": "running",
                    "current_platform": platform,
                    "current_repeat_index": repeat_index,
                    "current_prompt_index": prompt_index,
                    "current_prompt": prompt,
                    "records": [_device_progress_record(result) for result in retry_results],
                }
            )

    return (
        sorted(results_by_device.values(), key=lambda item: item.device_id),
        sorted(attempt_results, key=lambda item: (item.device_id, item.attempt_index)),
    )


def _needs_failed_record_backfill(record: DeviceRunRecord) -> bool:
    if record.status != "succeeded":
        return True
    if not isinstance(record.result, dict):
        return True
    response = str(record.result.get("response") or "").strip()
    return not response


def _is_cancelled(cancellation_checker: CancellationChecker | None) -> bool:
    if cancellation_checker is None:
        return False
    try:
        return bool(cancellation_checker())
    except Exception as exc:
        logger.warning("Cancellation checker failed and will be ignored: %s", exc)
        return False


def _mark_records_cancelled(records: list[DeviceRunRecord]) -> None:
    for record in records:
        if record.status == "succeeded":
            continue
        record.status = "cancelled"
        record.error = "cancelled by GEO"


def _attach_backfill_metadata(
    record: DeviceRunRecord,
    *,
    attempt: int,
    previous_failures: list[dict[str, object]],
) -> None:
    if not isinstance(record.result, dict):
        return
    platform_extra = record.result.get("platform_extra")
    if not isinstance(platform_extra, dict):
        platform_extra = {}
    platform_extra["backfill"] = {
        "attempt": attempt,
        "previous_failures": previous_failures,
    }
    record.result["platform_extra"] = platform_extra


def _device_progress_record(record: DeviceRunRecord) -> dict[str, object]:
    return {
        "platform_id": f"wuying-{record.platform}",
        "platform": record.platform,
        "device_id": record.device_id,
        "instance_id": record.instance_id,
        "adb_endpoint": record.adb_endpoint,
        "prompt_index": record.prompt_index,
        "repeat_index": record.repeat_index,
        "query": record.prompt,
        "prompt": record.prompt,
        "status": record.status,
        "attempt_index": record.attempt_index,
        "is_final_attempt": record.is_final_attempt,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "result_path": record.result_path,
        "error": record.error,
    }


def _run_device(
    *,
    worker_manager: WorkerManager,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    device: DeviceTarget,
    record_timeout_seconds: int,
    deadline: BatchDeadline,
    attempt_index: int,
    cancellation_checker: CancellationChecker | None = None,
) -> DeviceRunRecord:
    started_at = _utc_now()
    if _is_cancelled(cancellation_checker):
        finished_at = _utc_now()
        return DeviceRunRecord(
            device_id=device.device_id,
            instance_id=device.instance_id,
            adb_endpoint=device.adb_endpoint,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            status="cancelled",
            started_at=started_at,
            finished_at=finished_at,
            attempt_index=attempt_index,
            error="cancelled by GEO",
        )
    timeout_seconds = deadline.record_timeout(record_timeout_seconds)
    if timeout_seconds <= 0:
        finished_at = _utc_now()
        return DeviceRunRecord(
            device_id=device.device_id,
            instance_id=device.instance_id,
            adb_endpoint=device.adb_endpoint,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            status="timeout",
            started_at=started_at,
            finished_at=finished_at,
            attempt_index=attempt_index,
            error="Batch timeout reached before device run started",
        )
    logger.info(
        "Device run started: platform=%s device=%s instance=%s repeat=%s prompt_index=%s attempt=%s timeout=%ss",
        platform,
        device.device_id,
        device.instance_id,
        repeat_index,
        prompt_index,
        attempt_index,
        timeout_seconds,
    )
    try:
        result = worker_manager.run_on_device(
            device=device,
            platform=platform,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            save_result=False,
        )
        integrity_error = _result_integrity_error(result)
        finished_at = _utc_now()
        if integrity_error:
            logger.warning(
                "Device run completed with incomplete result: platform=%s device=%s repeat=%s prompt_index=%s error=%s",
                platform,
                device.device_id,
                repeat_index,
                prompt_index,
                integrity_error,
            )
        else:
            logger.info(
                "Device run succeeded: platform=%s device=%s repeat=%s prompt_index=%s",
                platform,
                device.device_id,
                repeat_index,
                prompt_index,
            )
        return DeviceRunRecord(
            device_id=device.device_id,
            instance_id=device.instance_id,
            adb_endpoint=device.adb_endpoint,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            status="failed" if integrity_error else "succeeded",
            started_at=started_at,
            finished_at=finished_at,
            attempt_index=attempt_index,
            result_path=str(result.get("output_path") or "") or None,
            error=integrity_error,
            result=result,
        )
    except TimeoutError as exc:
        finished_at = _utc_now()
        logger.warning(
            "Device run timed out: platform=%s device=%s repeat=%s prompt_index=%s error=%s",
            platform,
            device.device_id,
            repeat_index,
            prompt_index,
            exc,
        )
        return DeviceRunRecord(
            device_id=device.device_id,
            instance_id=device.instance_id,
            adb_endpoint=device.adb_endpoint,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            status="timeout",
            started_at=started_at,
            finished_at=finished_at,
            attempt_index=attempt_index,
            error=str(exc),
        )
    except Exception as exc:
        finished_at = _utc_now()
        status = "cancelled" if _is_cancelled(cancellation_checker) else "failed"
        error = "cancelled by GEO" if status == "cancelled" else str(exc)
        logger.warning(
            "Device run failed: platform=%s device=%s repeat=%s prompt_index=%s error=%s",
            platform,
            device.device_id,
            repeat_index,
            prompt_index,
            error,
        )
        return DeviceRunRecord(
            device_id=device.device_id,
            instance_id=device.instance_id,
            adb_endpoint=device.adb_endpoint,
            platform=platform,
            prompt=prompt,
            prompt_index=prompt_index,
            repeat_index=repeat_index,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            attempt_index=attempt_index,
            error=error,
        )


def _build_output_record(record: DeviceRunRecord, *, raw_dir: Path) -> dict[str, object]:
    result = dict(record.result or {})
    response = str(result.get("response") or "") if result else ""
    references = result.get("references") if isinstance(result.get("references"), dict) else _empty_references()
    platform_extra = result.get("platform_extra") if isinstance(result.get("platform_extra"), dict) else {}

    output_record = {
        "platform_id": f"wuying-{record.platform}",
        "platform": record.platform,
        "device_id": record.device_id,
        "instance_id": record.instance_id,
        "adb_endpoint": record.adb_endpoint,
        "query": record.prompt,
        "prompt": record.prompt,
        "prompt_index": record.prompt_index,
        "repeat_index": record.repeat_index,
        "attempt_index": record.attempt_index,
        "is_final_attempt": record.is_final_attempt,
        "response": response,
        "references": references,
        "raw_output_path": None,
        "status": record.status,
        "error": record.error,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "platform_extra": platform_extra,
    }
    output_record["raw_output_path"] = str(_write_raw_record(record, output_record, raw_dir=raw_dir))
    return output_record


def _write_raw_record(record: DeviceRunRecord, payload: dict[str, object], *, raw_dir: Path) -> Path:
    raw_path = raw_dir / (
        f"{_safe_filename_part(record.platform)}_"
        f"{_safe_filename_part(record.device_id)}_"
        f"p{record.prompt_index:03d}_r{record.repeat_index:03d}_a{record.attempt_index:03d}.json"
    )
    raw_payload = dict(payload)
    raw_payload["raw_output_path"] = str(raw_path)
    _write_json_atomic(raw_path, raw_payload)
    return raw_path


def _task_root_dir(settings: AppSettings, task_id: str) -> Path:
    return settings.batch_output_dir.parent / "tasks" / task_id


def _write_prompt_result_files(
    prompt_dir: Path,
    records: list[dict[str, object]],
    *,
    file_stamp: str,
    metrics_runtime: dict[str, object] | None,
    changed_keys: set[tuple[str, int, str]] | None = None,
) -> list[dict[str, object]]:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, int, str], list[dict[str, object]]] = {}
    for record in records:
        platform = str(record.get("platform") or "").strip()
        prompt = str(record.get("prompt") or record.get("query") or "").strip()
        if not platform or not prompt:
            continue
        prompt_index = _coerce_int(record.get("prompt_index"), default=0)
        grouped.setdefault((platform, prompt_index, prompt), []).append(record)

    current_paths: set[Path] = set()
    prompt_files: list[dict[str, object]] = []
    for (platform, prompt_index, prompt), prompt_records in sorted(
        grouped.items(),
        key=lambda item: (item[0][1], item[0][0], item[0][2]),
    ):
        group_key = (platform, prompt_index, prompt)
        output_path = prompt_dir / _prompt_result_filename(
            file_stamp=file_stamp,
            platform=platform,
            prompt_index=prompt_index,
            prompt=prompt,
        )
        should_rewrite = (
            changed_keys is None
            or group_key in changed_keys
            or not output_path.exists()
        )
        if should_rewrite:
            prompt_payload = _apply_prompt_metrics(
                prompt_records,
                metrics_runtime=metrics_runtime,
                output_path=output_path,
            )
            _write_json_atomic(output_path, prompt_payload)
        current_paths.add(output_path)
        prompt_files.append(
            {
                "platform": platform,
                "platform_id": f"wuying-{platform}",
                "prompt_index": prompt_index,
                "prompt": prompt,
                "path": str(output_path),
                "record_count": len(prompt_records),
            }
        )

    for stale_path in prompt_dir.glob("*.json"):
        if stale_path not in current_paths:
            try:
                stale_path.unlink(missing_ok=True)
            except PermissionError:
                logger.warning("Prompt result file is locked and cannot be removed: %s", stale_path)

    return prompt_files


def _find_prompt_file(
    *,
    prompt_files: list[dict[str, object]],
    platform: str,
    prompt_index: int,
    prompt: str,
) -> str | None:
    for item in prompt_files:
        if (
            item.get("platform") == platform
            and item.get("prompt_index") == prompt_index
            and item.get("prompt") == prompt
        ):
            path = item.get("path")
            return str(path) if path else None
    return None

def _apply_prompt_metrics(
    prompt_records: list[dict[str, object]],
    *,
    metrics_runtime: dict[str, object] | None,
    output_path: Path,
) -> dict[str, object]:
    metrics_payload = _calculate_prompt_metrics(
        prompt_records,
        metrics_runtime=metrics_runtime,
        output_path=output_path,
    )
    metrics = {
        key: metrics_payload.get(key)
        for key in _default_prompt_metrics()
    }
    metric_summary = metrics_payload.get("metric_summary")
    clean_records = [_without_prompt_metrics(record) for record in prompt_records]
    platform = str(prompt_records[0].get("platform") or "") if prompt_records else ""
    prompt = str(prompt_records[0].get("prompt") or prompt_records[0].get("query") or "") if prompt_records else ""
    prompt_index = _coerce_int(prompt_records[0].get("prompt_index"), default=0) if prompt_records else 0
    repeat_indexes = sorted(
        {
            _coerce_int(record.get("repeat_index"), default=0)
            for record in prompt_records
            if record.get("repeat_index") is not None
        }
    )
    return {
        "platform_id": f"wuying-{platform}" if platform else "",
        "platform": platform,
        "query": prompt,
        "prompt": prompt,
        "prompt_index": prompt_index,
        "repeat_indexes": repeat_indexes,
        "record_count": len(clean_records),
        "records": clean_records,
        **metrics,
        **({"metric_summary": metric_summary} if metric_summary else {}),
    }


def _without_prompt_metrics(record: dict[str, object]) -> dict[str, object]:
    clean_record = dict(record)
    for key in _default_prompt_metrics():
        clean_record.pop(key, None)
    clean_record.pop("metric_summary", None)
    return clean_record


def _calculate_prompt_metrics(
    prompt_records: list[dict[str, object]],
    *,
    metrics_runtime: dict[str, object] | None,
    output_path: Path,
) -> dict[str, object]:
    default_metrics = _default_prompt_metrics()
    if not prompt_records:
        return default_metrics
    if not metrics_runtime:
        return default_metrics

    analyzer = metrics_runtime.get("analyzer")
    if analyzer is None:
        return default_metrics

    try:
        summary = analyzer.analyze_records(prompt_records, input_file=str(output_path))
        metrics = summary.get("metrics")
        if not isinstance(metrics, dict):
            return default_metrics
        payload: dict[str, object] = {
            "提及率": metrics.get("提及率"),
            "前三率": metrics.get("前三率"),
            "置顶率": metrics.get("置顶率"),
            "负面提及率": metrics.get("负面提及率"),
            "attitude": metrics.get("attitude"),
        }
        metric_summary = _build_metric_summary(summary)
        if metric_summary:
            payload["metric_summary"] = metric_summary
        return payload
    except Exception as exc:
        logger.warning("Prompt metrics calculation failed: path=%s error=%s", output_path, exc)
        return default_metrics


def _default_prompt_metrics() -> dict[str, object]:
    return {
        "提及率": None,
        "前三率": None,
        "置顶率": None,
        "负面提及率": None,
        "attitude": None,
    }


def _build_metric_summary(summary: object) -> dict[str, object] | None:
    if not isinstance(summary, dict):
        return None
    allowed_keys = {
        "input_file",
        "keyword",
        "task_type",
        "detect_type",
        "negative_words",
        "record_count",
        "brand",
        "negative_word_stats",
        "details",
    }
    compact = {key: summary[key] for key in allowed_keys if key in summary}
    return compact or None


def _create_prompt_metrics_runtime(task_env: dict[str, Any]) -> dict[str, object] | None:
    keyword = _normalize_metric_keyword(
        _first_non_empty(
            task_env.get("metric_keyword"),
            task_env.get("metricKeyword"),
            task_env.get("keyword"),
            task_env.get("target_keyword"),
            task_env.get("targetKeyword"),
            task_env.get("brand_keyword"),
            task_env.get("brandKeyword"),
            task_env.get("product_name"),
            task_env.get("productName"),
            os.getenv("PIPELINE_METRIC_KEYWORD"),
            os.getenv("METRIC_KEYWORD"),
        )
    )
    if not keyword:
        logger.info("Prompt metrics analyzer is disabled because metric keyword is not configured.")
        return None

    task_type = _normalize_task_type(
        _first_non_empty(
            task_env.get("task_type"),
            task_env.get("taskType"),
            task_env.get("metric_task_type"),
            task_env.get("metricTaskType"),
        )
    )
    raw_detect_type = (
        _first_non_empty(
            task_env.get("metric_detect_type"),
            task_env.get("metricDetectType"),
            task_env.get("detect_type"),
            task_env.get("detectType"),
            os.getenv("PIPELINE_METRIC_DETECT_TYPE"),
        )
        or "rank"
    ).strip().lower()
    detect_type = "negative" if _is_negative_metric_task(task_env, task_type=task_type) else raw_detect_type
    negative_words = _parse_negative_words(
        task_env.get("negative_words"),
        task_env.get("negativeWords"),
        task_env.get("metric_negative_words"),
        task_env.get("metricNegativeWords"),
        task_env.get("negative_keywords"),
        task_env.get("negativeKeywords"),
        os.getenv("PIPELINE_NEGATIVE_WORDS"),
        os.getenv("METRIC_NEGATIVE_WORDS"),
    )

    try:
        from wuying.application.prompt_metrics import PromptMetricsAnalyzer

        analyzer = PromptMetricsAnalyzer(
            keyword=keyword,
            detect_type=detect_type,
            api_key=_first_non_empty(
                task_env.get("metric_api_key"),
                task_env.get("metricApiKey"),
                os.getenv("PIPELINE_LLM_API_KEY"),
            ),
            base_url=_first_non_empty(
                task_env.get("metric_base_url"),
                task_env.get("metricBaseUrl"),
                os.getenv("PIPELINE_LLM_BASE_URL"),
            )
            or "https://ark.cn-beijing.volces.com/api/v3",
            model=_first_non_empty(
                task_env.get("metric_model"),
                task_env.get("metricModel"),
                os.getenv("PIPELINE_LLM_MODEL"),
            )
            or "doubao-seed-1-6-lite-251015",
            negative_words=negative_words,
            task_type=task_type,
        )
    except Exception as exc:
        logger.warning("Prompt metrics analyzer init failed: keyword=%s detect_type=%s error=%s", keyword, detect_type, exc)
        return None

    logger.info("Prompt metrics analyzer enabled: keyword=%s detect_type=%s", keyword, detect_type)
    return {
        "keyword": keyword,
        "task_type": task_type,
        "detect_type": detect_type,
        "analyzer": analyzer,
    }


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _normalize_metric_keyword(value: str | None) -> str | None:
    if value is None:
        return None
    keyword = value.strip()
    if len(keyword) >= 2 and keyword[0] == keyword[-1] and keyword[0] in {'"', "'"}:
        keyword = keyword[1:-1].strip()
    return keyword or None


def _normalize_task_type(value: str | None) -> str:
    return (value or "normal_monitor").strip().lower() or "normal_monitor"


def _is_negative_metric_task(task_env: dict[str, Any], *, task_type: str | None = None) -> bool:
    if _normalize_task_type(task_type) == "negative_mention":
        return True
    raw = _first_non_empty_config_value(
        task_env.get("is_negative"),
        task_env.get("isNegative"),
        task_env.get("negative"),
        task_env.get("metric_is_negative"),
        task_env.get("metricIsNegative"),
    )
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "negative", "负面"}


def _parse_negative_words(*values: object) -> list[str]:
    for value in values:
        parsed = _parse_negative_words_value(value)
        if parsed:
            return parsed
    return []


def _parse_negative_words_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, list):
        return [str(item).strip() for item in payload if str(item).strip()]
    return [
        item.strip()
        for item in re.split(r"[,，;；\n]+", raw)
        if item.strip()
    ]


def _prompt_result_filename(*, file_stamp: str, platform: str, prompt_index: int, prompt: str) -> str:
    platform_part = _safe_filename_part(platform)[:40]
    prompt_part = _safe_filename_part(prompt)[:80]
    return f"{file_stamp}-{platform_part}-p{prompt_index:03d}-{prompt_part}.json"


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.write_text(content, encoding="utf-8")
        return
    except PermissionError:
        logger.debug("Direct result write failed, falling back to replace: %s", path)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    for attempt in range(10):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                path.write_text(content, encoding="utf-8")
                try:
                    tmp_path.unlink(missing_ok=True)
                except PermissionError:
                    logger.debug("Temporary result file is locked and cannot be removed: %s", tmp_path)
                return
            time.sleep(0.5)


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "unknown"


def _empty_references() -> dict[str, object]:
    return {"summary": None, "keywords": [], "items": []}


def _result_integrity_error(result: dict[str, object]) -> str | None:
    response = str(result.get("response") or "").strip()
    if not response:
        return "response empty"

    platform_extra = result.get("platform_extra")
    if not isinstance(platform_extra, dict):
        return None

    reference_collection = platform_extra.get("reference_collection")
    if not isinstance(reference_collection, dict):
        return None

    status = str(reference_collection.get("status") or "").strip()
    if status not in {"missing", "partial"}:
        return None

    expected_count = reference_collection.get("expected_count")
    collected_count = reference_collection.get("collected_count")
    if isinstance(expected_count, int):
        return (
            "reference collection incomplete: "
            f"status={status}, expected={expected_count}, collected={collected_count}"
        )
    return f"reference collection incomplete: status={status}, collected={collected_count}"


def _aggregate_device_status(device_results: list[DeviceRunRecord]) -> str:
    if not device_results:
        return "failed"
    if all(item.status == "cancelled" for item in device_results):
        return "cancelled"
    success_count = sum(1 for item in device_results if item.status == "succeeded")
    if success_count == len(device_results):
        return "succeeded"
    if success_count == 0:
        return "failed"
    return "partial_failed"


def _aggregate_overall_status(
    *,
    total_batches: int,
    failed_batches: int,
    stopped_reason: str | None,
    cancelled: bool = False,
) -> str:
    if cancelled:
        return "cancelled"
    if total_batches == 0:
        return "timeout" if stopped_reason else "failed"
    if stopped_reason and failed_batches:
        return "timeout" if failed_batches == total_batches else "partial_failed"
    if stopped_reason:
        return "timeout"
    if failed_batches == 0:
        return "succeeded"
    if failed_batches == total_batches:
        return "failed"
    return "partial_failed"


def _get_failed_record_retry_count(task_env: dict[str, Any]) -> int:
    raw = (
        _first_non_empty_config_value(
            task_env.get("failed_record_retry_count"),
            task_env.get("failedRecordRetryCount"),
            os.getenv("CRAWLER_FAILED_RECORD_RETRY_COUNT"),
        )
        or "0"
    )
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(f"CRAWLER_FAILED_RECORD_RETRY_COUNT must be an integer, got {raw!r}") from exc


def _get_failed_record_retry_delay_seconds(task_env: dict[str, Any]) -> float:
    raw = (
        _first_non_empty_config_value(
            task_env.get("failed_record_retry_delay_seconds"),
            task_env.get("failedRecordRetryDelaySeconds"),
            os.getenv("CRAWLER_FAILED_RECORD_RETRY_DELAY_SECONDS"),
        )
        or "2"
    )
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise ValueError(
            "CRAWLER_FAILED_RECORD_RETRY_DELAY_SECONDS must be a number, "
            f"got {raw!r}"
        ) from exc


def _first_non_empty_config_value(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        return str(value)
    return None


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


__all__ = ["run_batch_job_with_workers"]
