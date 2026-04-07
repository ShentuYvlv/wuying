from __future__ import annotations

from wuying.application.platform_registry import build_workflow
from wuying.config import AppSettings
from wuying.models import PlatformRunResult


def pick_default_instance(settings: AppSettings) -> str:
    if not settings.instance_ids:
        raise ValueError("No instance configured. Set WUYING_INSTANCE_IDS or pass --instance-id.")
    return settings.instance_ids[0]


def run_platform_once(
    *,
    settings: AppSettings,
    platform_name: str,
    prompt: str,
    instance_id: str | None = None,
) -> PlatformRunResult:
    workflow = build_workflow(settings, platform_name)
    resolved_instance_id = instance_id or pick_default_instance(settings)
    return workflow.run_once(instance_id=resolved_instance_id, prompt=prompt)
