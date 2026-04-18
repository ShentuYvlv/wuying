from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.compose_chat import ComposeChatWorkflow
from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError

logger = logging.getLogger(__name__)


class YuanbaoWorkflow(ComposeChatWorkflow):
    platform_name = "yuanbao"
    FULL_RESPONSE_WAIT_SECONDS = 20
    FULL_RESPONSE_SETTLE_SECONDS = 2
    RESPONSE_FAST_SCROLL_SWIPES = 6
    RESPONSE_FAST_SCROLL_SLEEP_SECONDS = 0.18

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.yuanbao)

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        return self._build_references_payload()

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        stitched = self._collect_response_with_fast_scroll(driver, prompt=prompt, initial_response=response)
        if stitched and len(self._normalize_text(stitched)) >= len(self._normalize_text(response)):
            return self._strip_leading_ui_noise(stitched)

        return self._strip_leading_ui_noise(
            self._wait_for_full_visible_response(driver, prompt=prompt, initial_response=response)
        )

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._click_top_right_new_chat(driver):
            time.sleep(0.5)
            return

        if self._click_new_chat_from_drawer(driver):
            time.sleep(0.35)
            return
        raise U2DriverError("Yuanbao new chat button not found in drawer.")

    def _click_top_right_new_chat(self, driver: U2Driver) -> bool:
        root = driver.dump_hierarchy_root()
        if self._find_drawer_bounds(root) is not None:
            return False

        bounds = self._find_top_right_new_chat_bounds(root)
        if bounds is None:
            return False

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        return True

    def _click_new_chat_from_drawer(self, driver: U2Driver) -> bool:
        if self._click_new_chat_if_visible(driver):
            return True

        root = driver.dump_hierarchy_root()
        menu_bounds = self._find_left_top_menu_bounds(root)
        if menu_bounds is None:
            return False

        left, top, right, bottom = menu_bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self._click_new_chat_if_visible(driver):
                return True
            time.sleep(0.15)
        return False

    def _click_new_chat_if_visible(self, driver: U2Driver) -> bool:
        root = driver.dump_hierarchy_root()
        bounds = self._find_new_chat_text_bounds(root)
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        return True

    def _find_drawer_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("resource-id") not in {
                "com.tencent.hunyuan.app.chat:id/cv_drawer_container",
                "com.tencent.hunyuan.app.chat:id/cv_drawer",
            }:
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if right - left < 250 or bottom - top < 300:
                continue
            return bounds
        return None

    def _find_top_right_new_chat_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            return None

        app_left, app_top, app_right, app_bottom = app_bounds
        max_bottom = app_top + int((app_bottom - app_top) * 0.14)
        min_left = app_left + int((app_right - app_left) * 0.82)
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            if width < 24 or height < 24:
                continue
            if left < min_left or bottom > max_bottom:
                continue
            if width > 120 or height > 120:
                continue
            candidates.append((right, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _find_left_top_menu_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue

            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            resource_id = node.attrib.get("resource-id", "")
            if resource_id == "ic_navigation_show":
                return bounds
            if top > 140 or left > 120 or width > 120 or height > 120:
                continue
            candidates.append((left + top, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _find_new_chat_text_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if self._node_label(node) != "新建对话":
                continue
            bounds = self._nearest_clickable_bounds(node, parent_map) or U2Driver._parse_bounds(
                node.attrib.get("bounds", "")
            )
            if bounds is not None:
                return bounds
        return None

    def _set_prompt_text(self, driver: U2Driver, *, prompt: str) -> None:
        target = self._find_focused_or_bottom_input(driver)
        if target is None:
            super()._set_prompt_text(driver, prompt=prompt)
            return

        target.click()
        try:
            target.clear_text()
        except Exception:
            pass
        target.set_text(prompt)

    def _find_focused_or_bottom_input(self, driver: U2Driver):
        focused = self._focused_edit_text(driver)
        if focused is not None:
            return focused

        input_bounds = self._find_bottom_input_area_bounds(driver.dump_hierarchy_root())
        if input_bounds is None:
            return None

        left, top, right, bottom = input_bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.2)
        return self._focused_edit_text(driver)

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        try:
            driver.click(self.app.selectors.send_selectors, timeout_seconds=self.SEND_SELECTOR_TIMEOUT_SECONDS)
            return
        except U2DriverError:
            pass

        root = driver.dump_hierarchy_root()
        input_bounds = self._find_prompt_input_bounds(root, prompt=prompt)
        if input_bounds is None:
            raise U2DriverError("Yuanbao prompt was not written into the chat input.")

        bounds = self._find_yuanbao_send_bounds(root, input_bounds=input_bounds)
        if bounds is None:
            raise U2DriverError("Yuanbao send button not found after prompt input.")

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.2)

    def _find_prompt_input_bounds(self, root: ET.Element, *, prompt: str) -> tuple[int, int, int, int] | None:
        prompt_norm = self._normalize_text(prompt)
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.EditText":
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            if prompt_norm and prompt_norm not in text:
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is not None:
                candidates.append((bounds[3], bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _find_yuanbao_send_bounds(
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
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            resource_id = attrs.get("resource-id", "")
            if resource_id == "com.tencent.hunyuan.app.chat:id/fl_slot_send_stop":
                return bounds
            if center_x < input_right - 80:
                continue
            if center_y < input_top - 60 or center_y > input_bottom + 100:
                continue
            candidates.append((center_x, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _wait_for_full_visible_response(self, driver: U2Driver, *, prompt: str, initial_response: str) -> str:
        best = initial_response
        last_change = time.monotonic()
        deadline = time.monotonic() + self.FULL_RESPONSE_WAIT_SECONDS

        while time.monotonic() < deadline:
            candidate = self._pick_full_visible_response(driver.dump_hierarchy_root(), prompt=prompt)
            if candidate and candidate != best and len(candidate) >= len(best):
                best = candidate
                last_change = time.monotonic()

            if best and time.monotonic() - last_change >= self.FULL_RESPONSE_SETTLE_SECONDS:
                return best
            time.sleep(0.35)

        return best

    def _collect_response_with_fast_scroll(self, driver: U2Driver, *, prompt: str, initial_response: str) -> str:
        merged = initial_response.strip()
        root = driver.dump_hierarchy_root()
        merged = self._append_response_block(
            merged,
            self._extract_visible_response_block(root, prompt=prompt),
        )
        merged = self._append_response_block(
            merged,
            self._copy_visible_labeled_block(driver, root=root, prompt=prompt),
        )

        for _ in range(self.RESPONSE_FAST_SCROLL_SWIPES):
            if self._has_latest_answer_bottom_action_row(root):
                break

            self._fast_scroll_response(driver, root=root)
            time.sleep(self.RESPONSE_FAST_SCROLL_SLEEP_SECONDS)

            root = driver.dump_hierarchy_root()
            before = merged
            merged = self._append_response_block(
                merged,
                self._extract_visible_response_block(root, prompt=prompt),
            )

            if merged == before and not self._find_jump_to_bottom_bounds(root):
                break

        jump_bounds = self._find_jump_to_bottom_bounds(root)
        if jump_bounds is not None:
            left, top, right, bottom = jump_bounds
            self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
            time.sleep(self.RESPONSE_FAST_SCROLL_SLEEP_SECONDS)
            merged = self._append_response_block(
                merged,
                self._extract_visible_response_block(driver.dump_hierarchy_root(), prompt=prompt),
            )

        return merged

    def _copy_visible_labeled_block(self, driver: U2Driver, *, root: ET.Element, prompt: str) -> str:
        bounds = self._find_labeled_copy_button_bounds(root)
        if bounds is None:
            return ""

        sentinel = f"__WUYING_YUANBAO_CLIPBOARD_{time.time_ns()}__"
        try:
            driver.device.clipboard = sentinel
        except Exception as exc:
            logger.debug("Failed to seed Yuanbao clipboard before table copy: %s", exc)
            return ""

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)

        prompt_norm = self._normalize_text(prompt)
        deadline = time.monotonic() + 1.2
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
                    and not self._is_non_response_text(self._normalize_text(copied))
                ):
                    return copied
            time.sleep(0.12)
        return ""

    def _find_labeled_copy_button_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if self._node_label(node) != "复制":
                continue

            for bounds in (
                self._nearest_clickable_bounds(node, parent_map),
                U2Driver._parse_bounds(node.attrib.get("bounds", "")),
            ):
                if bounds is None:
                    continue
                left, top, right, bottom = bounds
                width = right - left
                height = bottom - top
                if 20 <= width <= 180 and 20 <= height <= 120:
                    candidates.append((top + bottom, bounds))
                    break

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _extract_visible_response_block(self, root: ET.Element, *, prompt: str) -> str:
        containers = [
            node
            for node in root.iter("node")
            if node.attrib.get("package") == self.app.package_name
            and node.attrib.get("resource-id") == "com.tencent.hunyuan.app.chat:id/chats_message_dtmp_answer_container"
        ]
        if not containers:
            return ""

        container = containers[-1]
        cutoff_top = self._find_response_bottom_action_row_top(container)
        prompt_norm = self._normalize_text(prompt)
        rows: list[tuple[int, int, str]] = []

        for node in container.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.TextView":
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, top, _, _ = bounds
            if cutoff_top is not None and top >= cutoff_top:
                continue

            text = self._clean_response_text(node.attrib.get("text", ""))
            normalized = self._normalize_text(text)
            if not normalized or normalized == prompt_norm:
                continue
            if self._is_non_response_text(normalized):
                continue
            rows.append((top, left, text))

        rows.sort(key=lambda item: (item[0], item[1]))
        return "\n".join(text for _, _, text in rows).strip()

    def _find_response_bottom_action_row_top(self, container: ET.Element) -> int | None:
        row_tops: dict[int, int] = {}
        for node in container.iter("node"):
            attrs = node.attrib
            if attrs.get("package") != self.app.package_name:
                continue
            if attrs.get("class") != "android.widget.Button":
                continue
            if attrs.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            if not (30 <= width <= 100 and 30 <= height <= 100):
                continue

            bucket = top // 12
            row_tops[bucket] = row_tops.get(bucket, 0) + 1

        if not row_tops:
            return None

        best_bucket, count = max(row_tops.items(), key=lambda item: item[1])
        if count < 3:
            return None
        return best_bucket * 12

    def _has_latest_answer_bottom_action_row(self, root: ET.Element) -> bool:
        containers = [
            node
            for node in root.iter("node")
            if node.attrib.get("package") == self.app.package_name
            and node.attrib.get("resource-id") == "com.tencent.hunyuan.app.chat:id/chats_message_dtmp_answer_container"
        ]
        return bool(containers and self._find_response_bottom_action_row_top(containers[-1]) is not None)

    def _fast_scroll_response(self, driver: U2Driver, *, root: ET.Element) -> None:
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            start_x, start_y, end_y = 360, 900, 220
        else:
            left, top, right, bottom = app_bounds
            start_x = (left + right) // 2
            start_y = int(top + (bottom - top) * 0.76)
            end_y = int(top + (bottom - top) * 0.18)

        try:
            self.adb.input_swipe(
                driver.serial,
                start_x=start_x,
                start_y=start_y,
                end_x=start_x,
                end_y=end_y,
                duration_ms=110,
            )
        except Exception as exc:
            logger.debug("Yuanbao response fast scroll failed: %s", exc)

    def _find_jump_to_bottom_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            resource_id = node.attrib.get("resource-id", "")
            if resource_id != "com.tencent.hunyuan.app.chat:id/chat_image_arrow_down":
                continue
            if node.attrib.get("clickable") != "true":
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            _, top, _, bottom = bounds
            candidates.append((top + bottom, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @classmethod
    def _append_response_block(cls, current: str, block: str) -> str:
        current = current.strip()
        block = block.strip()
        if not block:
            return current
        if not current:
            return block
        if block in current:
            return current

        max_overlap = min(len(current), len(block), 1200)
        for size in range(max_overlap, 19, -1):
            if current[-size:] == block[:size]:
                return (current + block[size:]).strip()

        return f"{current}\n{block}".strip()

    @staticmethod
    def _clean_response_text(value: str) -> str:
        lines = [" ".join(line.replace("\xa0", " ").split()) for line in value.strip().splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip()

    @classmethod
    def _strip_leading_ui_noise(cls, value: str) -> str:
        lines = value.strip().splitlines()
        while lines and cls._is_non_response_text(cls._normalize_text(lines[0])):
            lines.pop(0)
        return "\n".join(lines).strip()

    def _pick_full_visible_response(self, root: ET.Element, *, prompt: str) -> str:
        prompt_norm = self._normalize_text(prompt)
        candidates: list[str] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.TextView":
                continue

            text = (node.attrib.get("text") or "").strip()
            normalized = self._normalize_text(text)
            if not text or normalized == prompt_norm:
                continue
            if self._is_non_response_text(normalized):
                continue
            if len(normalized) < 20:
                continue
            candidates.append(text)

        if not candidates:
            return ""
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    def _find_bottom_input_area_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.EditText":
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if right - left < 120 or bottom - top < 20:
                continue
            candidates.append((bottom, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _focused_edit_text(driver: U2Driver):
        try:
            focused = driver.device(className="android.widget.EditText", focused=True)
            if bool(focused.exists):
                return focused
        except Exception:
            return None
        return None

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
    def _is_non_response_text(value: str) -> bool:
        return value in {
            "元宝",
            "Hunyuan",
            "快速思考",
            "内容由AI生成",
            "发消息或按住说话...",
            "复制",
            "保存",
            "全屏",
            "表格",
            "源",
            "上一条",
            "创作",
            "我们",
            "新建对话",
        }

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


__all__ = ["YuanbaoWorkflow"]
