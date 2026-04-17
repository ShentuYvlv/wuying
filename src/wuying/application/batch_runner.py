from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from wuying.application.batch_models import BatchTaskRequest, DeviceRunRecord, PlatformPromptBatchRecord
from wuying.application.device_pool import DeviceTarget, resolve_execution_devices
from wuying.application.runner import run_platform_once_with_timeout
from wuying.config import AppSettings

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[dict[str, object]], None]


def run_batch_job(
    *,
    settings: AppSettings,
    task_id: str,
    request: BatchTaskRequest,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
    batch_timeout_seconds: int | None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    started_at = _utc_now()
    deadline = None
    if batch_timeout_seconds and batch_timeout_seconds > 0:
        deadline = datetime.now(tz=UTC).timestamp() + batch_timeout_seconds

    batch_root_dir = settings.batch_output_dir / task_id
    batch_root_dir.mkdir(parents=True, exist_ok=True)

    platform_batches: list[dict[str, object]] = []
    flattened_results: list[dict[str, object]] = []
    finished_records = 0
    failed_records = 0
    finished_batches = 0
    failed_batches = 0
    last_error: str | None = None
    stopped_reason: str | None = None

    for platform in request.platforms:
        for repeat_index in range(1, request.repeat + 1):
            for prompt_index, prompt in enumerate(request.prompts, start=1):
                if deadline is not None and datetime.now(tz=UTC).timestamp() > deadline:
                    stopped_reason = (
                        f"Batch task timed out after {batch_timeout_seconds}s before "
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
                platform_started_at = _utc_now()
                if progress_callback is not None:
                    progress_callback(
                        {
                            "current_platform": platform,
                            "current_repeat_index": repeat_index,
                            "current_prompt_index": prompt_index,
                            "current_prompt": prompt,
                            "status": "running",
                        }
                    )

                device_results = _run_platform_prompt_on_devices(
                    settings=settings,
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    devices=devices,
                    record_timeout_seconds=record_timeout_seconds,
                )
                batch_status = _aggregate_device_status(device_results)
                platform_finished_at = _utc_now()
                platform_output_path = _write_platform_batch_summary(
                    batch_root_dir=batch_root_dir,
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    status=batch_status,
                    started_at=platform_started_at,
                    finished_at=platform_finished_at,
                    device_results=device_results,
                )

                batch_record = PlatformPromptBatchRecord(
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    device_ids=[device.device_id for device in devices],
                    status=batch_status,
                    started_at=platform_started_at,
                    finished_at=platform_finished_at,
                    output_path=str(platform_output_path),
                    results=device_results,
                )
                platform_batches.append(batch_record.to_dict())
                finished_batches += 1
                if batch_status != "succeeded":
                    failed_batches += 1

                for device_result in device_results:
                    flattened_results.append(_flatten_device_result(device_result))
                    finished_records += 1
                    if device_result.status != "succeeded":
                        failed_records += 1
                        last_error = device_result.error or last_error

                if progress_callback is not None:
                    progress_callback(
                        {
                            "platform_batches": platform_batches,
                            "results": flattened_results,
                            "finished_records": finished_records,
                            "failed_records": failed_records,
                            "finished_batches": finished_batches,
                            "failed_batches": failed_batches,
                            "error": last_error,
                            "current_platform": platform,
                            "current_repeat_index": repeat_index,
                            "current_prompt_index": prompt_index,
                            "current_prompt": prompt,
                        }
                    )

            if stopped_reason is not None:
                break
        if stopped_reason is not None:
            break

    finished_at = _utc_now()
    overall_status = _aggregate_overall_status(
        total_batches=finished_batches,
        failed_batches=failed_batches,
        stopped_reason=stopped_reason,
    )
    summary_path = _write_batch_summary(
        batch_root_dir=batch_root_dir,
        task_id=task_id,
        platforms=request.platforms,
        device_ids=[device.device_id for device in devices],
        prompts=request.prompts,
        repeat=request.repeat,
        status=overall_status,
        started_at=started_at,
        finished_at=finished_at,
        platform_batches=platform_batches,
        error=last_error,
    )

    return {
        "status": overall_status,
        "started_at": started_at,
        "finished_at": finished_at,
        "platform_batches": platform_batches,
        "results": flattened_results,
        "finished_records": finished_records,
        "failed_records": failed_records,
        "finished_batches": finished_batches,
        "failed_batches": failed_batches,
        "summary_path": str(summary_path),
        "error": last_error,
        "current_platform": None,
        "current_repeat_index": None,
        "current_prompt_index": None,
        "current_prompt": None,
        "selected_devices": [device.to_dict() for device in devices],
    }


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


def _run_platform_prompt_on_devices(
    *,
    settings: AppSettings,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
) -> list[DeviceRunRecord]:
    max_workers = max(1, min(len(devices), settings.batch_max_workers))
    results: list[DeviceRunRecord] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"{platform}-devices") as executor:
        future_map = {
            executor.submit(
                _run_device,
                settings=settings,
                platform=platform,
                prompt=prompt,
                prompt_index=prompt_index,
                repeat_index=repeat_index,
                device=device,
                record_timeout_seconds=record_timeout_seconds,
            ): device
            for device in devices
        }
        for future in as_completed(future_map):
            results.append(future.result())

    results.sort(key=lambda item: item.device_id)
    return results


def _run_device(
    *,
    settings: AppSettings,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    device: DeviceTarget,
    record_timeout_seconds: int,
) -> DeviceRunRecord:
    started_at = _utc_now()
    logger.info(
        "Device run started: platform=%s device=%s instance=%s repeat=%s prompt_index=%s",
        platform,
        device.device_id,
        device.instance_id,
        repeat_index,
        prompt_index,
    )
    try:
        result = run_platform_once_with_timeout(
            settings=settings,
            platform_name=platform,
            prompt=prompt,
            device=device,
            timeout_seconds=record_timeout_seconds,
            save_result=False,
        ).to_dict()
        finished_at = _utc_now()
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
            status="succeeded",
            started_at=started_at,
            finished_at=finished_at,
            result_path=str(result.get("output_path") or "") or None,
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
            error=str(exc),
        )
    except Exception as exc:
        finished_at = _utc_now()
        logger.warning(
            "Device run failed: platform=%s device=%s repeat=%s prompt_index=%s error=%s",
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
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            error=str(exc),
        )


