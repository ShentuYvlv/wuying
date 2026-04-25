from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from wuying.application.device_pool import DeviceTarget
from wuying.application.device_worker import device_worker_main
from wuying.config import AppSettings

import logging


logger = logging.getLogger(__name__)


class WorkerManagerError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkerHandle:
    device: DeviceTarget
    process: mp.Process
    command_queue: mp.Queue
    result_queue: mp.Queue
    state: str = "starting"
    started_at: str | None = None
    last_error: str | None = None
    current_request_id: str | None = None
    current_platform: str | None = None
    current_prompt: str | None = None
    connection_state: str = "not_connected"
    driver_ready: bool = False
    last_finished_at: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device.device_id,
            "instance_id": self.device.instance_id,
            "adb_endpoint": self.device.adb_endpoint,
            "state": self.state,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "current_request_id": self.current_request_id,
            "current_platform": self.current_platform,
            "current_prompt": self.current_prompt,
            "connection_state": self.connection_state,
            "driver_ready": self.driver_ready,
            "ready_for_task": self.state == "idle" and self.driver_ready,
            "last_finished_at": self.last_finished_at,
        }


class WorkerManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._context = mp.get_context("spawn")
        self._handles: dict[str, WorkerHandle] = {}
        self._manager_lock = threading.Lock()

    def start_all(self, devices: list[DeviceTarget], *, strict: bool = False) -> None:
        if not devices:
            return
        errors: list[str] = []
        max_workers = max(1, len(devices))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="worker-startup") as executor:
            futures = {executor.submit(self.ensure_worker, device): device for device in devices}
            for future in as_completed(futures):
                device = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Failed to start device worker: device_id=%s error=%s", device.device_id, exc)
                    errors.append(f"{device.device_id}: {exc}")
                    continue
        if strict and errors:
            raise WorkerManagerError("Failed to start device workers: " + "; ".join(errors))

    def stop_all(self) -> None:
        with self._manager_lock:
            device_ids = list(self._handles)
        for device_id in device_ids:
            self.stop_worker(device_id)

    def ensure_worker(self, device: DeviceTarget) -> WorkerHandle:
        with self._manager_lock:
            handle = self._handles.get(device.device_id)
            if handle is None or not handle.process.is_alive():
                handle = self._spawn_worker_locked(device)
                self._handles[device.device_id] = handle
        self._ensure_worker_started(handle)
        return handle

    def restart_worker(self, device_id: str) -> WorkerHandle:
        with self._manager_lock:
            handle = self._handles.get(device_id)
            if handle is None:
                raise WorkerManagerError(f"Unknown device_id: {device_id}")
            device = handle.device
        self.stop_worker(device_id)
        return self.ensure_worker(device)

    def stop_worker(self, device_id: str) -> None:
        with self._manager_lock:
            handle = self._handles.pop(device_id, None)
        if handle is None:
            return

        try:
            handle.command_queue.put({"type": "shutdown"})
        except Exception:
            pass
        handle.process.join(timeout=5)
        if handle.process.is_alive():
            handle.process.terminate()
            handle.process.join(timeout=5)
        if handle.process.is_alive():
            handle.process.kill()
            handle.process.join(timeout=5)

    def run_on_device(
        self,
        *,
        device: DeviceTarget,
        platform: str,
        prompt: str,
        timeout_seconds: int,
        save_result: bool = False,
    ) -> dict[str, Any]:
        handle = self.ensure_worker(device)
        with handle._lock:
            if not handle.process.is_alive():
                handle = self.restart_worker(device.device_id)
                self._ensure_worker_started(handle)

            self._ensure_worker_started_locked(handle)

            request_id = uuid4().hex
            handle.current_request_id = request_id
            handle.current_platform = platform
            handle.current_prompt = prompt
            handle.state = "running"

            handle.command_queue.put(
                {
                    "type": "run",
                    "request_id": request_id,
                    "platform": platform,
                    "prompt": prompt,
                    "save_result": save_result,
                }
            )

            try:
                message = self._wait_task_result(handle, request_id=request_id, timeout_seconds=timeout_seconds)
            except TimeoutError:
                self._timeout_restart(handle, platform=platform, prompt=prompt)
                raise
            except Exception:
                self._failure_restart(handle, error=traceback.format_exc())
                raise

            handle.current_request_id = None
            handle.current_platform = None
            handle.current_prompt = None
            handle.last_finished_at = str(message.get("finished_at") or _utc_now())

            if message.get("status") == "succeeded":
                handle.state = "idle"
                handle.last_error = None
                handle.connection_state = "driver_ready"
                handle.driver_ready = True
                return dict(message["result"])

            handle.state = "failed"
            handle.last_error = str(message.get("error") or "device worker failed")
            handle.connection_state = "failed"
            handle.driver_ready = False
            self.restart_worker(device.device_id)
            raise WorkerManagerError(handle.last_error)

    def statuses(self) -> list[dict[str, object]]:
        with self._manager_lock:
            handles = list(self._handles.values())
        return [handle.to_dict() for handle in handles]

    def _wait_task_result(self, handle: WorkerHandle, *, request_id: str, timeout_seconds: int) -> dict[str, Any]:
        deadline = datetime.now(tz=UTC).timestamp() + timeout_seconds
        while datetime.now(tz=UTC).timestamp() < deadline:
            if not handle.process.is_alive():
                raise WorkerManagerError(
                    handle.last_error
                    or f"Device worker exited unexpectedly: {handle.device.device_id}"
                )
            remaining = max(0.1, deadline - datetime.now(tz=UTC).timestamp())
            try:
                message = handle.result_queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                continue

            message_type = str(message.get("type") or "")
            if message_type == "worker_ready":
                handle.state = "idle"
                handle.started_at = str(message.get("started_at") or _utc_now())
                handle.connection_state = str(message.get("connection_state") or "not_connected")
                handle.driver_ready = bool(message.get("driver_ready", False))
                continue
            if message_type == "worker_failed":
                handle.state = "failed"
                handle.last_error = str(message.get("error") or "worker startup failed")
                handle.connection_state = "failed"
                handle.driver_ready = False
                raise WorkerManagerError(handle.last_error)
            if message_type == "task_result" and message.get("request_id") == request_id:
                return message
        raise TimeoutError(
            f"Crawler record timed out after {timeout_seconds}s: "
            f"platform={handle.current_platform}, device_id={handle.device.device_id}, instance_id={handle.device.instance_id}"
        )

    def _spawn_worker_locked(self, device: DeviceTarget) -> WorkerHandle:
        command_queue: mp.Queue = self._context.Queue()
        result_queue: mp.Queue = self._context.Queue()
        process = self._context.Process(
            target=device_worker_main,
            args=(command_queue, result_queue, self.settings, device),
            name=f"wuying-device-{device.device_id}",
            daemon=True,
        )
        process.start()
        handle = WorkerHandle(
            device=device,
            process=process,
            command_queue=command_queue,
            result_queue=result_queue,
        )
        return handle

    def _ensure_worker_started(self, handle: WorkerHandle) -> None:
        with handle._lock:
            self._ensure_worker_started_locked(handle)

    def _ensure_worker_started_locked(self, handle: WorkerHandle) -> None:
        if handle.state == "idle":
            return
        if handle.state == "failed":
            raise WorkerManagerError(handle.last_error or f"Device worker failed: {handle.device.device_id}")
        if not handle.process.is_alive():
            raise WorkerManagerError(f"Device worker exited unexpectedly: {handle.device.device_id}")

        timeout_seconds = self.settings.device.worker_startup_timeout_seconds
        deadline = datetime.now(tz=UTC).timestamp() + timeout_seconds
        while datetime.now(tz=UTC).timestamp() < deadline:
            remaining = max(0.1, deadline - datetime.now(tz=UTC).timestamp())
            try:
                message = handle.result_queue.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if not handle.process.is_alive():
                    raise WorkerManagerError(f"Device worker exited unexpectedly: {handle.device.device_id}")
                continue

            if not isinstance(message, dict):
                continue

            message_type = str(message.get("type") or "")
            if message_type == "worker_ready":
                handle.state = "idle"
                handle.started_at = str(message.get("started_at") or _utc_now())
                handle.connection_state = str(message.get("connection_state") or "not_connected")
                handle.driver_ready = bool(message.get("driver_ready", False))
                return
            if message_type == "worker_failed":
                handle.state = "failed"
                handle.last_error = str(message.get("error") or "worker startup failed")
                handle.connection_state = "failed"
                handle.driver_ready = False
                self._discard_failed_handle(handle)
                raise WorkerManagerError(handle.last_error)

        self._discard_failed_handle(handle)
        raise WorkerManagerError(f"Device worker startup timed out: {handle.device.device_id}")

    def _timeout_restart(self, handle: WorkerHandle, *, platform: str, prompt: str) -> None:
        handle.state = "timeout"
        handle.last_error = (
            f"Crawler record timed out: platform={platform}, device_id={handle.device.device_id}, prompt={prompt}"
        )
        handle.connection_state = "timeout"
        handle.driver_ready = False
        self.restart_worker(handle.device.device_id)

    def _failure_restart(self, handle: WorkerHandle, *, error: str) -> None:
        handle.state = "failed"
        handle.last_error = error
        handle.connection_state = "failed"
        handle.driver_ready = False
        self.restart_worker(handle.device.device_id)

    def _discard_failed_handle(self, handle: WorkerHandle) -> None:
        with self._manager_lock:
            current = self._handles.get(handle.device.device_id)
            if current is handle:
                self._handles.pop(handle.device.device_id, None)
        try:
            handle.command_queue.put({"type": "shutdown"})
        except Exception:
            pass
        handle.process.join(timeout=2)
        if handle.process.is_alive():
            handle.process.terminate()
            handle.process.join(timeout=5)
        if handle.process.is_alive():
            handle.process.kill()
            handle.process.join(timeout=5)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


__all__ = ["WorkerHandle", "WorkerManager", "WorkerManagerError"]
