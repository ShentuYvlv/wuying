from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AdbEndpoint:
    instance_id: str
    host: str
    port: int
    source: str

    @property
    def serial(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(slots=True)
class SelectorSpec:
    resource_id: str | None = None
    text: str | None = None
    text_contains: str | None = None
    description: str | None = None
    description_contains: str | None = None
    class_name: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SelectorSpec":
        return cls(
            resource_id=data.get("resourceId") or data.get("resource_id"),
            text=data.get("text"),
            text_contains=data.get("textContains") or data.get("text_contains"),
            description=data.get("description"),
            description_contains=data.get("descriptionContains") or data.get("description_contains"),
            class_name=data.get("className") or data.get("class_name"),
        )

    def to_u2_kwargs(self) -> dict[str, str]:
        kwargs: dict[str, str] = {}
        if self.resource_id:
            kwargs["resourceId"] = self.resource_id
        if self.text:
            kwargs["text"] = self.text
        if self.text_contains:
            kwargs["textContains"] = self.text_contains
        if self.description:
            kwargs["description"] = self.description
        if self.description_contains:
            kwargs["descriptionContains"] = self.description_contains
        if self.class_name:
            kwargs["className"] = self.class_name
        return kwargs

    def describe(self) -> str:
        parts = [f"{key}={value}" for key, value in asdict(self).items() if value]
        return ", ".join(parts) or "<empty selector>"


@dataclass(slots=True)
class DoubaoSelectors:
    enter_chat_selectors: list[SelectorSpec]
    switch_to_text_input_selectors: list[SelectorSpec]
    input_selectors: list[SelectorSpec]
    send_selectors: list[SelectorSpec]
    response_selectors: list[SelectorSpec]


@dataclass(slots=True)
class DoubaoRunResult:
    instance_id: str
    prompt: str
    response: str
    adb_serial: str
    output_path: str
    started_at: str
    finished_at: str

    @classmethod
    def build(
        cls,
        *,
        instance_id: str,
        prompt: str,
        response: str,
        adb_serial: str,
        output_path: Path,
        started_at: datetime,
        finished_at: datetime,
    ) -> "DoubaoRunResult":
        return cls(
            instance_id=instance_id,
            prompt=prompt,
            response=response,
            adb_serial=adb_serial,
            output_path=str(output_path),
            started_at=started_at.astimezone(UTC).isoformat(),
            finished_at=finished_at.astimezone(UTC).isoformat(),
        )
