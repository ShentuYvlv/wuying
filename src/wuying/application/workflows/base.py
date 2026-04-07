from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

from wuying.config import AppSettings, ChatAppSettings
from wuying.invokers import AdbClient, U2Driver, U2DriverError, WuyingApiClient
from wuying.models import AdbEndpoint, PlatformRunResult

logger = logging.getLogger(__name__)


class ChatAppWorkflow(ABC):
    platform_name: str

    def __init__(self, settings: AppSettings, app: ChatAppSettings) -> None:
        self.settings = settings
        self.app = app
        self.adb = AdbClient(settings.device)

    @cached_property
    def api(self) -> WuyingApiClient:
        return WuyingApiClient(self.settings.aliyun)

    def run_once(self, *, instance_id: str, prompt: str) -> PlatformRunResult:
        started_at = datetime.now(tz=UTC)
        endpoint = self._resolve_endpoint(instance_id)
        serial = self.adb.connect(endpoint)
        self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)

        driver = U2Driver(serial)
        driver.wake()
        self._ensure_app_foreground(driver)
        self._ensure_new_chat_session(driver)
        self._ensure_chat_input_ready(driver)
        driver.set_text(self.app.selectors.input_selectors, prompt, timeout_seconds=30)
        driver.click(self.app.selectors.send_selectors, timeout_seconds=30)

        response = driver.wait_for_new_response(
            prompt=prompt,
            timeout_seconds=self.app.response_timeout_seconds,
            settle_seconds=self.app.response_settle_seconds,
            response_selectors=self.app.selectors.response_selectors,
        )
        extra = self._collect_extra_metadata(driver, prompt=prompt, response=response)

        finished_at = datetime.now(tz=UTC)
        output_path = self._build_output_path(instance_id=instance_id, finished_at=finished_at)
        result = PlatformRunResult.build(
            platform=self.platform_name,
            instance_id=instance_id,
            prompt=prompt,
            response=response,
            adb_serial=serial,
            output_path=output_path,
            started_at=started_at,
            finished_at=finished_at,
            extra=extra,
        )
        output_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Saved result to %s", output_path)
        return result

    def _build_output_path(self, *, instance_id: str, finished_at: datetime) -> Path:
        output_dir = self.app.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = finished_at.strftime("%Y%m%dT%H%M%SZ")
        return output_dir / f"{self.platform_name}_{instance_id}_{timestamp}.json"

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
        if driver.find_first(self.app.selectors.input_selectors) is not None:
            return

        entry = driver.find_first(self.app.selectors.enter_chat_selectors)
        if entry is not None:
            entry.click()

        try:
            driver.wait_for_any(self.app.selectors.input_selectors, timeout_seconds=15)
            return
        except U2DriverError:
            pass

        switch_input = driver.find_first(self.app.selectors.switch_to_text_input_selectors)
        if switch_input is not None:
            switch_input.click()

        try:
            driver.wait_for_any(self.app.selectors.input_selectors, timeout_seconds=15)
        except U2DriverError as exc:
            raise U2DriverError(
                f"{self.platform_name} opened, but the text input is still not visible. "
                "Adjust platform selectors."
            ) from exc

    def _ensure_app_foreground(self, driver: U2Driver) -> None:
        current_package = driver.current_package()
        if current_package == self.app.package_name:
            return

        logger.info(
            "Current foreground package is %s, launching %s",
            current_package or "<unknown>",
            self.app.package_name,
        )
        driver.start_app(self.app.package_name, self.app.launch_activity)

    def _ensure_new_chat_session(self, driver: U2Driver) -> None:
        if self._is_chat_page(driver):
            back = driver.find_first(self.app.selectors.chat_back_selectors)
            if back is not None:
                back.click()
                time.sleep(0.2)

        new_chat = driver.find_first(self.app.selectors.new_chat_selectors)
        if new_chat is not None:
            new_chat.click()

    def _is_chat_page(self, driver: U2Driver) -> bool:
        if driver.find_first(self.app.selectors.chat_back_selectors) is not None:
            return True
        if driver.find_first(self.app.selectors.switch_to_text_input_selectors) is not None:
            return True
        if driver.find_first(self.app.selectors.input_selectors) is not None:
            return True
        return False

    @abstractmethod
    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        raise NotImplementedError
