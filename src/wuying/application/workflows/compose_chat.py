from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from wuying.application.workflows.base import ChatAppWorkflow
from wuying.invokers import U2Driver, U2DriverError


class ComposeChatWorkflow(ChatAppWorkflow):
    SEND_SELECTOR_TIMEOUT_SECONDS = 2

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        try:
            driver.click(
                self.app.selectors.send_selectors,
                timeout_seconds=self.SEND_SELECTOR_TIMEOUT_SECONDS,
            )
            return
        except U2DriverError:
            pass

        self._tap_compose_trailing_action(driver)
        time.sleep(0.2)

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._click_new_chat_button(driver):
            time.sleep(0.35)
            return

        raise U2DriverError(f"{self.platform_name} new chat button not found.")

    def _click_new_chat_button(self, driver: U2Driver) -> bool:
        button = driver.find_first(self.app.selectors.new_chat_selectors)
        if button is not None:
            button.click()
            return True

        root = driver.dump_hierarchy_root()
        bounds = self._find_top_right_action_bounds(root)
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
            raise U2DriverError(f"{self.platform_name} send fallback failed: app bounds not found.")

        input_bounds = self._find_edit_text_bounds(root)
        if input_bounds is not None:
            input_left, input_top, input_right, input_bottom = input_bounds
            input_width = input_right - input_left
            min_x = input_left + int(input_width * 0.78)
            min_y = max(0, input_top - 40)
            max_y = input_bottom + 140
        else:
            left, top, right, bottom = app_bounds
            min_x = left + int((right - left) * 0.72)
            min_y = top + int((bottom - top) * 0.72)
            max_y = bottom

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
            if center_x < min_x or center_y < min_y or center_y > max_y:
                continue

            score = center_x * 2 + center_y
            if score > best_score:
                best_score = score
                best_center = (center_x, center_y)

        if best_center is None:
            raise U2DriverError(f"{self.platform_name} send fallback failed: trailing action button not found.")

        self.adb.input_tap(driver.serial, x=best_center[0], y=best_center[1])

    def _find_top_right_action_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
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

    def _find_edit_text_bounds(self, root: ET.Element) -> tuple[int, int, int, int] | None:
        for node in root.iter("node"):
            if node.attrib.get("package") != self.app.package_name:
                continue
            if node.attrib.get("class") != "android.widget.EditText":
                continue
            bounds = U2Driver._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is not None:
                return bounds
        return None
