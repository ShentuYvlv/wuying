from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BatchTaskRequest:
    platforms: list[str]
    prompts: list[str]
    repeat: int
    save_name: str | None
    env: dict[str, Any]
    device_ids: list[str] | None = None
    legacy_instance_id: str | None = None
    default_to_all_pool_devices: bool = False


@dataclass(slots=True)
class DeviceRunRecord:
    device_id: str
    instance_id: str
    adb_endpoint: str | None
    platform: str
    prompt: str
    prompt_index: int
    repeat_index: int
    status: str
    started_at: str
    finished_at: str
    attempt_index: int = 1
    is_final_attempt: bool = True
    result_path: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "adb_endpoint": self.adb_endpoint,
            "platform": self.platform,
            "prompt": self.prompt,
            "prompt_index": self.prompt_index,
            "repeat_index": self.repeat_index,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempt_index": self.attempt_index,
            "is_final_attempt": self.is_final_attempt,
            "result_path": self.result_path,
            "error": self.error,
            "result": self.result,
        }


@dataclass(slots=True)
class PlatformPromptBatchRecord:
    platform: str
    prompt: str
    prompt_index: int
    repeat_index: int
    device_ids: list[str]
    status: str
    started_at: str
    finished_at: str
    output_path: str
    results: list[DeviceRunRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "prompt": self.prompt,
            "prompt_index": self.prompt_index,
            "repeat_index": self.repeat_index,
            "device_ids": self.device_ids,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_path": self.output_path,
            "results": [item.to_dict() for item in self.results],
        }


__all__ = ["BatchTaskRequest", "DeviceRunRecord", "PlatformPromptBatchRecord"]
