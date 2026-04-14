from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from wuying.application.geo_watcher_payload import build_geo_watcher_records
from wuying.application.runner import pick_default_instance, run_platform_once
from wuying.config import AppSettings

logger = logging.getLogger(__name__)


PLATFORM_ID_TO_INTERNAL_PLATFORM: dict[str, str] = {
    "wuying-doubao": "doubao",
    "wuying-deepseek": "deepseek",
    "wuying-kimi": "kimi",
    "wuying-qianwen": "qianwen",
    "wuying-yuanbao": "yuanbao",
}

TERMINAL_STATUSES = {"succeeded", "failed", "partial_failed"}


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


class TaskStore:
    def __init__(self, root_dir: Path = Path("data/tasks")) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, request: CrawlerTaskRequest) -> dict[str, Any]:
        now = _utc_now()
        task_id = f"wuying-{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "trace_id": task_id,
            "type": request.platform_id,
            "internal_platform": PLATFORM_ID_TO_INTERNAL_PLATFORM[request.platform_id],
            "status": "queued",
            "expected_records": request.expected_records,
            "finished_records": 0,
            "failed_records": 0,
            "output_file": str(self.path_for(task_id)),
            "save_name": request.save_name,
            "prompts": request.prompts,
            "repeat": request.repeat,
            "env": request.env,
            "instance_id": request.instance_id,
            "results": [],
            "callback": None,
            "error": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
        self.write(task)
        return task

    def path_for(self, task_id: str) -> Path:
        return self.root_dir / f"{task_id}.json"

    def read(self, task_id: str) -> dict[str, Any]:
        path = self.path_for(task_id)
        if not path.exists():
            raise FileNotFoundError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, task: dict[str, Any]) -> None:
        path = self.path_for(str(task["task_id"]))
        tmp_path = path.with_suffix(".tmp")
        with self._lock:
            tmp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        task = self.read(task_id)
        task.update(changes)
        self.write(task)
        return task


