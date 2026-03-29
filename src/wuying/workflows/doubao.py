from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from wuying.aliyun_api import WuyingApiClient
from wuying.config import AppSettings
from wuying.device import AdbClient, U2Driver
from wuying.models import DoubaoRunResult

logger = logging.getLogger(__name__)


class DoubaoWorkflow:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.api = WuyingApiClient(settings.aliyun)
        self.adb = AdbClient(settings.device)

    def run_once(self, *, instance_id: str, prompt: str) -> DoubaoRunResult:
        started_at = datetime.now(tz=UTC)
        endpoint = self.api.ensure_adb_ready(
            instance_id,
            timeout_seconds=self.settings.device.adb_ready_timeout_seconds,
        )
        serial = self.adb.connect(endpoint)
        self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)

        driver = U2Driver(serial)
        driver.wake()
        driver.start_app(
            self.settings.doubao.package_name,
            self.settings.doubao.launch_activity,
        )
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
