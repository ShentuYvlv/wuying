from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from wuying.config import AppSettings
from wuying.invokers import U2Driver, U2DriverError
from wuying.application.workflows.base import ChatAppWorkflow

logger = logging.getLogger(__name__)


class DoubaoWorkflow(ChatAppWorkflow):
    platform_name = "doubao"
    NEW_CHAT_WAIT_SECONDS = 8
    NEW_CHAT_POLL_INTERVAL_SECONDS = 0.35
    REFERENCE_TITLE_RESOURCE_ID = "com.larus.nova:id/tv_reference_title"
    REFERENCE_WRAPPER_RESOURCE_ID = "com.larus.nova:id/ll_reference_title"
    REFERENCE_PANEL_RESOURCE_ID = "com.larus.nova:id/subview_container"
    REFERENCE_KEYWORD_CONTAINER_RESOURCE_ID = "com.larus.nova:id/sub_keyword_reference"
    REFERENCE_INDEX_RESOURCE_ID = "com.larus.nova:id/tv_reference_index"
    REFERENCE_CONTENT_RESOURCE_ID = "com.larus.nova:id/tv_reference_content"

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings, settings.doubao)

    def _ensure_chat_input_ready(self, driver: U2Driver) -> None:
        self._handle_update_dialog(driver)
        super()._ensure_chat_input_ready(driver)

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        deadline = time.monotonic() + self.NEW_CHAT_WAIT_SECONDS
        while time.monotonic() < deadline:
            self._handle_update_dialog(driver)

            new_chat = driver.find_first(self.app.selectors.new_chat_selectors)
            if new_chat is not None:
                new_chat.click()
                time.sleep(0.5)
                return

            if self._is_chat_page(driver):
                back = driver.find_first(self.app.selectors.chat_back_selectors)
                if back is not None:
                    back.click()
                    time.sleep(0.5)
                    continue

            time.sleep(self.NEW_CHAT_POLL_INTERVAL_SECONDS)

        raise U2DriverError("Doubao new chat button not found.")

    def _should_use_action_cache(self, action: str) -> bool:
        return action == "input"

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        try:
            super()._send_prompt(driver, prompt=prompt)
            return
        except U2DriverError:
            pass

        root = driver.dump_hierarchy_root()
        input_bounds = self._find_prompt_input_bounds(root, prompt=prompt)
        if input_bounds is None:
            raise U2DriverError("Doubao prompt was not written into the chat input; stop before fallback tapping.")

        send_bounds = self._find_send_button_bounds(root, input_bounds=input_bounds)
        if send_bounds is None:
            raise U2DriverError("Doubao send button not found after prompt input.")

        left, top, right, bottom = send_bounds
        self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
        self._remember_action_bounds(driver, "send", send_bounds)
        time.sleep(0.2)

    def _handle_update_dialog(self, driver: U2Driver) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            root = driver.dump_hierarchy_root()
            button_bounds = self._find_update_now_button_bounds(root)
            if button_bounds is None:
                return

            left, top, right, bottom = button_bounds
            logger.info("Doubao update dialog detected; clicking update now.")
            self.adb.input_tap(driver.serial, x=(left + right) // 2, y=(top + bottom) // 2)
            time.sleep(1.0)

    def _find_update_now_button_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        has_update_dialog = False
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            text = self._normalize_visible_text(node.attrib.get("text", ""))
            if "发现新版本" in text or "新版本" in text:
                has_update_dialog = True
            if "立即更新" not in text:
                continue

            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            _, top, _, bottom = bounds
            candidates.append((top + bottom, bounds))

        if not has_update_dialog or not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _find_prompt_input_bounds(self, root: ET.Element, *, prompt: str) -> tuple[int, int, int, int] | None:
        expected = re.sub(r"\s+", "", prompt)
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            text = re.sub(r"\s+", "", node.attrib.get("text", ""))
            if not text or expected not in text:
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

    def _find_send_button_bounds(
        self,
        root: ET.Element,
        *,
        input_bounds: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int] | None:
        _, input_top, input_right, input_bottom = input_bounds
        candidates: list[tuple[int, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue

            attrs = node.attrib
            bounds = U2Driver._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue

            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0 or width > 170 or height > 170:
                continue

            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            if center_x < input_right - 20:
                continue
            if center_y < input_top - 70 or center_y > input_bottom + 90:
                continue

            label = self._normalize_visible_text(attrs.get("text", "") or attrs.get("content-desc", ""))
            if any(token in label for token in ("更多", "面板", "语音", "相机")):
                continue
            if attrs.get("clickable") == "true" or "send" in attrs.get("resource-id", "").lower():
                candidates.append((center_x, bounds))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        reference_started_at = time.perf_counter()
        search_summary, reference_keywords, reference_titles = self._collect_reference_metadata(driver)
        if self._references_incomplete(driver, response=response, summary=search_summary, titles=reference_titles):
            logger.info("Doubao references expected but incomplete; retrying once.")
            self._close_reference_panel_if_open(driver)
            time.sleep(0.25)
            retry_summary, retry_keywords, retry_titles = self._collect_reference_metadata(driver)
            search_summary = retry_summary or search_summary
            self._extend_unique(reference_keywords, retry_keywords)
            reference_titles = self._merge_reference_title_lists(reference_titles, retry_titles)

        reference_elapsed = time.perf_counter() - reference_started_at
        logger.info(
            "Doubao timings: references=%.2fs, titles=%s",
            reference_elapsed,
            len(reference_titles),
        )
        payload = self._build_references_payload(
            summary=search_summary,
            keywords=reference_keywords,
            items=reference_titles,
        )
        payload["reference_collection"] = self._reference_collection_status(
            driver,
            response=response,
            summary=search_summary,
            titles=reference_titles,
        )
        return payload

    def _references_incomplete(
        self,
        driver: U2Driver,
        *,
        response: str,
        summary: str | None,
        titles: list[str],
    ) -> bool:
        expected_count = self._extract_reference_count(summary)
        if expected_count is not None:
            return len(titles) < expected_count
        if titles:
            return False
        if summary:
            return True
        if "[__LINK_ICON" in response or "[citation:" in response:
            return True
        try:
            return driver.find_first(self.settings.doubao.selectors.reference_expand_selectors) is not None
        except Exception:
            return False

    def _close_reference_panel_if_open(self, driver: U2Driver) -> None:
        try:
            root = driver.dump_hierarchy_root()
        except Exception:
            return
        if self._find_node_by_resource_id(root, self.REFERENCE_PANEL_RESOURCE_ID) is None:
            return
        try:
            self.adb.shell(driver.serial, "input", "keyevent", "4", timeout=5)
            time.sleep(0.2)
        except Exception as exc:
            logger.debug("Failed to close Doubao reference panel before retry: %s", exc)

    def _reference_collection_status(
        self,
        driver: U2Driver,
        *,
        response: str,
        summary: str | None,
        titles: list[str],
    ) -> dict[str, object]:
        expected_count = self._extract_reference_count(summary)
        collected_count = len(titles)

        expected = bool(summary or titles) or expected_count is not None or self._references_incomplete(
            driver,
            response=response,
            summary=summary,
            titles=titles,
        )
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

    def _extract_reference_metadata(self, visible_texts: list[str]) -> tuple[str | None, list[str], list[str]]:
        normalized = [self._normalize_visible_text(item) for item in visible_texts]
        summary_index = -1
        search_summary: str | None = None

        for index, line in enumerate(normalized):
            if self._looks_like_search_summary(line):
                search_summary = line
                summary_index = index
                break

        if search_summary is None:
            return None, [], []

        reference_keywords: list[str] = []
        reference_titles: list[str] = []
        pending_number: str | None = None

        for line in normalized[summary_index + 1 :]:
            if not line:
                continue

            if self._looks_like_search_summary(line):
                continue

            keywords = self._extract_quoted_keywords(line)
            if keywords:
                for keyword in keywords:
                    if keyword not in reference_keywords:
                        reference_keywords.append(keyword)
                continue

            combined_match = re.match(r"^(\d+)[.、]\s*(.+)$", line)
            if combined_match:
                title = combined_match.group(2).strip()
                if title and title not in reference_titles:
                    reference_titles.append(title)
                pending_number = None
                continue

            standalone_number = re.match(r"^(\d+)[.、]?$", line)
            if standalone_number:
                pending_number = standalone_number.group(1)
                continue

            if pending_number is not None:
                if line not in reference_titles:
                    reference_titles.append(line)
                pending_number = None

        return search_summary, reference_keywords, reference_titles

    def _collect_reference_metadata(self, driver: U2Driver) -> tuple[str | None, list[str], list[str]]:
        visible_texts = driver.dump_message_text_nodes(include_content_desc=True)
        search_summary, reference_keywords, reference_titles = self._extract_reference_metadata(visible_texts)

        expand = driver.find_first(self.settings.doubao.selectors.reference_expand_selectors)
        if expand is None:
            return search_summary, reference_keywords, reference_titles

        expand.click()
        time.sleep(0.05)

        latest_summary = search_summary
        latest_keywords = list(reference_keywords)
        latest_title_map: dict[int, str] = {}
        expected_reference_count = self._extract_reference_count(search_summary)
        stable_rounds = 0
        max_rounds = self._reference_scan_rounds(expected_reference_count)
        quick_swipe_plans = self._reference_quick_swipe_plans(expected_reference_count)
        last_max_index = 0

        for round_index in range(max_rounds):
            root = driver.dump_hierarchy_root()
            page_summary, page_keywords, page_title_map, panel_bounds = self._extract_reference_panel_state(root)
            if page_summary and not latest_summary:
                latest_summary = page_summary
                expected_reference_count = self._extract_reference_count(latest_summary)

            swipe_bounds = panel_bounds or self._extract_message_list_bounds(root)

            added_count = 0
            added_count += self._extend_unique(latest_keywords, page_keywords)
            added_count += self._merge_reference_title_map(latest_title_map, page_title_map)
            current_max_index = max(page_title_map.keys(), default=0)

            if expected_reference_count is not None and len(latest_title_map) >= expected_reference_count:
                break
            if expected_reference_count is not None and current_max_index >= expected_reference_count:
                break
            if added_count == 0 and current_max_index <= last_max_index:
                stable_rounds += 1
            else:
                stable_rounds = 0

            if stable_rounds >= 3:
                break

            last_max_index = max(last_max_index, current_max_index)
            if swipe_bounds is None:
                break

            if round_index < len(quick_swipe_plans):
                x_ratio, start_ratio, end_ratio, duration_ms, settle_seconds = quick_swipe_plans[round_index]
            else:
                x_ratio, start_ratio, end_ratio, duration_ms, settle_seconds = (0.26, 0.89, 0.33, 300, 0.15)

            start_x, start_y, end_x, end_y = self._build_swipe_points(
                swipe_bounds,
                x_ratio=x_ratio,
                start_ratio=start_ratio,
                end_ratio=end_ratio,
            )
            self.adb.input_swipe(
                driver.serial,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                duration_ms=duration_ms,
            )
            time.sleep(settle_seconds)

        latest_titles = [title for _, title in sorted(latest_title_map.items())]
        return latest_summary, latest_keywords, latest_titles

    @staticmethod
    def _normalize_visible_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _looks_like_search_summary(value: str) -> bool:
        return bool(re.search(r"搜索\s*\d+\s*个关键词.*参考\s*\d+\s*篇资料", value))

    @staticmethod
    def _extract_quoted_keywords(value: str) -> list[str]:
        matches = re.findall(r"[“\"]([^”\"]+)[”\"]", value)
        return [item.strip() for item in matches if item.strip()]

    def _extract_reference_titles_from_visible_texts(self, visible_texts: list[str]) -> list[str]:
        normalized = [self._normalize_visible_text(item) for item in visible_texts]
        reference_titles: list[str] = []
        pending_number: str | None = None

        for line in normalized:
            if not line:
                continue

            combined_match = re.match(r"^(\d+)[.、]\s*(.+)$", line)
            if combined_match:
                title = combined_match.group(2).strip()
                if title and title not in reference_titles:
                    reference_titles.append(title)
                pending_number = None
                continue

            standalone_number = re.match(r"^(\d+)[.、]?$", line)
            if standalone_number:
                pending_number = standalone_number.group(1)
                continue

            if pending_number is not None:
                if line not in reference_titles:
                    reference_titles.append(line)
                pending_number = None

        return reference_titles

    def _extract_reference_panel_state(
        self,
        root: ET.Element,
    ) -> tuple[str | None, list[str], dict[int, str], tuple[int, int, int, int] | None]:
        summary = None
        title_node = self._find_node_by_resource_id(root, self.REFERENCE_TITLE_RESOURCE_ID)
        if title_node is not None:
            summary = self._normalize_visible_text(title_node.attrib.get("text", ""))

        panel_node = self._find_node_by_resource_id(root, self.REFERENCE_PANEL_RESOURCE_ID)
        panel_bounds = None
        if panel_node is not None:
            panel_bounds = U2Driver._parse_bounds(panel_node.attrib.get("bounds", ""))

        keyword_container = self._find_node_by_resource_id(root, self.REFERENCE_KEYWORD_CONTAINER_RESOURCE_ID)
        keywords: list[str] = []
        if keyword_container is not None:
            for node in keyword_container.iter("node"):
                text = self._normalize_visible_text(node.attrib.get("text", ""))
                resource_id = node.attrib.get("resource-id", "")
                if not text or resource_id in {
                    self.REFERENCE_TITLE_RESOURCE_ID,
                    self.REFERENCE_INDEX_RESOURCE_ID,
                    self.REFERENCE_CONTENT_RESOURCE_ID,
                }:
                    continue
                for keyword in self._extract_quoted_keywords(text):
                    if keyword not in keywords:
                        keywords.append(keyword)

        title_map: dict[int, str] = {}
        for node in root.iter("node"):
            if node.attrib.get("resource-id") != "com.larus.nova:id/ll_source_item":
                continue
            item_index: int | None = None
            item_title: str | None = None
            for child in node.iter("node"):
                resource_id = child.attrib.get("resource-id", "")
                text = self._normalize_visible_text(child.attrib.get("text", ""))
                if not text:
                    continue
                if resource_id == self.REFERENCE_INDEX_RESOURCE_ID:
                    match = re.match(r"^(\d+)", text)
                    if match:
                        item_index = int(match.group(1))
                elif resource_id == self.REFERENCE_CONTENT_RESOURCE_ID:
                    item_title = text
            if item_index is not None and item_title:
                title_map[item_index] = item_title

        return summary, keywords, title_map, panel_bounds

    def _extract_message_list_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        node = self._find_node_by_resource_id(root, U2Driver.MESSAGE_LIST_RESOURCE_ID)
        if node is None:
            return None
        return U2Driver._parse_bounds(node.attrib.get("bounds", ""))

    @staticmethod
    def _build_swipe_points(
        bounds: tuple[int, int, int, int],
        *,
        x_ratio: float,
        start_ratio: float,
        end_ratio: float,
    ) -> tuple[int, int, int, int]:
        left, top, right, bottom = bounds
        x = int(left + (right - left) * x_ratio)
        start_y = int(top + (bottom - top) * start_ratio)
        end_y = int(top + (bottom - top) * end_ratio)
        return x, start_y, x, end_y

    @staticmethod
    def _extract_reference_count(search_summary: str | None) -> int | None:
        if not search_summary:
            return None
        match = re.search(r"参考\s*(\d+)\s*篇资料", search_summary)
        if not match:
            return None
        return int(match.group(1))

    @staticmethod
    def _extend_unique(target: list[str], new_items: list[str]) -> int:
        added = 0
        for item in new_items:
            if item and item not in target:
                target.append(item)
                added += 1
        return added

    @staticmethod
    def _merge_reference_title_lists(first: list[str], second: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for title in [*first, *second]:
            normalized = title.strip() if isinstance(title, str) else ""
            if not normalized or normalized in seen:
                continue
            merged.append(normalized)
            seen.add(normalized)
        return merged

    @staticmethod
    def _reference_scan_rounds(expected_reference_count: int | None) -> int:
        if expected_reference_count is None:
            return 10
        return max(8, min(16, (expected_reference_count // 8) + 4))

    @staticmethod
    def _reference_quick_swipe_plans(
        expected_reference_count: int | None,
    ) -> list[tuple[float, float, float, int, float]]:
        plans: list[tuple[float, float, float, int, float]] = [
            (0.26, 0.89, 0.33, 300, 0.15),
        ]
        if expected_reference_count is not None and expected_reference_count > 12:
            plans.append((0.26, 0.95, 0.10, 450, 0.18))
        return plans

    @staticmethod
    def _find_node_by_resource_id(root: ET.Element, resource_id: str) -> ET.Element | None:
        for node in root.iter("node"):
            if node.attrib.get("resource-id") == resource_id:
                return node
        return None

    @staticmethod
    def _merge_reference_title_map(target: dict[int, str], new_items: dict[int, str]) -> int:
        added = 0
        for index, title in new_items.items():
            if index not in target and title:
                target[index] = title
                added += 1
        return added


__all__ = ["DoubaoWorkflow"]
