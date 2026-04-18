from __future__ import annotations

import logging
from dataclasses import dataclass, field

from wuying.aliyun_api.client import WuyingApiClient
from wuying.config import AppSettings
from wuying.device.adb import AdbClient
from wuying.device.u2_driver import U2Driver
from wuying.models import AdbEndpoint

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceSession:
    settings: AppSettings
    instance_id: str
    device_id: str | None
    adb_endpoint: str | None = None
    adb: AdbClient = field(init=False)
    serial: str | None = field(init=False, default=None)
    driver: U2Driver | None = field(init=False, default=None)
    _resolved_endpoint: AdbEndpoint | None = field(init=False, default=None)
    _api: WuyingApiClient | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.adb = AdbClient(self.settings.device)

    def ensure_connected(self) -> str:
        if self.serial and self.adb.is_connected(self.serial):
            self.adb.wait_for_device(self.serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)
            return self.serial

        endpoint = self._resolve_endpoint()
        serial = self.adb.connect(endpoint)
        self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)
        self.serial = serial
        return serial

    def ensure_driver(self) -> U2Driver:
        serial = self.ensure_connected()
        if self.driver is None or self.driver.serial != serial:
            self.driver = U2Driver(serial)
        return self.driver

    def reset_driver(self) -> U2Driver:
        self.driver = None
        return self.ensure_driver()

    def reconnect(self) -> U2Driver:
        if self.serial:
            try:
                self.adb.disconnect(self.serial)
            except Exception as exc:
                logger.debug("adb disconnect ignored during reconnect: serial=%s error=%s", self.serial, exc)
        self.serial = None
        self.driver = None
        return self.ensure_driver()

    def close(self) -> None:
        if self.serial:
            try:
                self.adb.disconnect(self.serial)
            except Exception as exc:
                logger.debug("adb disconnect ignored during close: serial=%s error=%s", self.serial, exc)
        self.serial = None
        self.driver = None

    def _resolve_endpoint(self) -> AdbEndpoint:
        if self._resolved_endpoint is not None:
            return self._resolved_endpoint

        raw = self.adb_endpoint or self.settings.device.manual_adb_endpoint
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
            self._resolved_endpoint = AdbEndpoint(
                instance_id=self.instance_id,
                host=host.strip(),
                port=port,
                source="manual",
            )
            return self._resolved_endpoint

        self._resolved_endpoint = self.api.ensure_adb_ready(
            self.instance_id,
            timeout_seconds=self.settings.device.adb_ready_timeout_seconds,
        )
        return self._resolved_endpoint

    @property
    def api(self) -> WuyingApiClient:
        if self._api is None:
            self._api = WuyingApiClient(self.settings.aliyun)
        return self._api
