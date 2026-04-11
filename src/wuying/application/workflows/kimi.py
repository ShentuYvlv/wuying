from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.compose_chat import ComposeChatWorkflow
from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError

logger = logging.getLogger(__name__)


class KimiWorkflow(ComposeChatWorkflow):
    platform_name = "kimi"
    COPY_BUTTON_WAIT_SECONDS = 90
    COPY_BUTTON_POLL_INTERVAL_SECONDS = 0.35
    CLIPBOARD_UPDATE_WAIT_SECONDS = 2.0

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.kimi)

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        copied = self._wait_for_copy_button_and_read_clipboard(driver, prompt=prompt)
        if copied:
            return copied

        logger.warning("Kimi copy button path failed; falling back to visible UI text.")
        root = driver.dump_hierarchy_root()
        block = self._extract_response_block(root=root, prompt=prompt, first_segment=response)
        return block or response

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        return self._build_references_payload()

    def _set_prompt_text(self, driver: U2Driver, *, prompt: str) -> None:
        target = self._find_kimi_input_by_label(driver)
        if target is None:
            target = self._find_bottom_edit_text_after_tap(driver)
        if target is None:
            super()._set_prompt_text(driver, prompt=prompt)
            return

        target.click()
        try:
            target.clear_text()
        except Exception:
            pass
        target.set_text(prompt)

    def _find_kimi_input_by_label(self, driver: U2Driver):
        for selector in self.app.selectors.input_selectors:
            is_class_only = (
                selector.class_name
                and not selector.resource_id
                and not selector.text
                and not selector.text_contains
                and not selector.description
                and not selector.description_contains
            )
            if is_class_only:
                continue
            found = driver.find_first([selector])
            if found is not None:
                found.click()
                time.sleep(0.2)
                focused = self._focused_edit_text(driver)
                if focused is not None:
                    return focused
        return None

    def _find_bottom_edit_text_after_tap(self, driver: U2Driver):
        root = driver.dump_hierarchy_root()
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.EditText":
                continue

            label = self._node_label(node)
            if "搜索网页" in label or label == "搜索":
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue

            left, top, right, bottom = bounds
            if right - left <= 80 or bottom - top <= 20:
                continue

            score = bottom
            if "尽管问" in label or "发消息" in label or "按住说话" in label:
                score += 10_000
            candidates.append((score, bounds))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        left, top, right, bottom = candidates[0][1]
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.2)

        try:
            return self._focused_edit_text(driver)
        except Exception:
            return None

    @staticmethod
    def _focused_edit_text(driver: U2Driver):
        try:
            focused = driver.device(className="android.widget.EditText", focused=True)
            if bool(focused.exists):
                return focused
        except Exception:
            return None
        return None

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        prompt_input_bounds = self._find_prompt_input_bounds(driver.dump_hierarchy_root(), prompt=prompt)
        if prompt_input_bounds is None:
            raise U2DriverError("Kimi prompt was not written into the chat input; stop before clicking function chips.")

        try:
            driver.click(self.app.selectors.send_selectors, timeout_seconds=self.SEND_SELECTOR_TIMEOUT_SECONDS)
            return
        except U2DriverError:
            pass

        root = driver.dump_hierarchy_root()
        bounds = self._find_kimi_send_button_bounds(root, input_bounds=prompt_input_bounds)
        if bounds is None:
            raise U2DriverError("Kimi send button not found after prompt input; stop before clicking fallback buttons.")

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.2)

    def _find_prompt_input_bounds(self, root: ET.Element, *, prompt: str) -> tuple[int, int, int, int] | None:
        prompt_norm = self._normalize_text(prompt)
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            if not text or prompt_norm not in text:
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue

            _, top, _, bottom = bounds
            score = bottom
            if node.attrib.get("class") == "android.widget.EditText":
                score += 10_000
            candidates.append((score, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _find_kimi_send_button_bounds(
        self,
        root: ET.Element,
        *,
        input_bounds: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int] | None:
        _, input_top, input_right, input_bottom = input_bounds
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None or not self._is_reasonable_action_button_bounds(bounds):
                continue

            left, top, right, bottom = bounds
            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            if center_x < input_right - 160:
                continue
            if center_y < input_top - 60 or center_y > input_bottom + 100:
                continue

            label = self._node_label(node)
            if label in {"网站", "Agent", "PPT", "Kimi Claw", "深度研究"}:
                continue
            if any(token in label for token in ("新会话", "返回", "导航", "菜单")):
                continue

            candidates.append((center_x, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _extract_response_block(
        self,
        *,
        root: ET.Element,
        prompt: str,
        first_segment: str,
    ) -> str:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        prompt_norm = self._normalize_text(prompt)
        response_norm = self._normalize_text(first_segment)
        if not response_norm:
            return ""

        target_node: ET.Element | None = None
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            if not text:
                continue
            if text == response_norm or response_norm in text:
                target_node = node
                break

        if target_node is None:
            return ""

        current = parent_map.get(target_node)
        while current is not None:
            texts = self._collect_descendant_texts(current)
            if response_norm in texts and prompt_norm not in texts and len(texts) > 1:
                return "\n".join(texts)
            current = parent_map.get(current)

        return ""

    def _collect_descendant_texts(self, node: ET.Element) -> list[str]:
        texts: list[str] = []
        seen: set[str] = set()
        for child in node.iter("node"):
            if child.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(child.attrib.get("text", ""))
            if not text or text in seen:
                continue
            if self._is_non_response_text(text):
                continue
            texts.append(text)
            seen.add(text)
        return texts

    def _pick_best_visible_response(self, texts: list[str], *, prompt: str) -> str:
        prompt_norm = self._normalize_text(prompt)
        candidates = []
        for raw in texts:
            text = self._normalize_text(raw)
            if not text or text == prompt_norm or self._is_non_response_text(text):
                continue
            if self._looks_like_search_preamble(text):
                continue
            candidates.append(text)
        if not candidates:
            return ""
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    def _wait_for_copy_button_and_read_clipboard(self, driver: U2Driver, *, prompt: str) -> str:
        sentinel = f"__WUYING_KIMI_CLIPBOARD_{time.time_ns()}__"
        try:
            driver.device.clipboard = sentinel
        except Exception as exc:
            logger.warning("Failed to seed Kimi clipboard before copy: %s", exc)
            return ""

        logger.info("Waiting for Kimi answer copy button.")
        deadline = time.monotonic() + self.COPY_BUTTON_WAIT_SECONDS
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

            time.sleep(self.COPY_BUTTON_POLL_INTERVAL_SECONDS)

        return ""

    def _read_valid_clipboard(self, driver: U2Driver, *, sentinel: str, prompt: str) -> str:
        deadline = time.monotonic() + self.CLIPBOARD_UPDATE_WAIT_SECONDS
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
                    and self._normalize_text(copied) != self._normalize_text(prompt)
                    and not self._looks_like_search_preamble(copied)
                ):
                    return copied
            time.sleep(0.12)
        return ""

    def _find_completed_response_copy_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        return self._find_labeled_copy_button_bounds(root, parent_map)

    def _find_labeled_copy_button_bounds(
        self,
        root: ET.Element,
        parent_map: dict[ET.Element, ET.Element],
    ) -> tuple[int, int, int, int] | None:
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

    @classmethod
    def _node_label(cls, node: ET.Element) -> str:
        text = cls._normalize_text(node.attrib.get("text", ""))
        if text:
            return text
        return cls._normalize_text(node.attrib.get("content-desc", ""))

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _is_non_response_text(value: str) -> bool:
        if not value:
            return True
        return value in {
            "Kimi",
            "K2.5 快速",
            "Agent",
            "网站",
            "PPT",
            "Kimi Claw",
            "深度研究",
            "尽管问，带图也行",
            "内容由 AI 生成",
            "引用",
        }

    @staticmethod
    def _looks_like_search_preamble(value: str) -> bool:
        if not value:
            return False
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        normalized = " ".join(lines)
        line_count = len(lines)
        if "我来帮您搜索" in normalized and line_count <= 4:
            return True
        if "正在搜索网页" in normalized and line_count <= 4:
            return True
        if "搜索网页" in normalized and line_count <= 4:
            return True
        return False


__all__ = ["KimiWorkflow"]
