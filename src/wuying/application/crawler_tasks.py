from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from wuying.application.batch_models import BatchTaskRequest
from wuying.application.batch_runner import resolve_batch_devices, run_batch_job
from wuying.application.device_lease import DeviceLeaseManager
from wuying.application.device_pool import load_device_pool
from wuying.application.platform_registry import available_platform_names, get_platform_definition
from wuying.application.worker_manager import WorkerManager
from wuying.config import AppSettings

logger = logging.getLogger(__name__)


PLATFORM_ID_TO_INTERNAL_PLATFORM: dict[str, str] = {
    "wuying-doubao": "doubao",
    "wuying-deepseek": "deepseek",
    "wuying-kimi": "kimi",
    "wuying-qianwen": "qianwen",
    "wuying-yuanbao": "yuanbao",
}
INTERNAL_PLATFORM_TO_API_PLATFORM: dict[str, str] = {
    internal: platform_id for platform_id, internal in PLATFORM_ID_TO_INTERNAL_PLATFORM.items()
}


def validate_platform_id(platform_id: str) -> None:
    if platform_id not in PLATFORM_ID_TO_INTERNAL_PLATFORM:
        available = ", ".join(sorted(PLATFORM_ID_TO_INTERNAL_PLATFORM))
        raise ValueError(f"Unsupported platform_id: {platform_id}. Available: {available}")


