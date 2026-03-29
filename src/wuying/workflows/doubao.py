from __future__ import annotations

import json
import logging
from functools import cached_property
from datetime import UTC, datetime
from pathlib import Path

from wuying.aliyun_api import WuyingApiClient
from wuying.config import AppSettings
from wuying.device import AdbClient, U2Driver, U2DriverError
from wuying.models import AdbEndpoint, DoubaoRunResult

logger = logging.getLogger(__name__)


class DoubaoWorkflow:
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
        response = driver.wait_for_new_response(
            prompt=prompt,
            timeout_seconds=self.settings.doubao.response_timeout_seconds,
            settle_seconds=self.settings.doubao.response_settle_seconds,
            response_selectors=self.settings.doubao.selectors.response_selectors,
        )
        finished_at = datetime.now(tz=UTC)
        output_path = self._write_result(
            instance_id=instance_id,
            prompt=prompt,
            response=response,
            adb_serial=serial,
            started_at=started_at,
            finished_at=finished_at,
        )
        return DoubaoRunResult.build(
            instance_id=instance_id,
            prompt=prompt,
            response=response,
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
