from __future__ import annotations

from typing import Any


MOCK_METRICS: dict[str, int] = {
    "提及率": 100,
    "前三率": 100,
    "置顶率": 0,
    "负面提及率": 0,
}

MOCK_ATTITUDE = 92


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
            **MOCK_METRICS,
            "attitude": MOCK_ATTITUDE,
            "platform_id": platform_id,
            "platform": raw_result.get("platform", ""),
            "references": raw_result.get("references", {}),
            "raw_output_path": raw_result.get("output_path", ""),
        },
    ]


__all__ = ["MOCK_ATTITUDE", "MOCK_METRICS", "build_geo_watcher_records"]
