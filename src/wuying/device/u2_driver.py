from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

from wuying.models import SelectorSpec

logger = logging.getLogger(__name__)


class U2DriverError(RuntimeError):
    pass


class U2Driver:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.device = self._connect(serial)

    @staticmethod
    def _connect(serial: str) -> Any:
        try:
            import uiautomator2 as u2
        except ImportError as exc:
            raise U2DriverError("uiautomator2 is not installed. Install requirements.txt first.") from exc
        return u2.connect(serial)

    def wake(self) -> None:
        self.device.screen_on()

    def current_package(self) -> str:
        try:
            current = self.device.app_current()
        except Exception as exc:
            raise U2DriverError("Failed to read current foreground app.") from exc

        if not isinstance(current, dict):
            return ""
        package = current.get("package")
        return package.strip() if isinstance(package, str) else ""

    def start_app(self, package_name: str, activity: str | None = None) -> None:
        if activity:
            self.device.app_start(package_name, activity=activity, wait=True)
            return
        self.device.app_start(package_name, wait=True)

    def wait_for_any(self, selectors: list[SelectorSpec], timeout_seconds: int) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            found = self.find_first(selectors)
            if found is not None:
                return found
            time.sleep(1)
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

    def set_text(self, selectors: list[SelectorSpec], text: str, timeout_seconds: int = 30) -> None:
        target = self.wait_for_any(selectors, timeout_seconds)
        target.click()
        try:
            target.clear_text()
        except Exception:
            pass
        target.set_text(text)

    def click(self, selectors: list[SelectorSpec], timeout_seconds: int = 30) -> None:
        target = self.wait_for_any(selectors, timeout_seconds)
        target.click()

    def dump_text_nodes(self) -> list[str]:
        hierarchy = self.device.dump_hierarchy()
        try:
            root = ET.fromstring(hierarchy)
        except ET.ParseError as exc:
            raise U2DriverError("Failed to parse UI hierarchy dump.") from exc

        texts: list[str] = []
        for node in root.iter():
            text = (node.attrib.get("text") or "").strip()
            if text:
                texts.append(text)
        return texts

    def wait_for_new_response(
        self,
        *,
        prompt: str,
        timeout_seconds: int,
        settle_seconds: int,
        response_selectors: list[SelectorSpec] | None = None,
    ) -> str:
        baseline = self.dump_text_nodes()
        logger.info("Captured %s baseline text nodes", len(baseline))

        deadline = time.monotonic() + timeout_seconds
        last_candidate = ""
        last_change_ts = time.monotonic()

        while time.monotonic() < deadline:
            if response_selectors:
                response_obj = self.find_first(response_selectors)
                if response_obj is not None:
                    text = self._safe_text(response_obj)
                    if text and text != prompt:
                        if text != last_candidate:
                            last_candidate = text
                            last_change_ts = time.monotonic()
                        if time.monotonic() - last_change_ts >= settle_seconds:
                            return last_candidate

            current = self.dump_text_nodes()
            candidate = self._pick_response_candidate(baseline=baseline, current=current, prompt=prompt)
            if candidate:
                if candidate != last_candidate:
                    last_candidate = candidate
                    last_change_ts = time.monotonic()
                if time.monotonic() - last_change_ts >= settle_seconds:
                    return last_candidate

            time.sleep(1)

        raise U2DriverError("Timed out waiting for Doubao response.")

    @staticmethod
    def _pick_response_candidate(*, baseline: list[str], current: list[str], prompt: str) -> str:
        baseline_set = set(baseline)
        candidates = [
            item
            for item in current
            if item not in baseline_set and item.strip() and item.strip() != prompt.strip()
        ]
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
