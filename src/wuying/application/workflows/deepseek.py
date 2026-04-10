from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.base import ChatAppWorkflow
from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError

logger = logging.getLogger(__name__)


class DeepseekWorkflow(ChatAppWorkflow):
    platform_name = "deepseek"

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.deepseek)

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        try:
            super()._send_prompt(driver, prompt=prompt)
            return
        except U2DriverError:
            pass

        self._tap_compose_trailing_action(driver)
        time.sleep(0.2)

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._click_new_chat_button(driver):
            time.sleep(0.35)
            return

        raise U2DriverError("DeepSeek new chat button not found.")

    def _ensure_chat_input_ready(self, driver: U2Driver) -> None:
        super()._ensure_chat_input_ready(driver)
        self._ensure_search_only_mode(driver)

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        return self._build_references_payload()

    def _click_new_chat_button(self, driver: U2Driver) -> bool:
        button = driver.find_first(self.app.selectors.new_chat_selectors)
        if button is not None:
            button.click()
            return True

        root = driver.dump_hierarchy_root()
        bounds = self._find_top_right_new_chat_bounds(root)
        if bounds is None:
            return False

        left, top, right, bottom = bounds
        self.adb.input_tap(
            driver.serial,
            x=(left + right) // 2,
            y=(top + bottom) // 2,
        )
        return True

    def _tap_compose_trailing_action(self, driver: U2Driver) -> None:
        root = driver.dump_hierarchy_root()
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            raise U2DriverError("DeepSeek send fallback failed: app bounds not found.")

        left, top, right, bottom = app_bounds
        min_x = left + int((right - left) * 0.72)
        min_y = top + int((bottom - top) * 0.72)

        best_center: tuple[int, int] | None = None
        best_score = -1
        for node in root.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue

            node_left, node_top, node_right, node_bottom = bounds
            width = node_right - node_left
            height = node_bottom - node_top
            if width <= 0 or height <= 0:
                continue
            if width > 180 or height > 180:
                continue

            center_x = node_left + width // 2
            center_y = node_top + height // 2
            if center_x < min_x or center_y < min_y:
                continue

            score = center_x * 2 + center_y
            if score > best_score:
                best_score = score
                best_center = (center_x, center_y)

        if best_center is None:
            raise U2DriverError("DeepSeek send fallback failed: trailing action button not found.")

        self.adb.input_tap(driver.serial, x=best_center[0], y=best_center[1])

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

    def _find_top_right_new_chat_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            return None

        _, top, right, bottom = app_bounds
        max_y = top + int((bottom - top) * 0.18)
        min_x = right - int((right - app_bounds[0]) * 0.22)

        best_bounds: tuple[int, int, int, int] | None = None
        best_score = -1
        for node in root.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue

            left, node_top, node_right, node_bottom = bounds
            width = node_right - left
            height = node_bottom - node_top
            if width <= 0 or height <= 0:
                continue
            if width > 180 or height > 180:
                continue
            if left < min_x or node_bottom > max_y:
                continue

            score = left + node_right
            if score > best_score:
                best_score = score
                best_bounds = bounds
        return best_bounds

    def _find_app_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is not None:
                return bounds
        return None


__all__ = ["DeepseekWorkflow"]