def _flatten_device_result(record: DeviceRunRecord) -> dict[str, object]:
    if record.result:
        flattened = dict(record.result)
        flattened["device_id"] = record.device_id
        flattened["adb_endpoint"] = record.adb_endpoint
        flattened["status"] = record.status
        flattened["prompt_index"] = record.prompt_index
        flattened["repeat_index"] = record.repeat_index
        return flattened

    return {
        "platform": record.platform,
        "instance_id": record.instance_id,
        "device_id": record.device_id,
        "adb_endpoint": record.adb_endpoint,
        "prompt": record.prompt,
        "status": record.status,
        "error": record.error,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "prompt_index": record.prompt_index,
        "repeat_index": record.repeat_index,
    }


def _write_platform_batch_summary(
    *,
    batch_root_dir: Path,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    status: str,
    started_at: str,
    finished_at: str,
    device_results: list[DeviceRunRecord],
) -> Path:
    platform_dir = batch_root_dir / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    output_path = platform_dir / f"repeat_{repeat_index:03d}_prompt_{prompt_index:03d}.json"
    payload = {
        "platform": platform,
        "prompt": prompt,
        "prompt_index": prompt_index,
        "repeat_index": repeat_index,
        "device_ids": [item.device_id for item in device_results],
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "results": [item.to_dict() for item in device_results],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _write_batch_summary(
    *,
    batch_root_dir: Path,
    task_id: str,
    platforms: list[str],
    device_ids: list[str],
    prompts: list[str],
    repeat: int,
    status: str,
    started_at: str,
    finished_at: str,
    platform_batches: list[dict[str, object]],
    error: str | None,
) -> Path:
    output_path = batch_root_dir / "summary.json"
    payload = {
        "job_id": task_id,
        "platforms": platforms,
        "device_ids": device_ids,
        "prompts": prompts,
        "repeat": repeat,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "error": error,
        "platform_batches": [_compact_platform_batch(item) for item in platform_batches],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _compact_platform_batch(batch: dict[str, object]) -> dict[str, object]:
    return {
        "platform": batch.get("platform"),
        "prompt": batch.get("prompt"),
        "prompt_index": batch.get("prompt_index"),
        "repeat_index": batch.get("repeat_index"),
        "device_ids": batch.get("device_ids", []),
        "status": batch.get("status"),
        "started_at": batch.get("started_at"),
        "finished_at": batch.get("finished_at"),
        "output_path": batch.get("output_path"),
        "results": [
            _compact_device_result(item)
            for item in batch.get("results", [])
            if isinstance(item, dict)
        ],
    }


def _compact_device_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "device_id": result.get("device_id"),
        "instance_id": result.get("instance_id"),
        "adb_endpoint": result.get("adb_endpoint"),
        "platform": result.get("platform"),
        "prompt_index": result.get("prompt_index"),
        "repeat_index": result.get("repeat_index"),
        "status": result.get("status"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "result_path": result.get("result_path"),
        "error": result.get("error"),
    }


def _aggregate_device_status(device_results: list[DeviceRunRecord]) -> str:
    if not device_results:
        return "failed"
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
) -> str:
    if total_batches == 0:
        return "failed"
    if stopped_reason and failed_batches:
        return "partial_failed"
    if stopped_reason:
        return "failed"
    if failed_batches == 0:
        return "succeeded"
    if failed_batches == total_batches:
        return "failed"
    return "partial_failed"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


__all__ = ["resolve_batch_devices", "run_batch_job"]