class CrawlerTaskService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        store: TaskStore | None = None,
        callback_payload_dir: Path = Path("data/callback_payloads"),
    ) -> None:
        self.settings = settings
        self.store = store or TaskStore()
        self.callback_payload_dir = callback_payload_dir
        self.callback_payload_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._run_worker, name="wuying-crawler-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)

    def submit(self, request: CrawlerTaskRequest) -> dict[str, Any]:
        task = self.store.create(request)
        self._queue.put(str(task["task_id"]))
        return task

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.store.read(task_id)

    def get_results(self, task_id: str) -> dict[str, Any]:
        task = self.store.read(task_id)
        return {
            "task_id": task["task_id"],
            "trace_id": task["trace_id"],
            "type": task["type"],
            "status": task["status"],
            "results": task.get("results", []),
            "callback": task.get("callback"),
            "error": task.get("error"),
        }

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            task_id = self._queue.get()
            if task_id is None:
                return
            try:
                self._execute_task(task_id)
            except Exception:
                logger.exception("Crawler task failed unexpectedly: %s", task_id)

    def _execute_task(self, task_id: str) -> None:
        task = self.store.update(task_id, status="running", started_at=_utc_now())
        internal_platform = str(task["internal_platform"])
        instance_id = task.get("instance_id") or pick_default_instance(self.settings)

        results: list[dict[str, Any]] = []
        failed_records = 0

        for repeat_index in range(1, int(task["repeat"]) + 1):
            for prompt_index, prompt in enumerate(task["prompts"], start=1):
                try:
                    result = run_platform_once(
                        settings=self.settings,
                        platform_name=internal_platform,
                        prompt=str(prompt),
                        instance_id=str(instance_id),
                    ).to_dict()
                    result["geo_watcher"] = {
                        "platform_id": task["type"],
                        "repeat_index": repeat_index,
                        "prompt_index": prompt_index,
                    }
                    results.append(result)
                except Exception as exc:
                    failed_records += 1
                    error_text = str(exc)
                    results.append(
                        {
                            "platform": internal_platform,
                            "prompt": prompt,
                            "error": error_text,
                            "traceback": traceback.format_exc(),
                            "geo_watcher": {
                                "platform_id": task["type"],
                                "repeat_index": repeat_index,
                                "prompt_index": prompt_index,
                            },
                        }
                    )

                task = self.store.update(
                    task_id,
                    results=results,
                    finished_records=len(results),
                    failed_records=failed_records,
                    error=error_text if failed_records else None,
                )

        callback = self._write_and_upload_callback_payload(task_id=task_id, task=task, results=results)
        status = _final_status(total=len(results), failed=failed_records)
        self.store.update(
            task_id,
            status=status,
            failed_records=failed_records,
            callback=callback,
            finished_at=_utc_now(),
        )

    def _write_and_upload_callback_payload(
        self,
        *,
        task_id: str,
        task: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        platform_id = str(task["type"])
        for result in results:
            if result.get("error"):
                continue
            records.extend(build_geo_watcher_records(raw_result=result, platform_id=platform_id))

        payload_path = self.callback_payload_dir / f"{task_id}.json"
        payload_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if not records:
            return {"status": "skipped", "reason": "no successful records", "payload_path": str(payload_path)}

        env = task.get("env") if isinstance(task.get("env"), dict) else {}
        callback_url = _first_string(env.get("callback_url"), env.get("callbackUrl")) or _optional_env(
            "CRAWLER_CALLBACK_URL"
        )
        callback_api_key = _first_string(env.get("callback_api_key"), env.get("callbackApiKey")) or _optional_env(
            "CRAWLER_CALLBACK_API_KEY"
        )
        if not callback_url:
            return {"status": "skipped", "reason": "missing callback_url", "payload_path": str(payload_path)}
        if not callback_api_key:
            return {"status": "skipped", "reason": "missing callback_api_key", "payload_path": str(payload_path)}

        form_data = {
            "run_id": str(env.get("run_id", "")),
            "task_id": str(env.get("task_id", "")),
            "user_id": str(env.get("user_id", "")),
            "platform_id": str(env.get("platform_id", platform_id)),
            "product_id": str(env.get("product_id", "")),
            "keyword_id": str(env.get("keyword_id", "")),
            "monitor_date": str(env.get("monitor_date", "")),
        }

        try:
            with httpx.Client(timeout=30) as client:
                with payload_path.open("rb") as payload_file:
                    response = client.post(
                        callback_url,
                        headers={"x-api-key": callback_api_key},
                        data=form_data,
                        files={"files": (payload_path.name, payload_file, "application/json")},
                    )
            callback_status = "uploaded" if response.status_code < 400 else "failed"
            return {
                "status": callback_status,
                "payload_path": str(payload_path),
                "status_code": response.status_code,
                "response": _response_text(response),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "payload_path": str(payload_path),
                "error": str(exc),
            }


def validate_platform_id(platform_id: str) -> str:
    if platform_id not in PLATFORM_ID_TO_INTERNAL_PLATFORM:
        available = ", ".join(sorted(PLATFORM_ID_TO_INTERNAL_PLATFORM))
        raise ValueError(f"Unsupported platform_id: {platform_id}. Available: {available}")
    return platform_id


def _final_status(*, total: int, failed: int) -> str:
    if total == 0 or failed == total:
        return "failed"
    if failed:
        return "partial_failed"
    return "succeeded"


def _optional_env(name: str) -> str | None:
    import os

    value = os.getenv(name, "").strip()
    return value or None


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _response_text(response: httpx.Response) -> str:
    text = response.text.strip()
    return text[:2000]


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


__all__ = [
    "CrawlerTaskRequest",
    "CrawlerTaskService",
    "PLATFORM_ID_TO_INTERNAL_PLATFORM",
    "TERMINAL_STATUSES",
    "TaskStore",
    "validate_platform_id",
]
