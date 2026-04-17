from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
class ChatAppSelectors:
    new_chat_selectors: list[SelectorSpec]
    chat_back_selectors: list[SelectorSpec]
    enter_chat_selectors: list[SelectorSpec]
    switch_to_text_input_selectors: list[SelectorSpec]
    reference_expand_selectors: list[SelectorSpec]
    input_selectors: list[SelectorSpec]
    send_selectors: list[SelectorSpec]
    response_selectors: list[SelectorSpec]


@dataclass(slots=True)
class ReferenceItem:
    title: str | None = None
    source: str | None = None
    published_at: str | None = None
    url: str | None = None
    index: int | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ReferenceItem":
        return cls(
            title=data.get("title"),
            source=data.get("source"),
            published_at=data.get("published_at") or data.get("publishedAt"),
            url=data.get("url"),
            index=data.get("index"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at,
            "url": self.url,
        }


@dataclass(slots=True)
class ReferenceData:
    summary: str | None = None
    keywords: list[str] = field(default_factory=list)
    items: list[ReferenceItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "keywords": self.keywords,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True)
class PlatformRunResult:
    platform: str
    instance_id: str
    device_id: str | None
    prompt: str
    response: str
    adb_serial: str
    output_path: str
    started_at: str
    finished_at: str
    references: ReferenceData = field(default_factory=ReferenceData)
    platform_extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "instance_id": self.instance_id,
            "device_id": self.device_id,
            "prompt": self.prompt,
            "response": self.response,
            "adb_serial": self.adb_serial,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "references": self.references.to_dict(),
            "platform_extra": self.platform_extra,
        }

    @classmethod
    def build(
        cls,
        *,
        platform: str,
        instance_id: str,
        device_id: str | None,
        prompt: str,
        response: str,
        adb_serial: str,
        output_path: Path | str,
        started_at: datetime,
        finished_at: datetime,
        extra: dict[str, Any] | None = None,
    ) -> "PlatformRunResult":
        references, platform_extra = cls._normalize_extra(extra or {})
        return cls(
            platform=platform,
            instance_id=instance_id,
            device_id=device_id,
            prompt=prompt,
            response=response,
            adb_serial=adb_serial,
            output_path=str(output_path) if output_path else "",
            started_at=started_at.astimezone(UTC).isoformat(),
            finished_at=finished_at.astimezone(UTC).isoformat(),
            references=references,
            platform_extra=platform_extra,
        )

    @staticmethod
    def _normalize_extra(extra: dict[str, Any]) -> tuple[ReferenceData, dict[str, Any]]:
        remaining = dict(extra)

        references_raw = remaining.pop("references", None)
        summary = remaining.pop("search_summary", None)
        keywords_raw = remaining.pop("reference_keywords", [])
        items_raw = remaining.pop("reference_items", [])
        titles_raw = remaining.pop("reference_titles", [])

        if isinstance(references_raw, dict):
            summary = references_raw.get("summary", summary)
            keywords_raw = references_raw.get("keywords", keywords_raw)
            items_raw = references_raw.get("items", items_raw)
            if not titles_raw:
                titles_raw = references_raw.get("titles", [])

        keywords = [
            item.strip()
            for item in keywords_raw
            if isinstance(item, str) and item.strip()
        ]

        items: list[ReferenceItem] = []
        if isinstance(items_raw, list) and items_raw:
            for raw in items_raw:
                if isinstance(raw, dict):
                    item = ReferenceItem.from_mapping(raw)
                elif isinstance(raw, str) and raw.strip():
                    item = ReferenceItem(title=raw.strip())
                else:
                    continue
                if item.title or item.source or item.published_at or item.url:
                    items.append(item)
        elif isinstance(titles_raw, list):
            for index, raw_title in enumerate(titles_raw, start=1):
                if isinstance(raw_title, str) and raw_title.strip():
                    items.append(ReferenceItem(index=index, title=raw_title.strip()))

        references = ReferenceData(
            summary=summary if isinstance(summary, str) and summary.strip() else None,
            keywords=keywords,
            items=items,
        )
        return references, remaining


DoubaoSelectors = ChatAppSelectors
DoubaoRunResult = PlatformRunResult
