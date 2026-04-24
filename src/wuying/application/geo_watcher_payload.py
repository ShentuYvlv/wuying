from __future__ import annotations

from typing import Any


def build_geo_watcher_records(
    *,
    raw_result: dict[str, Any],
    platform_id: str,
) -> list[dict[str, Any]]:
    """Convert an internal crawler result into GEO-watcher callback records."""
    return [
        {
            "query": raw_result.get("prompt", ""),
            "response": raw_result.get("response", ""),
            "platform_id": platform_id,
            "platform": raw_result.get("platform", ""),
            "device_id": raw_result.get("device_id", ""),
            "references": raw_result.get("references", {}),
            "raw_output_path": raw_result.get("output_path", ""),
        },
    ]


__all__ = ["build_geo_watcher_records"]
