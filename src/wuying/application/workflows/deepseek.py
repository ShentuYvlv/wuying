from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.compose_chat import ComposeChatWorkflow
from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError

logger = logging.getLogger(__name__)


class DeepseekWorkflow(ComposeChatWorkflow):
    platform_name = "deepseek"

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.deepseek)

    def _ensure_chat_input_ready(self, driver: U2Driver) -> None:
        super()._ensure_chat_input_ready(driver)
        self._ensure_search_only_mode(driver)

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        return self._build_references_payload()

    def _ensure_search_only_mode(self, driver: U2Driver) -> None:
        root = driver.dump_hierarchy_root()
        toggles = self._extract_mode_toggles(root)
        if not toggles:
            logger.info("DeepSeek mode toggles not found; skip mode enforcement")
            return

        deep_think = toggles.get("deep_think")
        smart_search = toggles.get("smart_search")

        if deep_think is not None and deep_think["checked"]:
            self.adb.input_tap(driver.serial, x=deep_think["x"], y=deep_think["y"])
            logger.info("DeepSeek mode adjusted: disable deep_think")
            time.sleep(0.2)

        if smart_search is not None and not smart_search["checked"]:
            self.adb.input_tap(driver.serial, x=smart_search["x"], y=smart_search["y"])
            logger.info("DeepSeek mode adjusted: enable smart_search")
            time.sleep(0.2)

    def _extract_mode_toggles(self, root: ET.Element) -> dict[str, dict[str, int | bool]]:
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            return {}

        left, top, right, bottom = app_bounds
        min_y = top + int((bottom - top) * 0.72)
        max_x = left + int((right - left) * 0.78)

        toggles: dict[str, dict[str, int | bool]] = {}
        for node in root.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("checkable") != "true" or attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue

            node_left, node_top, node_right, node_bottom = bounds
            width = node_right - node_left
            height = node_bottom - node_top
            if width <= 0 or height <= 0:
                continue
            if node_top < min_y or node_right > max_x:
                continue
            if width > 260 or height > 140:
                continue

            label_parts: list[str] = []
            for child in node.iter("node"):
                text = (child.attrib.get("text") or "").strip()
                if text:
                    label_parts.append(text)
            label = " ".join(label_parts)
            if "深度思考" in label:
                toggles["deep_think"] = {
                    "checked": attrs.get("checked") == "true",
                    "x": node_left + width // 2,
                    "y": node_top + height // 2,
                }
            elif "智能搜索" in label:
                toggles["smart_search"] = {
                    "checked": attrs.get("checked") == "true",
                    "x": node_left + width // 2,
                    "y": node_top + height // 2,
                }

        return toggles


__all__ = ["DeepseekWorkflow"]
