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
    COPY_BUTTON_SCROLL_INTERVAL_SECONDS = 1.0
    CLIPBOARD_UPDATE_WAIT_SECONDS = 2.0
    REFERENCE_SHEET_WAIT_SECONDS = 4
    REFERENCE_SHEET_POLL_INTERVAL_SECONDS = 0.25

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.kimi)

    def _prepare_foreground_app(self, driver: U2Driver) -> None:
        self._close_reference_sheet(driver)

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        copied = self._wait_for_copy_button_and_read_clipboard(driver, prompt=prompt)
        if copied:
            return copied

        logger.warning("Kimi copy button path failed; falling back to visible UI text.")
        root = driver.dump_hierarchy_root()
        block = self._extract_response_block(root=root, prompt=prompt, first_segment=response)
        return block or response

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        summary, items = self._collect_references(driver)
        expected = self._references_expected(driver, summary=summary, items=items)
        if self._references_incomplete(expected=expected, summary=summary, items=items):
            logger.info("Kimi references expected but incomplete; retrying once.")
            self._close_reference_sheet(driver)
            time.sleep(0.25)
            retry_summary, retry_items = self._collect_references(driver)
            summary = retry_summary or summary
            items = self._merge_reference_items(items, retry_items)

        payload = self._build_references_payload(summary=summary, items=items)
        payload["reference_collection"] = self._reference_collection_status(
            expected=expected or bool(summary or items),
            summary=summary,
            items=items,
        )
        return payload

    def _set_prompt_text(self, driver: U2Driver, *, prompt: str) -> None:
        if self._try_fast_set_prompt_text(driver, prompt=prompt):
            return

        target = self._find_kimi_input_by_label(driver)
        if target is None:
            target = self._find_bottom_edit_text_after_tap(driver)
        if target is None:
            super()._set_prompt_text(driver, prompt=prompt)
            return

        self._remember_action_object_bounds(driver, "input", target)
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
        if self._try_fast_send_prompt(driver, prompt=prompt):
            return

        prompt_input_bounds = self._find_prompt_input_bounds(driver.dump_hierarchy_root(), prompt=prompt)
        if prompt_input_bounds is None:
            raise U2DriverError("Kimi prompt was not written into the chat input; stop before clicking function chips.")

        try:
            bounds = driver.click(self.app.selectors.send_selectors, timeout_seconds=self.SEND_SELECTOR_TIMEOUT_SECONDS)
            self._remember_action_bounds(driver, "send", bounds)
            return
        except U2DriverError:
            pass

        root = driver.dump_hierarchy_root()
        bounds = self._find_kimi_send_button_bounds(root, input_bounds=prompt_input_bounds)
        if bounds is None:
            raise U2DriverError("Kimi send button not found after prompt input; stop before clicking fallback buttons.")

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        self._remember_action_bounds(driver, "send", bounds)
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
                self._scroll_towards_answer_bottom(driver, root=root)
                last_scroll_ts = now
            time.sleep(self.COPY_BUTTON_POLL_INTERVAL_SECONDS)

        return ""

    def _scroll_towards_answer_bottom(self, driver: U2Driver, *, root: ET.Element) -> None:
        jump_bounds = self._find_jump_to_bottom_bounds(root)
        if jump_bounds is not None:
            left, top, right, bottom = jump_bounds
            self.adb.input_tap(
                driver.serial,
                x=(left + right) // 2,
                y=(top + bottom) // 2,
            )
            time.sleep(0.12)
            return

        try:
            driver.swipe_up(start_ratio=0.78, end_ratio=0.34, x_ratio=0.5)
        except Exception as exc:
            logger.debug("Kimi answer-bottom scroll failed: %s", exc)

    def _find_jump_to_bottom_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if self._node_label(node) != "JumpToBottom":
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

    def _collect_references(self, driver: U2Driver) -> tuple[str | None, list[dict[str, object]]]:
        try:
            if not self._open_reference_sheet(driver):
                return None, []
            summary, expected_count, items = self._read_reference_sheet(driver)
            if expected_count is not None and len(items) < expected_count:
                summary, items = self._read_reference_sheet_with_fast_scroll(
                    driver,
                    summary=summary,
                    expected_count=expected_count,
                    initial_items=items,
                )
            self._close_reference_sheet(driver)
            return summary, items
        except Exception as exc:
            logger.warning("Failed to collect Kimi references: %s", exc)
            return None, []

    def _references_expected(
        self,
        driver: U2Driver,
        *,
        summary: str | None,
        items: list[dict[str, object]],
    ) -> bool:
        if summary or items:
            return True
        try:
            root = driver.dump_hierarchy_root()
        except Exception:
            return False
        return bool(self._find_reference_summary(root)[0]) or self._find_reference_button_bounds(root) is not None

    def _references_incomplete(
        self,
        *,
        expected: bool,
        summary: str | None,
        items: list[dict[str, object]],
    ) -> bool:
        expected_count = self._reference_count_from_summary(summary)
        if expected_count is not None:
            return len(items) < expected_count
        return expected and not items

    @staticmethod
    def _reference_count_from_summary(summary: str | None) -> int | None:
        if not summary:
            return None
        match = re.search(r"引用来源\s*(\d+)", summary)
        if not match:
            return None
        return int(match.group(1))

    @staticmethod
    def _merge_reference_items(
        first: list[dict[str, object]],
        second: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen: set[tuple[object, object, object, object]] = set()
        for item in [*first, *second]:
            key = (
                item.get("title"),
                item.get("source"),
                item.get("published_at"),
                item.get("url"),
            )
            if key in seen:
                continue
            if not any(key):
                continue
            copied = dict(item)
            copied["index"] = len(merged) + 1
            merged.append(copied)
            seen.add(key)
        return merged

    def _reference_collection_status(
        self,
        *,
        expected: bool,
        summary: str | None,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        expected_count = self._reference_count_from_summary(summary)
        collected_count = len(items)
        if expected_count is not None:
            if collected_count >= expected_count:
                status = "complete"
            elif collected_count > 0:
                status = "partial"
            else:
                status = "missing"
        elif expected:
            status = "partial" if collected_count > 0 else "missing"
        else:
            status = "not_expected"

        return {
            "status": status,
            "expected_count": expected_count,
            "collected_count": collected_count,
        }

    def _open_reference_sheet(self, driver: U2Driver) -> bool:
        root = driver.dump_hierarchy_root()
        if self._find_reference_summary(root)[0]:
            return True

        bounds = self._find_reference_button_bounds(root)
        if bounds is None:
            return False

        left, top, right, bottom = bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)

        deadline = time.monotonic() + self.REFERENCE_SHEET_WAIT_SECONDS
        while time.monotonic() < deadline:
            root = driver.dump_hierarchy_root()
            if self._find_reference_summary(root)[0]:
                return True
            time.sleep(self.REFERENCE_SHEET_POLL_INTERVAL_SECONDS)
        return False

    def _read_reference_sheet(self, driver: U2Driver) -> tuple[str | None, int | None, list[dict[str, object]]]:
        root = driver.dump_hierarchy_root()
        summary, expected_count, summary_bounds = self._find_reference_summary(root)
        if not summary or summary_bounds is None:
            return None, None, []
        return summary, expected_count, self._extract_reference_rows(root, summary_bounds=summary_bounds)

    def _read_reference_sheet_with_fast_scroll(
        self,
        driver: U2Driver,
        *,
        summary: str | None,
        expected_count: int,
        initial_items: list[dict[str, object]],
    ) -> tuple[str | None, list[dict[str, object]]]:
        items_by_key = {
            (item.get("title"), item.get("source")): item
            for item in initial_items
            if item.get("title") or item.get("source")
        }
        max_swipes = max(0, min(4, (expected_count + 3) // 4))

        for _ in range(max_swipes):
            if len(items_by_key) >= expected_count:
                break

            root = driver.dump_hierarchy_root()
            _, _, sheet_bounds = self._find_reference_summary(root)
            if sheet_bounds is None:
                break

            # The summary is inside the bottom sheet. Swipe within the sheet, not the whole app.
            sheet_bottom = self._find_reference_sheet_bottom(root)
            if sheet_bottom is None:
                break
            summary_left, summary_top, summary_right, _ = sheet_bounds
            driver.swipe_up_in_bounds(
                (0, summary_top, max(summary_right, 720), sheet_bottom),
                start_ratio=0.82,
                end_ratio=0.30,
                duration=0.08,
            )
            time.sleep(0.12)

            next_summary, _, next_items = self._read_reference_sheet(driver)
            summary = next_summary or summary
            for item in next_items:
                key = (item.get("title"), item.get("source"))
                if key[0] or key[1]:
                    items_by_key[key] = item

        items = list(items_by_key.values())
        for index, item in enumerate(items, start=1):
            item["index"] = index
        return summary, items

    def _find_reference_button_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        parent_map = {child: parent for parent in root.iter() for child in parent}
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if self._node_label(node) != "引用":
                continue

            bounds = self._reference_label_click_bounds(node, parent_map)
            if bounds is None:
                continue

            _, top, _, bottom = bounds
            candidates.append((top + bottom, bounds))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _reference_label_click_bounds(
        self,
        node: ET.Element,
        parent_map: dict[ET.Element, ET.Element],
    ) -> tuple[int, int, int, int] | None:
        parent_bounds = self._nearest_clickable_bounds(node, parent_map)
        if parent_bounds is not None:
            left, top, right, bottom = parent_bounds
            width = right - left
            height = bottom - top
            if 30 <= width <= 500 and 20 <= height <= 140:
                return parent_bounds

        text_bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
        if text_bounds is None:
            return None
        if self._is_reasonable_action_button_bounds(text_bounds):
            return text_bounds
        return None

    def _find_reference_summary(
        self,
        root: ET.Element,
    ) -> tuple[str | None, int | None, tuple[int, int, int, int] | None]:
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = self._normalize_text(node.attrib.get("text", ""))
            if not text:
                continue
            match = re.match(r"^引用来源\s*(\d+)?$", text)
            if not match:
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            count = int(match.group(1)) if match.group(1) else None
            return text, count, bounds
        return None, None, None

    def _extract_reference_rows(
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

            row_bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if row_bounds is None:
                continue
            left, top, right, bottom = row_bounds
            if top <= summary_bottom or right - left < 500 or bottom - top > 140:
                continue

            text_nodes = self._collect_row_text_nodes(node)
            if not text_nodes:
                continue

            text_nodes.sort(key=lambda item: item[0])
            title = text_nodes[0][1]
            source = text_nodes[-1][1] if len(text_nodes) > 1 else None
            if source == title:
                source = None

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

    def _collect_row_text_nodes(self, row: ET.Element) -> list[tuple[int, str]]:
        text_nodes: list[tuple[int, str]] = []
        for child in row.iter("node"):
            if child.attrib.get("package") != self.app.package_name:
                continue
            if child.attrib.get("class") != "android.widget.TextView":
                continue
            text = self._normalize_text(child.attrib.get("text", ""))
            if not text or text.startswith("引用来源"):
                continue

            bounds = U2Driver._parse_bounds(child.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, _, _, _ = bounds
            text_nodes.append((left, text))
        return text_nodes

    def _find_reference_sheet_bottom(self, root: ET.Element) -> int | None:
        _, _, summary_bounds = self._find_reference_summary(root)
        if summary_bounds is None:
            return None

        _, summary_top, _, _ = summary_bounds
        best_bottom: int | None = None
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if left == 0 and right >= 700 and top <= summary_top <= bottom and bottom <= 1210:
                if best_bottom is None or bottom > best_bottom:
                    best_bottom = bottom
        return best_bottom

    def _close_reference_sheet(self, driver: U2Driver) -> None:
        root = driver.dump_hierarchy_root()
        bounds = self._find_close_sheet_bounds(root)
        if bounds is not None:
            left, top, right, bottom = bounds
            self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
            time.sleep(0.1)
            return

        _, _, summary_bounds = self._find_reference_summary(root)
        if summary_bounds is None:
            return

        left, top, right, _ = summary_bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=max(80, top - 80))
        time.sleep(0.1)

    def _find_close_sheet_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if self._node_label(node) != "关闭工作表":
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is not None:
                return bounds
        return None

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
