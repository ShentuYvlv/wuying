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

    def run_once(
        self,
        *,
        instance_id: str,
        prompt: str,
        device_id: str | None = None,
        adb_endpoint: str | None = None,
        save_result: bool = True,
    ) -> PlatformRunResult:
        started_at = datetime.now(tz=UTC)
        endpoint = self._resolve_endpoint(instance_id, adb_endpoint=adb_endpoint)
        serial = self.adb.connect(endpoint)
        self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)

        driver = U2Driver(serial)
        driver.wake()
        self._ensure_app_foreground(driver)
        self._ensure_new_chat_session(driver)
        self._ensure_chat_input_ready(driver)
        self._set_prompt_text(driver, prompt=prompt)
        response_baseline = self._capture_response_baseline(driver)
        self._send_prompt(driver, prompt=prompt)

        response = driver.wait_for_new_response(
            prompt=prompt,
            timeout_seconds=self.app.response_timeout_seconds,
            settle_seconds=self.app.response_settle_seconds,
            message_root_resource_id=self.app.message_list_resource_id,
            response_selectors=self.app.selectors.response_selectors,
            baseline=response_baseline,
        )
        response = self._finalize_response(driver, prompt=prompt, response=response)
        extra = self._collect_extra_metadata(driver, prompt=prompt, response=response)

        finished_at = datetime.now(tz=UTC)
        output_path = self._build_output_path(instance_id=instance_id, device_id=device_id, finished_at=finished_at)
        result = PlatformRunResult.build(
            platform=self.platform_name,
            instance_id=instance_id,
            device_id=device_id,
            prompt=prompt,
            response=response,
            adb_serial=serial,
            output_path=output_path if save_result else "",
            started_at=started_at,
            finished_at=finished_at,
            extra=extra,
        )
        if save_result:
            output_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Saved result to %s", output_path)
        return result

    def _build_output_path(
        self,
        *,
        instance_id: str,
        device_id: str | None,
        finished_at: datetime,
    ) -> Path:
        output_dir = self.app.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = finished_at.strftime("%Y%m%dT%H%M%SZ")
        path_key = device_id or instance_id
        return output_dir / f"{self.platform_name}_{path_key}_{timestamp}.json"

    def _resolve_endpoint(self, instance_id: str, *, adb_endpoint: str | None = None) -> AdbEndpoint:
        raw = adb_endpoint or self.settings.device.manual_adb_endpoint
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
        new_chat = driver.find_first(self.app.selectors.new_chat_selectors)
        if new_chat is not None:
            new_chat.click()
            time.sleep(0.5)
            return

        clicked_back = False
        if self._is_chat_page(driver):
            back = driver.find_first(self.app.selectors.chat_back_selectors)
            if back is not None:
                back.click()
                clicked_back = True
                time.sleep(0.5)

        new_chat = None
        try:
            new_chat = driver.wait_for_any(
                self.app.selectors.new_chat_selectors,
                timeout_seconds=5 if clicked_back else 2,
            )
        except U2DriverError:
            new_chat = driver.find_first(self.app.selectors.new_chat_selectors)
        if new_chat is not None:
            new_chat.click()
            time.sleep(0.5)

    def _is_chat_page(self, driver: U2Driver) -> bool:
        if driver.find_first(self.app.selectors.chat_back_selectors) is not None:
            return True
        if driver.find_first(self.app.selectors.switch_to_text_input_selectors) is not None:
            return True
        if driver.find_first(self.app.selectors.input_selectors) is not None:
            return True
        return False

    def _send_prompt(self, driver: U2Driver, *, prompt: str) -> None:
        driver.click(self.app.selectors.send_selectors, timeout_seconds=30)

    def _set_prompt_text(self, driver: U2Driver, *, prompt: str) -> None:
        driver.set_text(self.app.selectors.input_selectors, prompt, timeout_seconds=30)

    def _capture_response_baseline(self, driver: U2Driver) -> list[str] | None:
        if not self._capture_response_baseline_before_send():
            return None
        return driver.dump_text_nodes(
            include_content_desc=False,
            root_resource_id=self.app.message_list_resource_id,
        )

    def _capture_response_baseline_before_send(self) -> bool:
        return True

    def _finalize_response(self, driver: U2Driver, *, prompt: str, response: str) -> str:
        return response

    @staticmethod
    def _build_references_payload(
        *,
        summary: str | None = None,
        keywords: list[str] | None = None,
        items: list[dict[str, object]] | list[str] | None = None,
    ) -> dict[str, object]:
        normalized_summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
        normalized_keywords = [
            item.strip()
            for item in (keywords or [])
            if isinstance(item, str) and item.strip()
        ]
        normalized_items: list[dict[str, object]] = []

        for index, raw in enumerate(items or [], start=1):
            if isinstance(raw, str):
                title = raw.strip()
                if not title:
                    continue
                normalized_items.append(
                    {
                        "index": index,
                        "title": title,
                        "source": None,
                        "published_at": None,
                        "url": None,
                    }
                )
                continue

            if not isinstance(raw, dict):
                continue

            item_index = raw.get("index")
            if not isinstance(item_index, int):
                item_index = index

            title = raw.get("title")
            source = raw.get("source")
            published_at = raw.get("published_at")
            url = raw.get("url")

            if isinstance(title, str):
                title = title.strip() or None
            else:
                title = None
            if isinstance(source, str):
                source = source.strip() or None
            else:
                source = None
            if isinstance(published_at, str):
                published_at = published_at.strip() or None
            else:
                published_at = None
            if isinstance(url, str):
                url = url.strip() or None
            else:
                url = None

            if title or source or published_at or url:
                normalized_items.append(
                    {
                        "index": item_index,
                        "title": title,
                        "source": source,
                        "published_at": published_at,
                        "url": url,
                    }
                )

        return {
            "references": {
                "summary": normalized_summary,
                "keywords": normalized_keywords,
                "items": normalized_items,
            }
        }

    @abstractmethod
    def _collect_extra_metadata(self, driver: U2Driver, *, prompt: str, response: str) -> dict[str, object]:
        raise NotImplementedError
