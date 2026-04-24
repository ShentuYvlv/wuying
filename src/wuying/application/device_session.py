from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from wuying.aliyun_api.client import WuyingApiClient
from wuying.config import AppSettings
from wuying.device.adb import AdbClient
from wuying.device.u2_driver import U2Driver, U2DriverError
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
            try:
                self.adb.wait_for_device(self.serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)
                return self.serial
            except Exception as exc:
                logger.warning(
                    "Existing adb serial is no longer healthy: device_id=%s serial=%s error=%s",
                    self.device_id,
                    self.serial,
                    exc,
                )
                self._invalidate_connection(disconnect=True)

        endpoint = self._resolve_endpoint()
        last_exc: Exception | None = None
        max_attempts = max(1, self.settings.device.driver_init_retry_count + 1)
        for attempt in range(1, max_attempts + 1):
            try:
                serial = self.adb.connect(endpoint)
                self.adb.wait_for_device(serial, timeout_seconds=self.settings.device.adb_ready_timeout_seconds)
                self.serial = serial
                return serial
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "ADB ready failed: device_id=%s endpoint=%s retry=%s/%s error=%s",
                    self.device_id,
                    endpoint.serial,
                    attempt,
                    max_attempts - 1,
                    exc,
                )
                self._invalidate_connection(disconnect=True)
                if attempt >= max_attempts:
                    break
                time.sleep(self.settings.device.driver_init_retry_sleep_seconds)
        if last_exc is not None:
            raise last_exc
        raise U2DriverError("ADB ready failed with no captured exception.")

    def ensure_driver(self) -> U2Driver:
        last_exc: Exception | None = None
        max_attempts = max(1, self.settings.device.driver_init_retry_count + 1)
        for attempt in range(1, max_attempts + 1):
            try:
                serial = self.ensure_connected()
                if self.driver is None or self.driver.serial != serial:
                    self.driver = U2Driver(serial, settings=self.settings.device)
                else:
                    self.driver.health_check()
                return self.driver
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Device driver initialization failed: device_id=%s instance_id=%s retry=%s/%s error=%s",
                    self.device_id,
                    self.instance_id,
                    attempt,
                    max_attempts - 1,
                    exc,
                )
                self._invalidate_connection(disconnect=True)
                if attempt >= max_attempts:
                    break
                time.sleep(self.settings.device.driver_init_retry_sleep_seconds)
        raise U2DriverError(
            f"Failed to initialize device driver: device_id={self.device_id}, instance_id={self.instance_id}"
        ) from last_exc

    def reset_driver(self) -> U2Driver:
        self.driver = None
        return self.ensure_driver()

    def reconnect(self) -> U2Driver:
        self._invalidate_connection(disconnect=True)
        return self.ensure_driver()

    def close(self) -> None:
        self._invalidate_connection(disconnect=True)

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

    def _invalidate_connection(self, *, disconnect: bool) -> None:
        serial = self.serial
        self.driver = None
        self.serial = None
        if not disconnect or not serial:
            return
        try:
            self.adb.disconnect(serial)
        except Exception as exc:
            logger.debug("adb disconnect ignored during invalidate: serial=%s error=%s", serial, exc)
