from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from wuying.application.batch_models import BatchTaskRequest
from wuying.application.batch_runner import resolve_batch_devices, run_batch_job
from wuying.application.device_lease import DeviceLeaseError, DeviceLeaseManager
from wuying.application.geo_watcher_payload import build_geo_watcher_records
from wuying.application.platform_registry import available_platform_names, get_platform_definition
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

    def path_for(self, task_id: str) -> Path:
        return self.root_dir / f"{task_id}.json"

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
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)


class CrawlerTaskService:
    def __init__(self, *, settings: AppSettings) -> None:
        self.settings = settings
        self.store = TaskStore()
        self.callback_payload_dir = Path("data/callback_payloads")
        self.callback_payload_dir.mkdir(parents=True, exist_ok=True)
        self.record_timeout_seconds = _get_env_int("CRAWLER_RECORD_TIMEOUT_SECONDS", 300)
        self.batch_timeout_seconds = settings.batch_timeout_seconds
        self.lease_manager = DeviceLeaseManager(
            settings.device.device_lease_dir,
            stale_after_seconds=settings.device.device_lease_ttl_seconds,
        )
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="wuying-task-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=10)

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
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "type": task["type"],
            "platforms": task.get("platforms", []),
            "platform_ids": task.get("platform_ids", []),
            "device_ids": task.get("device_ids", []),
            "selected_devices": task.get("selected_devices", []),
            "results": task.get("results", []),
            "platform_batches": task.get("platform_batches", []),
            "summary_path": task.get("summary_path"),
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
        try:
            self.lease_manager.acquire_many([device.device_id for device in devices], owner=task_id)
        except DeviceLeaseError as exc:
            raise TaskConflictError(str(exc)) from exc

        created_at = _utc_now()
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
            "output_file": str(self.store.path_for(task_id)),
            "summary_path": None,
            "save_name": batch_request.save_name,
            "prompts": batch_request.prompts,
            "repeat": batch_request.repeat,
            "env": dict(batch_request.env),
            "instance_id": getattr(raw_request, "instance_id", None),
            "device_ids": [device.device_id for device in devices],
            "selected_devices": [device.to_dict() for device in devices],
            "platforms": batch_request.platforms,
            "platform_ids": platform_ids,
            "results": [],
            "platform_batches": [],
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
        self.store.create(task_payload)
        self._queue.put(
            {
                "task_id": task_id,
                "batch_request": batch_request,
                "devices": devices,
                "platform_ids": platform_ids,
            }
        )
        return task_payload

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                self._execute_task(item)
            finally:
                self._queue.task_done()

    def _execute_task(self, item: dict[str, Any]) -> None:
        task_id = str(item["task_id"])
        batch_request = item["batch_request"]
        devices = item["devices"]
        started_at = _utc_now()
        self.store.update(
            task_id,
            {
                "status": "running",
                "started_at": started_at,
            },
        )
        try:
            batch_result = run_batch_job(
                settings=self.settings,
                task_id=task_id,
                request=batch_request,
                devices=devices,
                record_timeout_seconds=self.record_timeout_seconds,
                batch_timeout_seconds=self.batch_timeout_seconds,
                progress_callback=lambda patch: self.store.update(task_id, patch),
            )
            task = self.store.update(task_id, batch_result)
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
        finally:
            self.lease_manager.release_many([device.device_id for device in devices], owner=task_id)

        callback_info = self._upload_callback(task)
        self.store.update(task_id, {"callback": callback_info})

    def _upload_callback(self, task: dict[str, Any]) -> dict[str, Any]:
        env = dict(task.get("env") or {})
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

        successful_results = [
            result
            for result in task.get("results", [])
            if str(result.get("status") or "") == "succeeded"
        ]
        if not successful_results:
            return {"status": "skipped", "reason": "no successful records"}

        records: list[dict[str, Any]] = []
        for raw_result in successful_results:
            platform_id = _first_non_empty(
                raw_result.get("platform_id"),
                env.get("platform_id"),
                api_platform_id_for_internal(str(raw_result.get("platform") or "")),
            )
            records.extend(
                build_geo_watcher_records(
                    raw_result=raw_result,
                    platform_id=platform_id or "",
                )
            )

        if not records:
            return {"status": "skipped", "reason": "no callback records"}

        payload_path = self.callback_payload_dir / f"{task['task_id']}.json"
        payload_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

        form_data = {
            "run_id": _first_non_empty(env.get("run_id"), task["task_id"]),
            "task_id": _first_non_empty(env.get("task_id"), task["task_id"]),
            "user_id": _first_non_empty(env.get("user_id")),
            "platform_id": _first_non_empty(
                env.get("platform_id"),
                records[0].get("platform_id"),
            ),
            "product_id": _first_non_empty(env.get("product_id")),
            "keyword_id": _first_non_empty(env.get("keyword_id")),
            "monitor_date": _first_non_empty(env.get("monitor_date")),
        }
        form_data = {key: value for key, value in form_data.items() if value}

        try:
            with httpx.Client(timeout=60.0, trust_env=False) as client:
                response = client.post(
                    callback_url,
                    headers={"x-api-key": callback_api_key},
                    data=form_data,
                    files={"files": (payload_path.name, payload_path.read_bytes(), "application/json")},
                )
                response.raise_for_status()
            logger.info("Callback uploaded successfully: task_id=%s status=%s", task["task_id"], response.status_code)
            return {
                "status": "succeeded",
                "payload_path": str(payload_path),
                "http_status": response.status_code,
                "response_text": response.text,
                "record_count": len(records),
            }
        except Exception as exc:
            logger.warning("Callback upload failed: task_id=%s error=%s", task["task_id"], exc)
            return {
                "status": "failed",
                "payload_path": str(payload_path),
                "error": str(exc),
                "record_count": len(records),
            }


def _build_task_id() -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    return f"wuying-{stamp}-{uuid4().hex[:8]}"


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


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
