from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from functools import cached_property
from datetime import UTC, datetime
from pathlib import Path

from wuying.aliyun_api import WuyingApiClient
from wuying.config import AppSettings
from wuying.device import AdbClient, U2Driver, U2DriverError
from wuying.models import AdbEndpoint, DoubaoRunResult

logger = logging.getLogger(__name__)


class DoubaoWorkflow:
    REFERENCE_TITLE_RESOURCE_ID = "com.larus.nova:id/tv_reference_title"
    REFERENCE_WRAPPER_RESOURCE_ID = "com.larus.nova:id/ll_reference_title"
    REFERENCE_PANEL_RESOURCE_ID = "com.larus.nova:id/subview_container"
    REFERENCE_KEYWORD_CONTAINER_RESOURCE_ID = "com.larus.nova:id/sub_keyword_reference"
    REFERENCE_INDEX_RESOURCE_ID = "com.larus.nova:id/tv_reference_index"
    REFERENCE_CONTENT_RESOURCE_ID = "com.larus.nova:id/tv_reference_content"

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.adb = AdbClient(settings.device)

    @cached_property
    def api(self) -> WuyingApiClient:
        return WuyingApiClient(self.settings.aliyun)

    def run_once(self, *, instance_id: str, prompt: str) -> DoubaoRunResult:
        started_at = datetime.now(tz=UTC)
        endpoint = self._resolve_endpoint(instance_id)
        serial = self.adb.connect(endpoint)
        self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)

        driver = U2Driver(serial)
        driver.wake()
        self._ensure_doubao_foreground(driver)
        self._ensure_new_chat_session(driver)
        self._ensure_chat_input_ready(driver)
        driver.set_text(self.settings.doubao.selectors.input_selectors, prompt, timeout_seconds=30)
        driver.click(self.settings.doubao.selectors.send_selectors, timeout_seconds=30)
        response_started_at = time.perf_counter()
        response = driver.wait_for_new_response(
            prompt=prompt,
            timeout_seconds=self.settings.doubao.response_timeout_seconds,
            settle_seconds=self.settings.doubao.response_settle_seconds,
            response_selectors=self.settings.doubao.selectors.response_selectors,
        )
        response_elapsed = time.perf_counter() - response_started_at

        reference_started_at = time.perf_counter()
        search_summary, reference_keywords, reference_titles = self._collect_reference_metadata(driver)
        reference_elapsed = time.perf_counter() - reference_started_at
        logger.info(
            "Doubao timings: response=%.2fs, references=%.2fs, titles=%s",
            response_elapsed,
            reference_elapsed,
            len(reference_titles),
        )
        finished_at = datetime.now(tz=UTC)
        output_path = self._write_result(
            instance_id=instance_id,
            prompt=prompt,
            response=response,
            search_summary=search_summary,
            reference_keywords=reference_keywords,
            reference_titles=reference_titles,
            adb_serial=serial,
            started_at=started_at,
            finished_at=finished_at,
        )
        return DoubaoRunResult.build(
            instance_id=instance_id,
            prompt=prompt,
            response=response,
            search_summary=search_summary,
            reference_keywords=reference_keywords,
            reference_titles=reference_titles,
            adb_serial=serial,
            output_path=output_path,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _write_result(
        self,
        *,
        instance_id: str,
        prompt: str,
        response: str,
        search_summary: str | None,
        reference_keywords: list[str],
        reference_titles: list[str],
        adb_serial: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> Path:
        output_dir = self.settings.doubao.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = finished_at.strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"doubao_{instance_id}_{timestamp}.json"
        payload = {
            "instance_id": instance_id,
            "adb_serial": adb_serial,
            "prompt": prompt,
            "response": response,
            "search_summary": search_summary,
            "reference_keywords": reference_keywords,
            "reference_titles": reference_titles,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Saved result to %s", output_path)
        return output_path

    def _resolve_endpoint(self, instance_id: str) -> AdbEndpoint:
        raw = self.settings.device.manual_adb_endpoint
        if raw:
            if ":" not in raw:
                raise ValueError(
                    "WUYING_MANUAL_ADB_ENDPOINT must use host:port format, for example 1.2.3.4:5555"
                )
            host, port_text = raw.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError as exc:
                raise ValueError("WUYING_MANUAL_ADB_ENDPOINT port must be an integer.") from exc
            logger.info("Using manual ADB endpoint for %s via %s", instance_id, raw)
            return AdbEndpoint(instance_id=instance_id, host=host.strip(), port=port, source="manual")

        return self.api.ensure_adb_ready(
            instance_id,
            timeout_seconds=self.settings.device.adb_ready_timeout_seconds,
        )

    def _ensure_chat_input_ready(self, driver: U2Driver) -> None:
        if driver.find_first(self.settings.doubao.selectors.input_selectors) is not None:
            return

        entry = driver.find_first(self.settings.doubao.selectors.enter_chat_selectors)
        if entry is not None:
            entry.click()

        try:
            driver.wait_for_any(self.settings.doubao.selectors.input_selectors, timeout_seconds=15)
            return
        except U2DriverError:
            pass

        switch_input = driver.find_first(self.settings.doubao.selectors.switch_to_text_input_selectors)
        if switch_input is not None:
            switch_input.click()

        try:
            driver.wait_for_any(self.settings.doubao.selectors.input_selectors, timeout_seconds=15)
        except U2DriverError as exc:
            raise U2DriverError(
                "Doubao opened, but the text input is still not visible. Adjust "
                "DOUBAO_SWITCH_TO_TEXT_INPUT_SELECTORS_JSON or DOUBAO_INPUT_SELECTORS_JSON."
            ) from exc

    def _ensure_doubao_foreground(self, driver: U2Driver) -> None:
        current_package = driver.current_package()
        if current_package == self.settings.doubao.package_name:
            return

        logger.info(
            "Current foreground package is %s, launching %s",
            current_package or "<unknown>",
            self.settings.doubao.package_name,
        )
        driver.start_app(
            self.settings.doubao.package_name,
            self.settings.doubao.launch_activity,
        )

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._is_chat_page(driver):
            back = driver.find_first(self.settings.doubao.selectors.chat_back_selectors)
            if back is not None:
                back.click()

        new_chat = driver.find_first(self.settings.doubao.selectors.new_chat_selectors)
        if new_chat is not None:
            new_chat.click()

    def _is_chat_page(self, driver: U2Driver) -> bool:
        if driver.find_first(self.settings.doubao.selectors.chat_back_selectors) is not None:
            return True
        if driver.find_first(self.settings.doubao.selectors.switch_to_text_input_selectors) is not None:
            return True
        if driver.find_first(self.settings.doubao.selectors.input_selectors) is not None:
            return True
        return False

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
