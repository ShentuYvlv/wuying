from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
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
        content = json.dumps(task, ensure_ascii=False, indent=2)
        with self._lock:
            tmp_path.write_text(content, encoding="utf-8")
            try:
                _replace_with_retry(tmp_path, path)
            except PermissionError:
                logger.debug("Atomic task file replace failed; falling back to direct write: %s", path)
                path.write_text(content, encoding="utf-8")
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

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
        record_timeout_seconds: int | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or TaskStore()
        self.callback_payload_dir = callback_payload_dir
        self.record_timeout_seconds = record_timeout_seconds
        if self.record_timeout_seconds is None:
            self.record_timeout_seconds = _int_env("CRAWLER_RECORD_TIMEOUT_SECONDS", 300)
        if self.record_timeout_seconds <= 0:
            raise ValueError("CRAWLER_RECORD_TIMEOUT_SECONDS must be greater than 0")
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
        last_error: str | None = None

        for repeat_index in range(1, int(task["repeat"]) + 1):
            for prompt_index, prompt in enumerate(task["prompts"], start=1):
                try:
                    result = _run_platform_once_with_timeout(
                        platform_name=internal_platform,
                        prompt=str(prompt),
                        instance_id=str(instance_id),
                        timeout_seconds=self.record_timeout_seconds,
                    ).to_dict()
                    result["geo_watcher"] = {
                        "platform_id": task["type"],
                        "repeat_index": repeat_index,
                        "prompt_index": prompt_index,
                    }
                    results.append(result)
                except Exception as exc:
                    failed_records += 1
                    last_error = str(exc)
                    results.append(
                        {
                            "platform": internal_platform,
                            "prompt": prompt,
                            "error": last_error,
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
                    error=last_error,
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
            logger.info("Uploading callback payload for %s to %s", task_id, callback_url)
            with httpx.Client(timeout=30, trust_env=False) as client:
                with payload_path.open("rb") as payload_file:
                    response = client.post(
                        callback_url,
                        headers={"x-api-key": callback_api_key},
                        data=form_data,
                        files={"files": (payload_path.name, payload_file, "application/json")},
                    )
            callback_status = "uploaded" if response.status_code < 400 else "failed"
            if callback_status == "uploaded":
                logger.info("Callback payload uploaded for %s: status_code=%s", task_id, response.status_code)
            else:
                logger.warning(
                    "Callback payload failed for %s: status_code=%s response=%s",
                    task_id,
                    response.status_code,
                    _response_text(response),
                )
            return {
                "status": callback_status,
                "payload_path": str(payload_path),
                "status_code": response.status_code,
                "response": _response_text(response),
            }
        except Exception as exc:
            logger.warning("Callback payload upload raised for %s: %s", task_id, exc)
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


def _run_platform_once_with_timeout(
    *,
    platform_name: str,
    prompt: str,
    instance_id: str,
    timeout_seconds: int,
):
    context = mp.get_context("spawn")
    result_path = _ipc_result_path(platform_name)
    process = context.Process(
        target=_run_platform_once_child,
        args=(str(result_path), platform_name, prompt, instance_id),
        name=f"wuying-{platform_name}-record",
    )
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        logger.warning(
            "Crawler record timed out after %ss: platform=%s, instance_id=%s",
            timeout_seconds,
            platform_name,
            instance_id,
        )
        try:
            result_path.unlink()
        except OSError:
            pass
        raise TimeoutError(
            f"Crawler record timed out after {timeout_seconds}s: "
            f"platform={platform_name}, instance_id={instance_id}"
        )

    if not result_path.exists():
        raise RuntimeError(
            f"Crawler record exited without result: platform={platform_name}, exitcode={process.exitcode}"
        )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    try:
        result_path.unlink()
    except OSError:
        pass
    if payload.get("ok"):
        return _DictBackedResult(payload["result"])

    raise RuntimeError(str(payload.get("error") or "Crawler record failed in child process"))


def _run_platform_once_child(
    result_path: str,
    platform_name: str,
    prompt: str,
    instance_id: str,
) -> None:
    try:
        from wuying.logging_utils import configure_logging

        configure_logging()
        settings = AppSettings.from_env()
        result = run_platform_once(
            settings=settings,
            platform_name=platform_name,
            prompt=prompt,
            instance_id=instance_id,
        ).to_dict()
        _write_child_result(result_path, {"ok": True, "result": result})
    except Exception:
        _write_child_result(result_path, {"ok": False, "error": traceback.format_exc()})


def _ipc_result_path(platform_name: str) -> Path:
    root_dir = Path(os.getenv("CRAWLER_TASK_IPC_DIR", "data/task_ipc"))
    root_dir.mkdir(parents=True, exist_ok=True)
    return root_dir / f"{platform_name}_{uuid4().hex}.json"


def _write_child_result(result_path: str, payload: dict[str, Any]) -> None:
    Path(result_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _DictBackedResult:
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.data


def _final_status(*, total: int, failed: int) -> str:
    if total == 0 or failed == total:
        return "failed"
    if failed:
        return "partial_failed"
    return "succeeded"


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _replace_with_retry(source: Path, target: Path) -> None:
    last_error: PermissionError | None = None
    for _ in range(5):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.1)
    assert last_error is not None
    raise last_error


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
