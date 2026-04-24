from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from wuying.application.batch_models import BatchTaskRequest, DeviceRunRecord
from wuying.application.device_pool import DeviceTarget
from wuying.application.worker_manager import WorkerManager
from wuying.config import AppSettings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, object]], None]


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
) -> dict[str, object]:
    started_at = _utc_now()
    deadline = None
    if batch_timeout_seconds and batch_timeout_seconds > 0:
        deadline = datetime.now(tz=UTC).timestamp() + batch_timeout_seconds

    task_root_dir = _task_root_dir(settings, task_id)
    raw_dir = task_root_dir / "raw"
    prompt_dir = task_root_dir / "prompts"
    file_stamp = datetime.now().strftime("%Y-%m-%d-%H")
    task_root_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    worker_manager.start_all(devices)
    metrics_runtime = _create_prompt_metrics_runtime(request.env)
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

    for platform in request.platforms:
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
                    worker_manager=worker_manager,
                    platform=platform,
                    prompt=prompt,
                    prompt_index=prompt_index,
                    repeat_index=repeat_index,
                    devices=devices,
                    record_timeout_seconds=record_timeout_seconds,
                    progress_callback=progress_callback,
                )
                batch_status = _aggregate_device_status(device_results)
                finished_batches += 1
                if batch_status != "succeeded":
                    failed_batches += 1

                for device_result in device_results:
                    output_record = _build_output_record(
                        device_result,
                        raw_dir=raw_dir,
                    )
                    records.append(output_record)
                    finished_records += 1
                    if device_result.status != "succeeded":
                        failed_records += 1
                        last_error = device_result.error or last_error

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
    worker_manager: WorkerManager,
    platform: str,
    prompt: str,
    prompt_index: int,
    repeat_index: int,
    devices: list[DeviceTarget],
    record_timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> list[DeviceRunRecord]:
    max_workers = max(1, len(devices))
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
        result = worker_manager.run_on_device(
            device=device,
            platform=platform,
            prompt=prompt,
            timeout_seconds=record_timeout_seconds,
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
        f"p{record.prompt_index:03d}_r{record.repeat_index:03d}.json"
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
    metrics = _calculate_prompt_metrics(
        prompt_records,
        metrics_runtime=metrics_runtime,
        output_path=output_path,
    )
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
    }


def _without_prompt_metrics(record: dict[str, object]) -> dict[str, object]:
    clean_record = dict(record)
    for key in _default_prompt_metrics():
        clean_record.pop(key, None)
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
        return {
            "提及率": metrics.get("提及率"),
            "前三率": metrics.get("前三率"),
            "置顶率": metrics.get("置顶率"),
            "负面提及率": metrics.get("负面提及率"),
            "attitude": metrics.get("attitude"),
        }
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


def _create_prompt_metrics_runtime(task_env: dict[str, Any]) -> dict[str, object] | None:
    keyword = _first_non_empty(
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
    if not keyword:
        logger.info("Prompt metrics analyzer is disabled because metric keyword is not configured.")
        return None

    detect_type = (
        _first_non_empty(
            task_env.get("metric_detect_type"),
            task_env.get("metricDetectType"),
            task_env.get("detect_type"),
            task_env.get("detectType"),
            os.getenv("PIPELINE_METRIC_DETECT_TYPE"),
        )
        or "rank"
    ).strip().lower()

    try:
        from pipeline import PromptMetricsAnalyzer

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
        )
    except Exception as exc:
        logger.warning("Prompt metrics analyzer init failed: keyword=%s detect_type=%s error=%s", keyword, detect_type, exc)
        return None

    logger.info("Prompt metrics analyzer enabled: keyword=%s detect_type=%s", keyword, detect_type)
    return {
        "keyword": keyword,
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
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    for attempt in range(5):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                path.write_text(content, encoding="utf-8")
                try:
                    tmp_path.unlink(missing_ok=True)
                except PermissionError:
                    logger.warning("Temporary result file is locked and cannot be removed: %s", tmp_path)
                return
            time.sleep(0.2)


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "unknown"


def _empty_references() -> dict[str, object]:
    return {"summary": None, "keywords": [], "items": []}


def _result_integrity_error(result: dict[str, object]) -> str | None:
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


__all__ = ["run_batch_job_with_workers"]
