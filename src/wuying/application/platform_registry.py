from __future__ import annotations

from dataclasses import dataclass

from wuying.application.workflows.base import ChatAppWorkflow
from wuying.application.workflows.deepseek import DeepseekWorkflow
from wuying.application.workflows.doubao import DoubaoWorkflow
from wuying.application.workflows.kimi import KimiWorkflow
from wuying.application.workflows.qianwen import QianwenWorkflow
from wuying.config import AppSettings


@dataclass(frozen=True, slots=True)
class PlatformDefinition:
    name: str
    workflow_class: type[ChatAppWorkflow]
    description: str


PLATFORM_REGISTRY: dict[str, PlatformDefinition] = {
    "doubao": PlatformDefinition(
        name="doubao",
        workflow_class=DoubaoWorkflow,
        description="Doubao app automation",
    ),
    "deepseek": PlatformDefinition(
        name="deepseek",
        workflow_class=DeepseekWorkflow,
        description="DeepSeek app automation",
    ),
    "kimi": PlatformDefinition(
        name="kimi",
        workflow_class=KimiWorkflow,
        description="Kimi app automation",
    ),
    "qianwen": PlatformDefinition(
        name="qianwen",
        workflow_class=QianwenWorkflow,
        description="Qianwen app automation",
    ),
}


def get_platform_definition(name: str) -> PlatformDefinition:
    key = name.strip().lower()
    try:
        return PLATFORM_REGISTRY[key]
    except KeyError as exc:
        available = ", ".join(sorted(PLATFORM_REGISTRY))
        raise ValueError(f"Unsupported platform: {name}. Available: {available}") from exc


def build_workflow(settings: AppSettings, platform_name: str) -> ChatAppWorkflow:
    definition = get_platform_definition(platform_name)
    return definition.workflow_class(settings)


def available_platform_names() -> list[str]:
    return sorted(PLATFORM_REGISTRY)
