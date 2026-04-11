from __future__ import annotations

import logging
import math
import re
import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.compose_chat import ComposeChatWorkflow
from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError

logger = logging.getLogger(__name__)


class QianwenWorkflow(ComposeChatWorkflow):
    platform_name = "qianwen"
    FULL_RESPONSE_WAIT_SECONDS = 15
    FULL_RESPONSE_SETTLE_SECONDS = 2
    REFERENCE_PANEL_WAIT_SECONDS = 4
    REFERENCE_PANEL_POLL_INTERVAL_SECONDS = 0.25
    REFERENCE_PANEL_MAX_SWIPES = 24
    REFERENCE_ENTRY_SCAN_SWIPES = 4

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.qianwen)

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        return self._wait_for_full_visible_response(driver, prompt=prompt, initial_response=response)

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        summary, items = self._collect_references(driver)
        return self._build_references_payload(summary=summary, items=items)

    def _collect_references(self, driver: U2Driver) -> tuple[str | None, list[dict[str, object]]]:
        opened = False
        try:
            root, initial_summary, initial_expected_count, _ = self._find_reference_entry(driver)
            if not initial_summary:
                panel_items = self._extract_numbered_reference_items(root)
                if panel_items:
                    return f"参考了{len(panel_items)}篇资料", panel_items
                return None, []

            opened = self._open_reference_panel(driver)
            if not opened:
                return None, []
            summary, items = self._read_reference_panel(
                driver,
                fallback_summary=initial_summary,
                expected_count=initial_expected_count,
            )
            return summary, items
        except Exception as exc:
            logger.warning("Failed to collect Qianwen references: %s", exc)
            return None, []
        finally:
            if opened:
                self._close_reference_panel(driver)

    def _find_reference_entry(
        self,
        driver: U2Driver,
    ) -> tuple[ET.Element, str | None, int | None, tuple[int, int, int, int] | None]:
        root = driver.dump_hierarchy_root()
        summary, expected_count, bounds = self._find_reference_summary(root)
        if summary:
            return root, summary, expected_count, bounds

        for _ in range(self.REFERENCE_ENTRY_SCAN_SWIPES):
            self._swipe_down_for_previous_content(driver)
            time.sleep(0.12)
            root = driver.dump_hierarchy_root()
            summary, expected_count, bounds = self._find_reference_summary(root)
            if summary:
                return root, summary, expected_count, bounds

        return root, None, None, None

    def _open_reference_panel(self, driver: U2Driver) -> bool:
        root = driver.dump_hierarchy_root()
        summary, _, bounds = self._find_reference_summary(root)
        if not summary or bounds is None:
            return False

        before_signature = self._reference_text_signature(root)
        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)

        deadline = time.monotonic() + self.REFERENCE_PANEL_WAIT_SECONDS
        while time.monotonic() < deadline:
            root = driver.dump_hierarchy_root()
            if self._reference_text_signature(root) != before_signature:
                return True
            if self._extract_numbered_reference_items(root):
                return True
            time.sleep(self.REFERENCE_PANEL_POLL_INTERVAL_SECONDS)
        return False

    def _read_reference_panel(
        self,
        driver: U2Driver,
        *,
        fallback_summary: str | None,
        expected_count: int | None,
    ) -> tuple[str | None, list[dict[str, object]]]:
        root = driver.dump_hierarchy_root()
        summary, visible_expected_count, summary_bounds = self._find_reference_summary(root)
        expected_count = expected_count or visible_expected_count
        if not summary or summary_bounds is None:
            summary = fallback_summary
            items = self._extract_numbered_reference_items_with_scroll(
                driver,
                expected_count=expected_count,
            )
            if not summary and items:
                summary = f"参考了{len(items)}篇资料"
            return summary, items

        items = self._extract_reference_items(root, summary_bounds=summary_bounds)
        if expected_count is not None:
            items = items[:expected_count]
        for index, item in enumerate(items, start=1):
            item["index"] = index
        return summary, items

    def _extract_numbered_reference_items_with_scroll(
        self,
        driver: U2Driver,
        *,
        expected_count: int | None,
    ) -> list[dict[str, object]]:
        items_by_index: dict[int, dict[str, object]] = {}
        stable_rounds = 0

        root = driver.dump_hierarchy_root()
        for item in self._extract_numbered_reference_items(root):
            index = item.get("index")
            if isinstance(index, int):
                items_by_index[index] = item
        last_seen_count = len(items_by_index)

        max_swipes = self._reference_panel_swipe_count(
            expected_count=expected_count,
            visible_count=len(items_by_index),
        )

        for swipe_index in range(max_swipes):
            if expected_count is not None and len(items_by_index) >= expected_count:
                break

            self._fast_swipe_reference_panel(driver, root=root)
            time.sleep(0.04)
            root = driver.dump_hierarchy_root()

            for item in self._extract_numbered_reference_items(root):
                index = item.get("index")
                if isinstance(index, int):
                    items_by_index[index] = item

            if len(items_by_index) <= last_seen_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_seen_count = len(items_by_index)
            if expected_count is None and stable_rounds >= 2:
                break

        items = [items_by_index[index] for index in sorted(items_by_index)]
        if expected_count is not None:
            items = items[:expected_count]
        for index, item in enumerate(items, start=1):
            item["index"] = index
        return items

    def _reference_panel_swipe_count(self, *, expected_count: int | None, visible_count: int) -> int:
        if expected_count is None:
            return 3
        if visible_count <= 0:
            return min(self.REFERENCE_PANEL_MAX_SWIPES, 4)
        remaining = max(0, expected_count - visible_count)
        estimated = math.ceil(remaining / max(1, visible_count - 2)) + 2
        return max(1, min(self.REFERENCE_PANEL_MAX_SWIPES, estimated))

    def _fast_swipe_reference_panel(self, driver: U2Driver, *, root: ET.Element) -> None:
        bounds = self._find_reference_scroll_bounds(root)
        if bounds is None:
            try:
                width, height = driver.device.window_size()
            except Exception as exc:
                raise U2DriverError("Failed to read device window size for Qianwen reference swipe.") from exc
            bounds = (0, int(height * 0.16), width, int(height * 0.94))

        left, top, right, bottom = bounds
        x = (left + right) // 2
        start_y = int(top + (bottom - top) * 0.82)
        end_y = int(top + (bottom - top) * 0.24)
        driver.device.swipe(x, start_y, x, end_y, 0.08)

    def _find_reference_scroll_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        item_bounds: list[tuple[int, int, int, int]] = []
        for node in root.iter("node"):
            text = self._normalize_text(node.attrib.get("text", ""))
            if not re.match(r"^\d+\.\s*.+$", text):
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is not None:
                item_bounds.append(bounds)

        if not item_bounds:
            return None

        left = min(item[0] for item in item_bounds)
        top = min(item[1] for item in item_bounds)
        right = max(item[2] for item in item_bounds)
        bottom = max(item[3] for item in item_bounds)
        app_bounds = self._find_app_bounds(root)
        if app_bounds is None:
            return (max(0, left - 40), max(0, top - 80), right + 40, bottom + 420)

        app_left, app_top, app_right, app_bottom = app_bounds
        return (
            app_left,
            max(app_top, top - 80),
            app_right,
            app_bottom,
        )

    def _extract_numbered_reference_items(self, root: ET.Element) -> list[dict[str, object]]:
        nodes: list[tuple[int, int, str]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.TextView":
                continue

            text = self._normalize_text(node.attrib.get("text", ""))
            if not text or self._is_non_response_text(text):
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            _, top, _, bottom = bounds
            nodes.append((top, bottom, text))

        nodes.sort(key=lambda item: item[0])
        titles: list[tuple[int, int, int, str]] = []
        for top, bottom, text in nodes:
            match = re.match(r"^(\d+)\.\s*(.+)$", text)
            if not match:
                continue
            title = match.group(2).strip()
            if not title:
                continue
            titles.append((int(match.group(1)), top, bottom, title))

        items: list[dict[str, object]] = []
        for pos, (index, title_top, title_bottom, title) in enumerate(titles):
            next_title_top = titles[pos + 1][1] if pos + 1 < len(titles) else 10_000
            source = self._find_reference_source_between(
                nodes,
                start_y=title_bottom,
                end_y=next_title_top,
            )
            items.append(
                {
                    "index": index,
                    "title": title,
                    "source": source,
                    "published_at": None,
                    "url": None,
                }
            )

        items.sort(key=lambda item: int(item["index"]))
        return items

    def _find_reference_source_between(
        self,
        nodes: list[tuple[int, int, str]],
        *,
        start_y: int,
        end_y: int,
    ) -> str | None:
        for top, _, text in nodes:
            if top <= start_y or top >= end_y:
                continue
            if len(text) > 80:
                continue
            if "." in text or text.startswith("www"):
                return text
        return None

    def _find_reference_summary(
        self,
        root: ET.Element,
    ) -> tuple[str | None, int | None, tuple[int, int, int, int] | None]:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            match = re.match(r"^参考了\s*(\d+)\s*篇资料$", text)
            if not match:
                continue

            bounds = self._nearest_clickable_bounds(node, parent_map) or U2Driver._parse_bounds(
                node.attrib.get("bounds", "")
            )
            if bounds is None:
                continue
            return text, int(match.group(1)), bounds
        return None, None, None

    def _extract_reference_items(
        self,
        root: ET.Element,
        *,
        summary_bounds: tuple[int, int, int, int],
    ) -> list[dict[str, object]]:
        _, _, _, summary_bottom = summary_bounds
        rows: list[tuple[int, dict[str, object]]] = []

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
            if top <= summary_bottom or right - left < 360 or bottom - top > 180:
                continue

            texts = self._collect_reference_row_texts(node)
            if not texts:
                continue

            title = texts[0]
            source = texts[1] if len(texts) > 1 else None
            rows.append(
                (
                    top,
                    {
                        "index": len(rows) + 1,
                        "title": title,
                        "source": source,
                        "published_at": None,
                        "url": None,
                    },
                )
            )

        rows.sort(key=lambda item: item[0])
        items: list[dict[str, object]] = []
        seen: set[tuple[object, object]] = set()
        for _, item in rows:
            key = (item.get("title"), item.get("source"))
            if key in seen:
                continue
            seen.add(key)
            item["index"] = len(items) + 1
            items.append(item)
        return items

    def _collect_reference_row_texts(self, row: ET.Element) -> list[str]:
        values: list[tuple[int, str]] = []
        for child in row.iter("node"):
            if child.attrib.get("package") != self.app.package_name:
                continue
            if child.attrib.get("class") != "android.widget.TextView":
                continue
            text = self._normalize_text(child.attrib.get("text", ""))
            if not text or text.startswith("参考了"):
                continue
            if self._is_non_response_text(text) or len(text) > 240:
                continue
            bounds = U2Driver._parse_bounds(child.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, _, _, _ = bounds
            values.append((left, text))

        values.sort(key=lambda item: item[0])
        return [value for _, value in values]

    def _reference_text_signature(self, root: ET.Element) -> tuple[str, ...]:
        values: list[str] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            if text:
                values.append(text)
        return tuple(values)

    def _close_reference_panel(self, driver: U2Driver) -> None:
        self.adb.shell(driver.serial, "input", "keyevent", "BACK", timeout=10)
        time.sleep(0.2)

    def _swipe_down_for_previous_content(self, driver: U2Driver) -> None:
        try:
            width, height = driver.device.window_size()
        except Exception as exc:
            raise U2DriverError("Failed to read device window size for Qianwen reference scan.") from exc

        x = width // 2
        driver.device.swipe(x, int(height * 0.34), x, int(height * 0.78), 0.12)

    def _wait_for_full_visible_response(self, driver: U2Driver, *, prompt: str, initial_response: str) -> str:
        best = initial_response
        last_change = time.monotonic()
        deadline = time.monotonic() + self.FULL_RESPONSE_WAIT_SECONDS

        while time.monotonic() < deadline:
            candidate = self._pick_full_visible_response(driver.dump_hierarchy_root(), prompt=prompt)
            if candidate and candidate != best:
                if len(candidate) >= len(best):
                    best = candidate
                    last_change = time.monotonic()

            if best and time.monotonic() - last_change >= self.FULL_RESPONSE_SETTLE_SECONDS:
                return best
            time.sleep(0.35)

        return best

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

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._click_new_chat_from_drawer(driver):
            time.sleep(0.35)
            return
        raise U2DriverError("Qianwen new chat button not found in drawer.")

    def _click_new_chat_from_drawer(self, driver: U2Driver) -> bool:
        if self._click_new_chat_if_visible(driver):
            return True

        root = driver.dump_hierarchy_root()
        menu_bounds = self._find_left_top_menu_bounds(root)
        if menu_bounds is None:
            return False

        left, top, right, bottom = menu_bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.3)
        return self._click_new_chat_if_visible(driver)

    def _click_new_chat_if_visible(self, driver: U2Driver) -> bool:
        root = driver.dump_hierarchy_root()
        bounds = self._find_new_chat_text_bounds(root)
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        return True

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

    @staticmethod
    def _focused_edit_text(driver: U2Driver):
        try:
            focused = driver.device(className="android.widget.EditText", focused=True)
            if bool(focused.exists):
                return focused
        except Exception:
            return None
        return None

    def _find_bottom_input_area_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue

            label = self._node_label(node)
            is_input_label = "发消息" in label or "按住说话" in label
            is_edit_text = node.attrib.get("class") == "android.widget.EditText"
            if not is_input_label and not is_edit_text:
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
            "我是千问",
            "为你答疑 办事 创作，随时找我聊天",
            "深度思考",
            "AI生图",
            "拍题答疑",
            "AI生视频",
            "发消息或按住说话...",
            "内容由AI生成",
        } or value.startswith("参考了")

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


__all__ = ["QianwenWorkflow"]