def normalize_platform_inputs(platforms: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in platforms:
        value = raw.strip().lower()
        if not value:
            continue
        if value in PLATFORM_ID_TO_INTERNAL_PLATFORM:
            normalized.append(PLATFORM_ID_TO_INTERNAL_PLATFORM[value])
            continue
        get_platform_definition(value)
        normalized.append(value)
    if not normalized:
        available = ", ".join(available_platform_names())
        raise ValueError(f"No valid platforms configured. Available: {available}")
    return normalized


def api_platform_id_for_internal(platform_name: str) -> str:
    return INTERNAL_PLATFORM_TO_API_PLATFORM.get(platform_name, platform_name)


@dataclass(frozen=True, slots=True)
class CrawlerTaskRequest:
    platform_id: str
    prompts: list[str]
    repeat: int
    save_name: str | None
    env: dict[str, Any]
    instance_id: str | None = None

    @property
    def expected_records(self) -> int:
        return len(self.prompts) * self.repeat


@dataclass(frozen=True, slots=True)
class BatchCrawlerTaskRequest:
    platforms: list[str]
    prompts: list[str]
    repeat: int
    save_name: str | None
    env: dict[str, Any]
    device_ids: list[str] | None = None
    instance_id: str | None = None


class TaskConflictError(RuntimeError):
    pass


class TaskStore:
    def __init__(self, root_dir: Path | str = Path("data/tasks")) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def dir_for(self, task_id: str) -> Path:
        return self.root_dir / task_id

    def path_for(self, task_id: str) -> Path:
        return self.dir_for(task_id) / "status.json"

    def read_records(self, task_id: str) -> list[dict[str, Any]]:
        raw_dir = self.dir_for(task_id) / "raw"
        records: list[dict[str, Any]] = []
        if raw_dir.exists():
            for path in sorted(raw_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        if records:
            return sorted(
                records,
                key=lambda item: (
                    _coerce_int(item.get("prompt_index"), default=0),
                    _coerce_int(item.get("repeat_index"), default=0),
                    str(item.get("platform") or ""),
                    str(item.get("device_id") or ""),
                    _coerce_int(item.get("attempt_index"), default=1),
                ),
            )

        legacy_path = self.dir_for(task_id) / "records.json"
        if not legacy_path.exists():
            return []
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload["task_id"])
        with self._lock:
            self._write_atomic(self.path_for(task_id), payload)
        return payload

    def get(self, task_id: str) -> dict[str, Any]:
        path = self.path_for(task_id)
        if not path.exists():
            raise FileNotFoundError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get(task_id)
            current.update(patch)
            self._write_atomic(self.path_for(task_id), current)
        return current

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            path.write_text(content, encoding="utf-8")
            return
        except PermissionError:
            logger.debug("Direct task status write failed, falling back to replace: %s", path)
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
                        logger.debug("Temporary task status file is locked and cannot be removed: %s", tmp_path)
                    return
                time.sleep(0.5)


class CrawlerTaskService:
    def __init__(self, *, settings: AppSettings) -> None:
        self.settings = settings
        self.store = TaskStore(settings.batch_output_dir.parent / "tasks")
        self.record_timeout_seconds = _get_env_int("CRAWLER_RECORD_TIMEOUT_SECONDS", 300)
        self.batch_timeout_seconds = settings.batch_timeout_seconds
        self.lease_manager = DeviceLeaseManager(
            settings.device.device_lease_dir,
            stale_after_seconds=settings.device.device_lease_ttl_seconds,
        )
        self.worker_manager = WorkerManager(settings)
        self.callback_timeout_seconds = _get_env_int("CRAWLER_CALLBACK_TIMEOUT_SECONDS", 10)
        self.callback_max_workers = _get_env_int("CRAWLER_CALLBACK_MAX_WORKERS", 2)
        self._task_executor: ThreadPoolExecutor | None = None
        self._callback_executor: ThreadPoolExecutor | None = None
        self._futures: set[Future[None]] = set()
        self._callback_futures: set[Future[None]] = set()
        self._task_futures: dict[str, Future[None]] = {}
        self._task_device_ids: dict[str, list[str]] = {}
        self._cancelled_task_ids: set[str] = set()
        self._reserved_device_ids: set[str] = set()
        self._reservation_lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._task_executor is not None:
            return
        self._stop_event.clear()
        self.worker_manager.start_all(load_device_pool(self.settings).enabled_devices())
        self._task_executor = ThreadPoolExecutor(
            max_workers=max(1, self.settings.batch_max_workers),
            thread_name_prefix="wuying-task",
        )
        self._callback_executor = ThreadPoolExecutor(
            max_workers=max(1, self.callback_max_workers),
            thread_name_prefix="wuying-callback",
        )

    def stop(self) -> None:
        self._stop_event.set()
        executor = self._task_executor
        callback_executor = self._callback_executor
        self._task_executor = None
        self._callback_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if callback_executor is not None:
            callback_executor.shutdown(wait=False, cancel_futures=True)
        self.worker_manager.stop_all()

    def submit(self, request: CrawlerTaskRequest) -> dict[str, Any]:
        validate_platform_id(request.platform_id)
        batch_request = BatchTaskRequest(
            platforms=[PLATFORM_ID_TO_INTERNAL_PLATFORM[request.platform_id]],
            prompts=request.prompts,
            repeat=request.repeat,
            save_name=request.save_name,
            env=request.env,
            device_ids=None,
            legacy_instance_id=request.instance_id,
            default_to_all_pool_devices=False,
        )
        return self._submit_common(
            kind="v1",
            batch_request=batch_request,
            raw_request=request,
            type_name=request.platform_id,
            platform_ids=[request.platform_id],
        )

    def submit_batch(self, request: BatchCrawlerTaskRequest) -> dict[str, Any]:
        internal_platforms = normalize_platform_inputs(request.platforms)
        platform_ids = [api_platform_id_for_internal(item) for item in internal_platforms]
        batch_request = BatchTaskRequest(
            platforms=internal_platforms,
            prompts=request.prompts,
            repeat=request.repeat,
            save_name=request.save_name,
            env=request.env,
            device_ids=request.device_ids,
            legacy_instance_id=request.instance_id,
            default_to_all_pool_devices=True,
        )
        return self._submit_common(
            kind="v2",
            batch_request=batch_request,
            raw_request=request,
            type_name="wuying-batch",
            platform_ids=platform_ids,
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.store.get(task_id)

    def get_results(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        records = self.store.read_records(task_id)
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "type": task["type"],
            "platforms": task.get("platforms", []),
            "platform_ids": task.get("platform_ids", []),
            "device_ids": task.get("device_ids", []),
            "selected_devices": task.get("selected_devices", []),
            "records_path": task.get("records_path"),
            "prompt_files": task.get("prompt_files", []),
            "records": records,
            "results": records,
            "callback": task.get("callback"),
            "error": task.get("error"),
        }

    def _submit_common(
        self,
        *,
        kind: str,
        batch_request: BatchTaskRequest,
        raw_request: CrawlerTaskRequest | BatchCrawlerTaskRequest,
        type_name: str,
        platform_ids: list[str],
    ) -> dict[str, Any]:
        devices = resolve_batch_devices(self.settings, batch_request)
        task_id = _build_task_id()
        device_ids = [device.device_id for device in devices]
        self._reserve_devices_or_raise(device_ids, owner=task_id)

        created_at = _utc_now()
        output_dir = self.store.dir_for(task_id) / "prompts"
        task_payload = {
            "task_id": task_id,
            "trace_id": task_id,
            "type": type_name,
            "kind": kind,
            "status": "pending",
            "expected_records": len(batch_request.platforms) * len(batch_request.prompts) * batch_request.repeat * len(devices),
            "expected_batches": len(batch_request.platforms) * len(batch_request.prompts) * batch_request.repeat,
            "finished_records": 0,
            "failed_records": 0,
            "finished_batches": 0,
            "failed_batches": 0,
            "output_file": str(output_dir),
            "records_path": None,
            "prompt_files": [],
            "save_name": batch_request.save_name,
            "prompts": batch_request.prompts,
            "repeat": batch_request.repeat,
            "env": dict(batch_request.env),
            "instance_id": getattr(raw_request, "instance_id", None),
            "device_ids": device_ids,
            "selected_devices": [device.to_dict() for device in devices],
            "platforms": batch_request.platforms,
            "platform_ids": platform_ids,
            "callback": {"status": "pending"},
            "error": None,
            "current_platform": None,
            "current_repeat_index": None,
            "current_prompt_index": None,
            "current_prompt": None,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
        }
        try:
            self.store.create(task_payload)
            self._submit_execution(
                task_id,
                {
                    "task_id": task_id,
                    "batch_request": batch_request,
                    "devices": devices,
                    "platform_ids": platform_ids,
                }
            )
        except Exception:
            self._release_reservation(device_ids)
            raise
        return task_payload

    def _submit_execution(self, task_id: str, item: dict[str, Any]) -> None:
        executor = self._task_executor
        if executor is None:
            raise RuntimeError("CrawlerTaskService is not started.")
        future = executor.submit(self._execute_task, item)
        with self._reservation_lock:
            self._futures.add(future)
            self._task_futures[task_id] = future
            self._task_device_ids[task_id] = [device.device_id for device in item["devices"]]
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future: Future[None]) -> None:
        with self._reservation_lock:
            self._futures.discard(future)
            finished_task_ids = [
                task_id for task_id, task_future in self._task_futures.items() if task_future is future
            ]
            for task_id in finished_task_ids:
                self._task_futures.pop(task_id, None)
                self._task_device_ids.pop(task_id, None)
                self._cancelled_task_ids.discard(task_id)

    def _reserve_devices_or_raise(self, device_ids: list[str], *, owner: str) -> None:
        unique_device_ids = sorted(set(device_ids))
        with self._reservation_lock:
            reserved = [device_id for device_id in unique_device_ids if device_id in self._reserved_device_ids]
            if reserved:
                raise TaskConflictError(
                    f"Device {', '.join(reserved)} is already reserved by another running task."
                )

            leased: list[str] = []
            for device_id in unique_device_ids:
                lease = self.lease_manager.read(device_id)
                if lease is not None:
                    leased.append(f"{device_id}({lease.owner})")
            if leased:
                raise TaskConflictError(f"Device {', '.join(leased)} is already leased.")

            self._reserved_device_ids.update(unique_device_ids)

    def _release_reservation(self, device_ids: list[str]) -> None:
        with self._reservation_lock:
            for device_id in set(device_ids):
                self._reserved_device_ids.discard(device_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        status = str(task.get("status") or "")
        if status in {"succeeded", "failed", "timeout", "partial_failed", "cancelled"}:
            return {
                "task_id": task_id,
                "status": status,
                "cancelled": status == "cancelled",
                "message": f"batch already {status}",
            }

        device_ids = [str(item) for item in task.get("device_ids", []) if str(item).strip()]
        with self._reservation_lock:
            self._cancelled_task_ids.add(task_id)
            future = self._task_futures.get(task_id)
        future_cancelled = future.cancel() if future is not None else False

        self.worker_manager.cancel_devices(device_ids, reason=f"cancelled task {task_id}")
        self._release_reservation(device_ids)
        if future_cancelled:
            with self._reservation_lock:
                self._task_futures.pop(task_id, None)
                self._task_device_ids.pop(task_id, None)
                self._cancelled_task_ids.discard(task_id)

        cancelled_at = _utc_now()
        task = self.store.update(
            task_id,
            {
                "status": "cancelled",
                "finished_at": cancelled_at,
                "error": "cancelled by GEO",
                "current_platform": None,
                "current_repeat_index": None,
                "current_prompt_index": None,
                "current_prompt": None,
            },
        )
        self._upload_progress(
            task=task,
            patch={
                "event_type": "batch_cancelled",
                "message": "Wuying batch cancelled by GEO",
                "status": "cancelled",
                "finished_at": cancelled_at,
                "error": "cancelled by GEO",
            },
        )
        return {
            "task_id": task_id,
            "status": "cancelled",
            "cancelled": True,
            "message": "batch cancelled",
        }

    def _is_task_cancelled(self, task_id: str) -> bool:
        with self._reservation_lock:
            return task_id in self._cancelled_task_ids

    def _execute_task(self, item: dict[str, Any]) -> None:
        task_id = str(item["task_id"])
        batch_request = item["batch_request"]
        devices = item["devices"]
        device_ids = [device.device_id for device in devices]
        started_at = _utc_now()
        lease_acquired = False
        task = self.store.get(task_id)
        try:
            self.lease_manager.acquire_many(device_ids, owner=task_id)
            lease_acquired = True
            self.store.update(
                task_id,
                {
                    "status": "running",
                    "started_at": started_at,
                },
            )
            batch_result = run_batch_job(
                settings=self.settings,
                task_id=task_id,
                request=batch_request,
                devices=devices,
                record_timeout_seconds=self.record_timeout_seconds,
                batch_timeout_seconds=self.batch_timeout_seconds,
                progress_callback=lambda patch: self._handle_progress(task_id, patch),
                cancellation_checker=lambda: self._is_task_cancelled(task_id),
                worker_manager=self.worker_manager,
            )
            task = self.store.update(task_id, _without_records(batch_result))
            self._upload_progress(
                task=task,
                patch={
                    "event_type": "batch_finished",
                    "message": "Wuying batch finished",
                    "status": task.get("status"),
                    "finished_at": task.get("finished_at"),
                },
            )
        except Exception as exc:
            logger.exception("Crawler task failed: task_id=%s", task_id)
            task = self.store.update(
                task_id,
                {
                    "status": "failed",
                    "finished_at": _utc_now(),
                    "error": str(exc),
                },
            )
            self._upload_progress(
                task=task,
                patch={
                    "event_type": "batch_failed",
                    "message": "Wuying batch failed",
                    "status": "failed",
                    "finished_at": task.get("finished_at"),
                    "error": str(exc),
                },
            )
        finally:
            if lease_acquired:
                try:
                    self.lease_manager.release_many(device_ids, owner=task_id)
                except Exception as exc:
                    logger.warning("Failed to release task device leases: task_id=%s error=%s", task_id, exc)
            self._release_reservation(device_ids)

        self._schedule_callback_upload(task_id)

    def _handle_progress(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        store_patch = _progress_store_patch(patch)
        task = self.store.update(task_id, store_patch) if store_patch else self.store.get(task_id)
        self._upload_progress(task=task, patch=patch)
        return task

    def _upload_progress(self, *, task: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        progress_url = _resolve_progress_url(task)
        if not progress_url:
            return {"status": "skipped", "reason": "missing progress_url"}
        progress_api_key = _resolve_progress_api_key(task)
        if not progress_api_key:
            return {"status": "skipped", "reason": "missing progress_api_key"}

        payload = _build_progress_payload(task=task, patch=patch)
        try:
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                response = client.post(
                    progress_url,
                    headers={"x-api-key": progress_api_key, "content-type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
            logger.debug(
                "Progress uploaded: task_id=%s event_type=%s status=%s",
                task.get("task_id"),
                payload.get("event_type"),
                response.status_code,
            )
            return {"status": "succeeded", "http_status": response.status_code}
        except Exception as exc:
            logger.warning(
                "Progress upload failed: task_id=%s event_type=%s error=%s",
                task.get("task_id"),
                payload.get("event_type"),
                exc,
            )
            return {"status": "failed", "error": str(exc)}

    def _schedule_callback_upload(self, task_id: str) -> None:
        try:
            self.store.update(task_id, {"callback": {"status": "uploading"}})
        except Exception as exc:
            logger.warning("Failed to mark callback as uploading: task_id=%s error=%s", task_id, exc)

        executor = self._callback_executor
        if executor is None:
            callback_info = self._upload_callback(self.store.get(task_id))
            self.store.update(task_id, {"callback": callback_info})
            return

        future = executor.submit(self._execute_callback_upload, task_id)
        with self._reservation_lock:
            self._callback_futures.add(future)
        future.add_done_callback(self._forget_callback_future)

    def _execute_callback_upload(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
            callback_info = self._upload_callback(task)
            self.store.update(task_id, {"callback": callback_info})
        except Exception as exc:
            logger.warning("Callback upload worker failed: task_id=%s error=%s", task_id, exc)
            try:
                self.store.update(task_id, {"callback": {"status": "failed", "error": str(exc)}})
            except Exception:
                logger.debug("Failed to persist callback worker failure: task_id=%s", task_id, exc_info=True)

    def _forget_callback_future(self, future: Future[None]) -> None:
        with self._reservation_lock:
            self._callback_futures.discard(future)

    def _upload_callback(self, task: dict[str, Any]) -> dict[str, Any]:
        if str(task.get("status") or "") == "cancelled":
            return {"status": "skipped", "reason": "task cancelled"}

        env = dict(task.get("env") or {})
        if _is_presales_task(task):
            callback_url = _first_non_empty(env.get("callback_url"), env.get("callbackUrl"))
            callback_api_key = _first_non_empty(env.get("callback_api_key"), env.get("callbackApiKey"))
        else:
            callback_url = _first_non_empty(
                env.get("callback_url"),
                env.get("callbackUrl"),
                os.getenv("CRAWLER_CALLBACK_URL"),
            )
            callback_api_key = _first_non_empty(
                env.get("callback_api_key"),
                env.get("callbackApiKey"),
                os.getenv("CRAWLER_CALLBACK_API_KEY"),
            )
        if not callback_url:
            return {"status": "skipped", "reason": "missing callback_url"}
        if not callback_api_key:
            return {"status": "skipped", "reason": "missing callback_api_key"}

        task_records = self.store.read_records(str(task["task_id"]))
        if not task_records:
            return {"status": "skipped", "reason": "no records"}

        callback_files, callback_record_count = _build_callback_files(
            prompt_files=task.get("prompt_files"),
            records=task_records,
        )
        if not callback_files:
            return {"status": "skipped", "reason": "no callback files"}

        form_data = {
            "run_id": _first_non_empty(env.get("run_id"), task["task_id"]),
            "task_id": _first_non_empty(env.get("task_id"), task["task_id"]),
            "crawler_task_id": _first_non_empty(task.get("task_id")),
            "trace_id": _first_non_empty(task.get("trace_id"), task.get("task_id")),
            "source_type": _first_non_empty(env.get("source_type"), env.get("sourceType")),
            "business_id": _first_non_empty(env.get("business_id"), env.get("businessId")),
            "diagnostic_id": _first_non_empty(env.get("diagnostic_id"), env.get("diagnosticId")),
            "input_id": _first_non_empty(env.get("input_id"), env.get("inputId")),
            "user_id": _first_non_empty(env.get("user_id")),
            "platform_id": _first_non_empty(
                env.get("platform_id"),
                task_records[0].get("platform_id"),
            ),
            "product_id": _first_non_empty(env.get("product_id")),
            "keyword_id": _first_non_empty(env.get("keyword_id")),
            "monitor_date": _first_non_empty(env.get("monitor_date")),
            "file_count": str(len(callback_files)),
        }
        form_data = {key: value for key, value in form_data.items() if value}

        try:
            with httpx.Client(timeout=max(1, self.callback_timeout_seconds), trust_env=False) as client:
                response = client.post(
                    callback_url,
                    headers={"x-api-key": callback_api_key},
                    data=form_data,
                    files=callback_files,
                )
                response.raise_for_status()
            logger.info("Callback uploaded successfully: task_id=%s status=%s", task["task_id"], response.status_code)
            return {
                "status": "succeeded",
                "records_path": task.get("records_path"),
                "prompt_files": task.get("prompt_files", []),
                "http_status": response.status_code,
                "response_text": response.text,
                "file_count": len(callback_files),
                "record_count": callback_record_count,
            }
        except Exception as exc:
            logger.warning("Callback upload failed: task_id=%s error=%s", task["task_id"], exc)
            return {
                "status": "failed",
                "records_path": task.get("records_path"),
                "prompt_files": task.get("prompt_files", []),
                "error": str(exc),
                "file_count": len(callback_files),
                "record_count": callback_record_count,
            }

    def get_worker_statuses(self) -> list[dict[str, object]]:
        return self.worker_manager.statuses()

    def restart_worker(self, device_id: str) -> dict[str, object]:
        handle = self.worker_manager.restart_worker(device_id)
        return handle.to_dict()


def _build_task_id() -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    return f"wuying-{stamp}-{uuid4().hex[:8]}"


def _resolve_progress_url(task: dict[str, Any]) -> str | None:
    env = dict(task.get("env") or {})
    if _is_presales_task(task):
        return _first_non_empty(
            env.get("progress_url"),
            env.get("progressUrl"),
            env.get("callback_progress_url"),
            env.get("callbackProgressUrl"),
        )

    explicit = _first_non_empty(
        env.get("progress_url"),
        env.get("progressUrl"),
        env.get("callback_progress_url"),
        env.get("callbackProgressUrl"),
        os.getenv("CRAWLER_PROGRESS_URL"),
    )
    if explicit:
        return explicit

    callback_url = _first_non_empty(
        env.get("callback_url"),
        env.get("callbackUrl"),
        os.getenv("CRAWLER_CALLBACK_URL"),
    )
    if not callback_url:
        return None
    if callback_url.endswith("/uploads"):
        return f"{callback_url[:-len('/uploads')]}/progress"
    return None


def _resolve_progress_api_key(task: dict[str, Any]) -> str | None:
    env = dict(task.get("env") or {})
    if _is_presales_task(task):
        return _first_non_empty(
            env.get("progress_api_key"),
            env.get("progressApiKey"),
            env.get("callback_api_key"),
            env.get("callbackApiKey"),
        )
    return _first_non_empty(
        env.get("progress_api_key"),
        env.get("progressApiKey"),
        env.get("callback_api_key"),
        env.get("callbackApiKey"),
        os.getenv("CRAWLER_PROGRESS_API_KEY"),
        os.getenv("CRAWLER_CALLBACK_API_KEY"),
    )


def _build_progress_payload(*, task: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    env = dict(task.get("env") or {})
    current_platform = _api_platform_id_for_value(
        _first_non_empty(
            patch.get("current_platform"),
            task.get("current_platform"),
        )
    )
    payload: dict[str, Any] = {
        "run_id": _first_non_empty(env.get("run_id"), task.get("task_id")),
        "task_id": _first_non_empty(env.get("task_id"), task.get("task_id")),
        "crawler_task_id": task.get("task_id"),
        "trace_id": task.get("trace_id") or task.get("task_id"),
        "source_type": _first_non_empty(env.get("source_type"), env.get("sourceType")),
        "business_id": _first_non_empty(env.get("business_id"), env.get("businessId")),
        "diagnostic_id": _first_non_empty(env.get("diagnostic_id"), env.get("diagnosticId")),
        "input_id": _first_non_empty(env.get("input_id"), env.get("inputId")),
        "event_type": patch.get("event_type") or "progress",
        "message": patch.get("message") or "",
        "status": patch.get("status") or task.get("status"),
        "platform_ids": task.get("platform_ids", []),
        "device_ids": task.get("device_ids", []),
        "current_platform": current_platform,
        "current_platform_internal": _first_non_empty(patch.get("current_platform"), task.get("current_platform")),
        "current_repeat_index": patch.get("current_repeat_index", task.get("current_repeat_index")),
        "current_prompt_index": patch.get("current_prompt_index", task.get("current_prompt_index")),
        "current_prompt": patch.get("current_prompt", task.get("current_prompt")),
        "expected_batches": task.get("expected_batches"),
        "finished_batches": patch.get("finished_batches", task.get("finished_batches", 0)),
        "failed_batches": patch.get("failed_batches", task.get("failed_batches", 0)),
        "expected_records": task.get("expected_records"),
        "finished_records": patch.get("finished_records", task.get("finished_records", 0)),
        "failed_records": patch.get("failed_records", task.get("failed_records", 0)),
        "started_at": patch.get("started_at") or task.get("started_at"),
        "finished_at": patch.get("finished_at") or task.get("finished_at"),
        "error": patch.get("error") or task.get("error"),
    }
    if "record" in patch:
        payload["record"] = _normalize_progress_record(patch["record"])
    if "records" in patch:
        records = patch["records"]
        if isinstance(records, list):
            payload["records"] = [_normalize_progress_record(item) for item in records]
    if "platform_batches" in patch:
        platform_batches = patch["platform_batches"]
        if isinstance(platform_batches, list):
            payload["platform_batches"] = [_normalize_platform_batch(item) for item in platform_batches]
    return {key: value for key, value in payload.items() if value is not None}


def _progress_store_patch(patch: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "status",
        "records_path",
        "output_file",
        "prompt_files",
        "finished_records",
        "failed_records",
        "finished_batches",
        "failed_batches",
        "error",
        "current_platform",
        "current_repeat_index",
        "current_prompt_index",
        "current_prompt",
    }
    return {key: patch[key] for key in allowed_keys if key in patch}


def _normalize_progress_record(value: object) -> object:
    if not isinstance(value, dict):
        return value
    record = dict(value)
    platform = _first_non_empty(record.get("platform"))
    record["platform_id"] = _api_platform_id_for_value(_first_non_empty(record.get("platform_id"), platform))
    if "query" not in record and "prompt" in record:
        record["query"] = record.get("prompt")
    return record


def _normalize_platform_batch(value: object) -> object:
    if not isinstance(value, dict):
        return value
    item = dict(value)
    platform = _first_non_empty(item.get("platform"))
    item["platform_id"] = _api_platform_id_for_value(_first_non_empty(item.get("platform_id"), platform))
    return item


def _api_platform_id_for_value(value: str | None) -> str | None:
    if not value:
        return None
    if value in PLATFORM_ID_TO_INTERNAL_PLATFORM:
        return value
    return api_platform_id_for_internal(value)


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _is_presales_task(task: dict[str, Any]) -> bool:
    env = dict(task.get("env") or {})
    source_type = _first_non_empty(env.get("source_type"), env.get("sourceType"))
    return source_type == "presales_diagnostic"


def _build_callback_files(
    *,
    prompt_files: object,
    records: list[dict[str, Any]],
) -> tuple[list[tuple[str, tuple[str, bytes, str]]], int]:
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    record_count = 0

    if isinstance(prompt_files, list):
        for item in prompt_files:
            if not isinstance(item, dict):
                continue
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            payload_bytes = path.read_bytes()
            try:
                payload = json.loads(payload_bytes.decode("utf-8"))
                if isinstance(payload, list):
                    record_count += len(payload)
                elif isinstance(payload, dict):
                    records_payload = payload.get("records")
                    if isinstance(records_payload, list):
                        record_count += len(records_payload)
            except Exception:
                pass
            files.append(("files", (path.name, payload_bytes, "application/json")))

    if files:
        return files, record_count

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for record in records:
        platform = str(record.get("platform") or "").strip()
        prompt = str(record.get("prompt") or record.get("query") or "").strip()
        prompt_index = _coerce_int(record.get("prompt_index"), default=0)
        if not platform:
            platform = "unknown"
        if not prompt:
            prompt = f"prompt-{prompt_index:03d}"
        grouped.setdefault((platform, prompt_index, prompt), []).append(record)

    for (platform, prompt_index, prompt), records in sorted(
        grouped.items(),
        key=lambda item: (item[0][1], item[0][0], item[0][2]),
    ):
        filename = f"callback-{_safe_filename_part(platform)[:40]}-p{prompt_index:03d}-{_safe_filename_part(prompt)[:80]}.json"
        payload = {
            "platform_id": f"wuying-{platform}",
            "platform": platform,
            "query": prompt,
            "prompt": prompt,
            "prompt_index": prompt_index,
            "record_count": len(records),
            "records": records,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        files.append(("files", (filename, payload_bytes, "application/json")))
        record_count += len(records)

    return files, record_count


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "unknown"


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _without_records(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    compact.pop("records", None)
    return compact


__all__ = [
    "BatchCrawlerTaskRequest",
    "CrawlerTaskRequest",
    "CrawlerTaskService",
    "INTERNAL_PLATFORM_TO_API_PLATFORM",
    "PLATFORM_ID_TO_INTERNAL_PLATFORM",
    "TaskConflictError",
    "TaskStore",
    "api_platform_id_for_internal",
    "normalize_platform_inputs",
    "validate_platform_id",
]
