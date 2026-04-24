from __future__ import annotations

import logging
import queue
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any, Callable

from wuying.config import DeviceSettings
from wuying.models import SelectorSpec

logger = logging.getLogger(__name__)


class U2DriverError(RuntimeError):
    pass


class U2Driver:
    MESSAGE_LIST_RESOURCE_ID = "com.larus.nova:id/message_list"
    DEFAULT_FIND_POLL_INTERVAL_SECONDS = 0.2
    DEFAULT_RESPONSE_POLL_INTERVAL_SECONDS = 0.35
    DEFAULT_RPC_RETRY_COUNT = 2
    DEFAULT_RPC_RETRY_SLEEP_SECONDS = 0.8
    DEFAULT_CONNECT_RETRY_COUNT = 2
    DEFAULT_CONNECT_RETRY_SLEEP_SECONDS = 1.5

    def __init__(self, serial: str, *, settings: DeviceSettings | None = None) -> None:
        self.serial = serial
        self.settings = settings
        self.find_poll_interval_seconds = self.DEFAULT_FIND_POLL_INTERVAL_SECONDS
        self.response_poll_interval_seconds = self.DEFAULT_RESPONSE_POLL_INTERVAL_SECONDS
        self.rpc_retry_count = settings.u2_rpc_retry_count if settings is not None else self.DEFAULT_RPC_RETRY_COUNT
        self.rpc_retry_sleep_seconds = (
            settings.u2_rpc_retry_sleep_seconds
            if settings is not None
            else self.DEFAULT_RPC_RETRY_SLEEP_SECONDS
        )
        self.connect_retry_count = (
            settings.u2_connect_retry_count
            if settings is not None
            else self.DEFAULT_CONNECT_RETRY_COUNT
        )
        self.connect_retry_sleep_seconds = (
            settings.u2_connect_retry_sleep_seconds
            if settings is not None
            else self.DEFAULT_CONNECT_RETRY_SLEEP_SECONDS
        )
        self.connect_attempt_timeout_seconds = (
            settings.u2_connect_attempt_timeout_seconds
            if settings is not None
            else 35.0
        )
        self.device = self._connect_with_retry()

    @staticmethod
    def _connect(serial: str) -> Any:
        try:
            import uiautomator2 as u2
        except ImportError as exc:
            raise U2DriverError("uiautomator2 is not installed. Install requirements.txt first.") from exc
        return u2.connect(serial)

    def _connect_with_retry(self) -> Any:
        last_exc: Exception | None = None
        max_attempts = max(1, self.connect_retry_count + 1)
        for attempt in range(1, max_attempts + 1):
            try:
                device = self._run_with_timeout(
                    lambda: self._connect(self.serial),
                    timeout_seconds=self.connect_attempt_timeout_seconds,
                    timeout_label="uiautomator2 connect",
                )
                self._probe_device(device)
                return device
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                logger.warning(
                    "uiautomator2 connect failed: serial=%s retry=%s/%s error=%s",
                    self.serial,
                    attempt,
                    max_attempts - 1,
                    exc,
                )
                time.sleep(self.connect_retry_sleep_seconds)
        raise U2DriverError(f"uiautomator2 connect failed after recovery: {self.serial}") from last_exc

    @staticmethod
    def _probe_device(device: Any) -> None:
        info = device.info
        if not isinstance(info, dict) or not info:
            raise U2DriverError("uiautomator2 connected, but device info probe returned no data.")

    def _reset_connection(self) -> None:
        try:
            self.device.reset_uiautomator()
        except Exception as exc:
            logger.warning("Failed to reset uiautomator2 for %s before reconnect: %s", self.serial, exc)
        self.device = self._connect_with_retry()

    def _rpc(self, action: str, func: Callable[[], Any]) -> Any:
        last_exc: Exception | None = None
        max_attempts = max(1, self.rpc_retry_count + 1)
        for attempt in range(1, max_attempts + 1):
            try:
                return func()
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                logger.warning(
                    "uiautomator2 RPC failed: action=%s serial=%s retry=%s/%s error=%s",
                    action,
                    self.serial,
                    attempt,
                    max_attempts - 1,
                    exc,
                )
                self._reset_connection()
                time.sleep(self.rpc_retry_sleep_seconds)
        raise U2DriverError(f"uiautomator2 RPC failed after recovery: {action}") from last_exc

    def health_check(self) -> None:
        self._rpc("health_check", lambda: self.device.info)

    @staticmethod
    def _run_with_timeout(
        func: Callable[[], Any],
        *,
        timeout_seconds: float,
        timeout_label: str,
    ) -> Any:
        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put_nowait((True, func()))
            except Exception as exc:
                result_queue.put_nowait((False, exc))

        thread = threading.Thread(target=run, name="u2-connect-timeout", daemon=True)
        thread.start()
        try:
            succeeded, value = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise U2DriverError(f"{timeout_label} timed out after {timeout_seconds:.1f}s") from exc
        if succeeded:
            return value
        raise value

    def wake(self) -> None:
        self._rpc("screen_on", lambda: self.device.screen_on())

    def current_package(self) -> str:
        current = self._rpc("app_current", lambda: self.device.app_current())

        if not isinstance(current, dict):
            return ""
        package = current.get("package")
        return package.strip() if isinstance(package, str) else ""

    def start_app(self, package_name: str, activity: str | None = None) -> None:
        if activity:
            self._rpc("app_start", lambda: self.device.app_start(package_name, activity=activity, wait=True))
            return
        self._rpc("app_start", lambda: self.device.app_start(package_name, wait=True))

    def wait_for_any(self, selectors: list[SelectorSpec], timeout_seconds: int) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            found = self.find_first(selectors)
            if found is not None:
                return found
            time.sleep(self.find_poll_interval_seconds)
        joined = "; ".join(selector.describe() for selector in selectors)
        raise U2DriverError(f"Timed out waiting for selectors: {joined}")

    def find_first(self, selectors: list[SelectorSpec]) -> Any | None:
        for selector in selectors:
            kwargs = selector.to_u2_kwargs()
            if not kwargs:
                continue
            obj = self.device(**kwargs)
            try:
                exists = bool(obj.exists)
            except Exception:
                exists = False
            if exists:
                return obj
        return None

    def set_text(
        self,
        selectors: list[SelectorSpec],
        text: str,
        timeout_seconds: int = 30,
    ) -> tuple[int, int, int, int] | None:
        target = self.wait_for_any(selectors, timeout_seconds)
        bounds = self.object_bounds(target)
        target.click()
        try:
            target.clear_text()
        except Exception:
            pass
        try:
            target.set_text(text)
        except Exception as exc:
            logger.debug("uiautomator2 object set_text failed; trying clipboard paste: %s", exc)

        if self.wait_for_input_text(text, timeout_seconds=2):
            return bounds

        logger.info("set_text did not update focused input; trying clipboard paste fallback.")
        target.click()
        try:
            self.send_keys(text, clear=True)
        except Exception as exc:
            raise U2DriverError("Failed to paste text into focused input.") from exc

        if not self.wait_for_input_text(text, timeout_seconds=3):
            raise U2DriverError("Text input was not written after set_text and clipboard paste fallback.")
        return bounds

    def click(self, selectors: list[SelectorSpec], timeout_seconds: int = 30) -> tuple[int, int, int, int] | None:
        target = self.wait_for_any(selectors, timeout_seconds)
        bounds = self.object_bounds(target)
        target.click()
        return bounds

    def send_keys(self, text: str, *, clear: bool = True) -> None:
        self._rpc("send_keys", lambda: self.device.send_keys(text, clear=clear))

    def window_size(self) -> tuple[int, int]:
        width, height = self._rpc("window_size", lambda: self.device.window_size())
        return int(width), int(height)

    def wait_for_text(self, text: str, *, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        expected = self._normalize_for_match(text)
        if not expected:
            return True
        while time.monotonic() < deadline:
            root = self.dump_hierarchy_root()
            for node in root.iter("node"):
                current = self._normalize_for_match(node.attrib.get("text", ""))
                if current and (expected in current or current in expected):
                    return True
            time.sleep(self.find_poll_interval_seconds)
        return False

    def wait_for_input_text(self, text: str, *, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        expected = self._normalize_for_match(text)
        if not expected:
            return True
        while time.monotonic() < deadline:
            root = self.dump_hierarchy_root()
            for node in root.iter("node"):
                if node.attrib.get("class") != "android.widget.EditText":
                    continue
                current = self._normalize_for_match(node.attrib.get("text", ""))
                if current and (expected in current or current in expected):
                    return True
            time.sleep(self.find_poll_interval_seconds)
        return False

    def swipe_up(self, start_ratio: float = 0.82, end_ratio: float = 0.28, x_ratio: float = 0.5) -> None:
        try:
            width, height = self.device.window_size()
        except Exception as exc:
            raise U2DriverError("Failed to read device window size for swipe.") from exc

        start_x = int(width * x_ratio)
        end_x = start_x
        start_y = int(height * start_ratio)
        end_y = int(height * end_ratio)
        self.device.swipe(start_x, start_y, end_x, end_y, 0.15)

    def swipe_up_in_message_list_fast(self) -> None:
        try:
            width, height = self.device.window_size()
        except Exception as exc:
            raise U2DriverError("Failed to read device window size for swipe.") from exc

        x = width // 2
        start_y = int(height * 0.74)
        end_y = int(height * 0.42)
        self.device.swipe(x, start_y, x, end_y, 0.10)

    def swipe_up_in_bounds(
        self,
        bounds: tuple[int, int, int, int],
        *,
        start_ratio: float = 0.86,
        end_ratio: float = 0.34,
        duration: float = 0.10,
    ) -> None:
        left, top, right, bottom = bounds
        x = (left + right) // 2
        start_y = int(top + (bottom - top) * start_ratio)
        end_y = int(top + (bottom - top) * end_ratio)
        self.device.swipe(x, start_y, x, end_y, duration)

    def swipe_up_in_best_container(self) -> None:
        hierarchy = self.device.dump_hierarchy()
        try:
            root = ET.fromstring(hierarchy)
        except ET.ParseError as exc:
            raise U2DriverError("Failed to parse UI hierarchy dump.") from exc

        candidate: tuple[int, tuple[int, int, int, int]] | None = None
        for node in root.iter("node"):
            attrs = node.attrib
            cls = attrs.get("class", "")
            resource_id = attrs.get("resource-id", "")
            scrollable = attrs.get("scrollable", "false") == "true"
            is_list_like = (
                scrollable
                or "RecyclerView" in cls
                or "ScrollView" in cls
                or "ListView" in cls
            )
            if not is_list_like:
                continue
            if resource_id.endswith(":id/action_bar"):
                continue

            bounds = self._parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 150:
                continue

            area = width * height
            if candidate is None or area > candidate[0]:
                candidate = (area, bounds)

        if candidate is None:
            self.swipe_up()
            return

        _, (left, top, right, bottom) = candidate
        x = (left + right) // 2
        start_y = int(top + (bottom - top) * 0.82)
        end_y = int(top + (bottom - top) * 0.22)
        self.device.swipe(x, start_y, x, end_y, 0.2)

    def dump_text_nodes(
        self,
        *,
        include_content_desc: bool = True,
        root_resource_id: str | None = None,
    ) -> list[str]:
        root = self.dump_hierarchy_root()
        walk_root = self._find_node_by_resource_id(root, root_resource_id) if root_resource_id else root
        if walk_root is None:
            return []

        texts: list[str] = []
        seen: set[str] = set()
        attrs = ("text", "content-desc") if include_content_desc else ("text",)
        for node in walk_root.iter():
            for attr in attrs:
                value = (node.attrib.get(attr) or "").strip()
                if value and value not in seen:
                    texts.append(value)
                    seen.add(value)
        return texts

    def dump_message_text_nodes(self, *, include_content_desc: bool = False) -> list[str]:
        return self.dump_text_nodes(
            include_content_desc=include_content_desc,
            root_resource_id=self.MESSAGE_LIST_RESOURCE_ID,
        )

    def dump_hierarchy_root(self) -> ET.Element:
        hierarchy = self._rpc("dump_hierarchy", lambda: self.device.dump_hierarchy())
        try:
            return ET.fromstring(hierarchy)
        except ET.ParseError as exc:
            raise U2DriverError("Failed to parse UI hierarchy dump.") from exc

    def wait_for_new_response(
        self,
        *,
        prompt: str,
        timeout_seconds: int,
        settle_seconds: int,
        message_root_resource_id: str | None = None,
        response_selectors: list[SelectorSpec] | None = None,
        baseline: list[str] | None = None,
    ) -> str:
        if baseline is None:
            baseline = self.dump_text_nodes(
                include_content_desc=False,
                root_resource_id=message_root_resource_id,
            )
            logger.info("Captured %s baseline text nodes", len(baseline))
        else:
            logger.info("Using %s baseline text nodes captured before sending prompt", len(baseline))

        deadline = time.monotonic() + timeout_seconds
        last_candidate = ""
        last_change_ts = time.monotonic()

        while time.monotonic() < deadline:
            if response_selectors:
                response_obj = self.find_first(response_selectors)
                if response_obj is not None:
                    text = self._safe_text(response_obj)
                    if text and text != prompt and not self._looks_like_loading_response(text):
                        if text != last_candidate:
                            last_candidate = text
                            last_change_ts = time.monotonic()
                        if time.monotonic() - last_change_ts >= settle_seconds:
                            return last_candidate

            current = self.dump_text_nodes(
                include_content_desc=False,
                root_resource_id=message_root_resource_id,
            )
            candidate = self._pick_response_candidate(baseline=baseline, current=current, prompt=prompt)
            if candidate:
                if candidate != last_candidate:
                    last_candidate = candidate
                    last_change_ts = time.monotonic()
                if time.monotonic() - last_change_ts >= settle_seconds:
                    return last_candidate

            time.sleep(self.response_poll_interval_seconds)

        raise U2DriverError("Timed out waiting for Doubao response.")

    @staticmethod
    def _pick_response_candidate(*, baseline: list[str], current: list[str], prompt: str) -> str:
        baseline_counts = Counter(baseline)
        current_counts: Counter[str] = Counter()
        candidates: list[str] = []
        for item in current:
            current_counts[item] += 1
            if current_counts[item] <= baseline_counts[item]:
                continue
            if not item.strip():
                continue
            if item.strip() == prompt.strip():
                continue
            if U2Driver._looks_like_loading_response(item):
                continue
            if U2Driver._looks_like_invalid_response_candidate(item, prompt=prompt):
                continue
            candidates.append(item)
        if not candidates:
            return ""
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    @staticmethod
    def _safe_text(obj: Any) -> str:
        for attr in ("get_text", "text"):
            value = getattr(obj, attr, None)
            try:
                result = value() if callable(value) else value
            except Exception:
                continue
            if isinstance(result, str) and result.strip():
                return result.strip()
        return ""

    @staticmethod
    def object_bounds(obj: Any) -> tuple[int, int, int, int] | None:
        try:
            info = obj.info
        except Exception:
            return None
        if not isinstance(info, dict):
            return None
        bounds = info.get("bounds")
        if not isinstance(bounds, dict):
            return None
        try:
            left = int(bounds["left"])
            top = int(bounds["top"])
            right = int(bounds["right"])
            bottom = int(bounds["bottom"])
        except (KeyError, TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    @staticmethod
    def _normalize_for_match(value: str) -> str:
        return re.sub(r"\s+", "", value).strip()

    @staticmethod
    def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
        match = re.match(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$", value.strip())
        if not match:
            return None
        return tuple(int(group) for group in match.groups())  # type: ignore[return-value]

    @staticmethod
    def _find_node_by_resource_id(root: ET.Element, resource_id: str | None) -> ET.Element | None:
        if not resource_id:
            return root
        for node in root.iter("node"):
            if node.attrib.get("resource-id") == resource_id:
                return node
        return None

    @staticmethod
    def _looks_like_loading_response(value: str) -> bool:
        text = value.strip()
        if not text:
            return False
        line_count = len([line for line in text.splitlines() if line.strip()])
        if "正在搜索网页" in text and line_count <= 4:
            return True
        if "我来帮您搜索" in text and line_count <= 4:
            return True
        if "搜索网页" in text and line_count <= 4:
            return True
        if re.search(r"找到\s*\d+\s*篇资料", text):
            return True
        if re.search(r"搜索\s*\d+\s*个关键词.*参考\s*\d+\s*篇资料", text):
            return True
        if "⚫" in text or "..." in text or "……" in text:
            first_line = text.splitlines()[0].strip()
            if re.search(r"找到\s*\d+\s*篇资料", first_line):
                return True
        return False

    @staticmethod
    def _looks_like_invalid_response_candidate(value: str, *, prompt: str) -> bool:
        text = value.strip()
        if not text:
            return True
        compact = re.sub(r"\s+", "", text)
        if len(compact) < 8 and not U2Driver._prompt_allows_short_response(prompt, compact):
            return True
        if re.fullmatch(r"\d{1,2}:\d{2}", compact):
            return True
        if compact in {
            "内容由AI生成",
            "发送",
            "复制",
            "引用",
            "搜索网页",
            "新建对话",
            "快速",
            "AI创作",
            "拍题答疑",
            "电话",
            "问点什么",
            "发消息或按住说话",
            "按住说话",
        }:
            return True
        return False

    @staticmethod
    def _prompt_allows_short_response(prompt: str, compact_response: str) -> bool:
        prompt_compact = re.sub(r"\s+", "", prompt or "").upper()
        response_upper = compact_response.upper()
        if not response_upper:
            return False
        if any(token in prompt_compact for token in ("只回复", "仅回复", "只输出", "仅输出", "回答OK", "回复OK")):
            return True
        if response_upper in {"OK", "YES", "NO", "是", "否", "好", "对", "错"}:
            return any(token in prompt_compact for token in ("OK", "YES", "NO", "是", "否", "好", "对", "错"))
        return False
