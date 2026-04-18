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
    COPY_BUTTON_WAIT_SECONDS = 90
    COPY_BUTTON_POLL_INTERVAL_SECONDS = 0.35
    COPY_BUTTON_SCROLL_INTERVAL_SECONDS = 1.0
    CLIPBOARD_UPDATE_WAIT_SECONDS = 2.0

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.deepseek)

    def _ensure_chat_input_ready(self, driver: U2Driver) -> None:
        super()._ensure_chat_input_ready(driver)
        self._ensure_search_only_mode(driver)

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        return self._build_references_payload()

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        copied = self._wait_for_copy_button_and_read_clipboard(driver, prompt=prompt)
        if copied:
            return copied

        logger.warning("DeepSeek copy button path failed; falling back to visible UI text.")
        return response

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

    def _wait_for_copy_button_and_read_clipboard(self, driver: U2Driver, *, prompt: str) -> str:
        sentinel = f"__WUYING_DEEPSEEK_CLIPBOARD_{time.time_ns()}__"
        try:
            driver.device.clipboard = sentinel
        except Exception as exc:
            logger.warning("Failed to seed DeepSeek clipboard before copy: %s", exc)
            return ""

        logger.info("Waiting for DeepSeek answer copy button.")
        deadline = time.monotonic() + self.COPY_BUTTON_WAIT_SECONDS
        last_scroll_ts = 0.0
        while time.monotonic() < deadline:
            root = driver.dump_hierarchy_root()
            bounds = self._find_completed_response_copy_bounds(root)
            if bounds is not None:
                left, top, right, bottom = bounds
                self.adb.input_tap(
                    driver.serial,
                    x=(left + right) // 2,
                    y=(top + bottom) // 2,
                )
                copied = self._read_valid_clipboard(driver, sentinel=sentinel, prompt=prompt)
                if copied:
                    return copied

            now = time.monotonic()
            if now - last_scroll_ts >= self.COPY_BUTTON_SCROLL_INTERVAL_SECONDS:
                self._scroll_towards_answer_bottom(driver)
                last_scroll_ts = now
            time.sleep(self.COPY_BUTTON_POLL_INTERVAL_SECONDS)

        return ""

    def _read_valid_clipboard(self, driver: U2Driver, *, sentinel: str, prompt: str) -> str:
        deadline = time.monotonic() + self.CLIPBOARD_UPDATE_WAIT_SECONDS
        prompt_norm = self._normalize_text(prompt)
        while time.monotonic() < deadline:
            try:
                copied = driver.device.clipboard
            except Exception:
                return ""

            if isinstance(copied, str):
                copied = copied.strip()
                if (
                    copied
                    and copied != sentinel
                    and self._normalize_text(copied) != prompt_norm
                    and not U2Driver._looks_like_loading_response(copied)
                ):
                    return copied
            time.sleep(0.12)
        return ""

    def _find_completed_response_copy_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if not self._is_copy_button_label(self._node_label(node)):
                continue

            for bounds in (
                self._nearest_clickable_bounds(node, parent_map),
                U2Driver._parse_bounds(node.attrib.get("bounds", "")),
            ):
                if bounds is None or not self._is_reasonable_action_button_bounds(bounds):
                    continue
                _, top, _, bottom = bounds
                candidates.append((top + bottom, bounds))
                break

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _scroll_towards_answer_bottom(self, driver: U2Driver) -> None:
        try:
            if self._dismiss_keyboard_if_visible(driver):
                return
            driver.swipe_up(start_ratio=0.78, end_ratio=0.34, x_ratio=0.5)
        except Exception as exc:
            logger.debug("DeepSeek answer-bottom scroll failed: %s", exc)

    def _dismiss_keyboard_if_visible(self, driver: U2Driver) -> bool:
        try:
            root = driver.dump_hierarchy_root()
        except Exception:
            return False

        if not self._keyboard_visible(root):
            return False

        logger.info("DeepSeek keyboard is visible during answer wait; closing keyboard before scrolling.")
        try:
            self.adb.shell(driver.serial, "input", "keyevent", "4", timeout=5)
            time.sleep(0.25)
            return True
        except Exception as exc:
            logger.debug("Failed to close DeepSeek keyboard before scrolling: %s", exc)
            return False

    @staticmethod
    def _keyboard_visible(root: ET.Element) -> bool:
        for node in root.iter("node"):
            package_name = (node.attrib.get("package") or "").lower()
            class_name = (node.attrib.get("class") or "").lower()
            resource_id = (node.attrib.get("resource-id") or "").lower()
            if "inputmethod" in package_name or "keyboard" in package_name:
                return True
            if "inputmethod" in class_name or "keyboard" in class_name:
                return True
            if "inputmethod" in resource_id or "keyboard" in resource_id:
                return True
        return False

    @classmethod
    def _node_label(cls, node: ET.Element) -> str:
        text = cls._normalize_text(node.attrib.get("text", ""))
        if text:
            return text
        return cls._normalize_text(node.attrib.get("content-desc", ""))

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _nearest_clickable_bounds(
        node: ET.Element,
        parent_map: dict[ET.Element, ET.Element],
    ) -> tuple[int, int, int, int] | None:
        current: ET.Element | None = node
        while current is not None:
            if current.attrib.get("clickable") == "true":
                bounds = U2Driver._parse_bounds(current.attrib.get("bounds", ""))
                if bounds is not None:
                    return bounds
            current = parent_map.get(current)
        return None

    @staticmethod
    def _is_reasonable_action_button_bounds(bounds: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = bounds
        width = right - left
        height = bottom - top
        return 12 <= width <= 150 and 12 <= height <= 150

    @classmethod
    def _is_copy_button_label(cls, label: str) -> bool:
        normalized = cls._normalize_text(label)
        if normalized == "复制":
            return True
        if normalized.startswith("复制") and normalized not in {"复制全部", "复制链接"}:
            return True
        return False


__all__ = ["DeepseekWorkflow"]
