from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from wuying.config import DeviceSettings
from wuying.models import AdbEndpoint

logger = logging.getLogger(__name__)


class AdbError(RuntimeError):
    pass


class AdbClient:
    _server_lock = threading.Lock()
    _server_started = False

    def __init__(self, settings: DeviceSettings) -> None:
        self.settings = settings

    def connect(self, endpoint: AdbEndpoint) -> str:
        self.ensure_server()
        if self.is_connected(endpoint.serial):
            logger.info("ADB serial %s is already connected; skipping adb connect", endpoint.serial)
            return endpoint.serial
        command = [self.settings.adb_path, "connect", endpoint.serial]
        try:
            result = self._run(command, timeout=self.settings.adb_connect_timeout_seconds)
        except AdbError as exc:
            if self.is_connected(endpoint.serial):
                logger.warning(
                    "adb connect reported failure for %s, but the serial is already present in adb devices; reusing it",
                    endpoint.serial,
                )
                return endpoint.serial
            if self._has_authentication_failure(str(exc)):
                logger.warning(
                    "adb connect authentication failed for %s; restarting adb server with configured vendor keys and retrying once",
                    endpoint.serial,
                )
                self.ensure_server(force_restart=True)
                result = self._run(command, timeout=self.settings.adb_connect_timeout_seconds)
                logger.info("adb connect output for %s after restart: %s", endpoint.serial, result)
                self._raise_if_connect_failed(endpoint.serial, result)
                return endpoint.serial
            raise
        logger.info("adb connect output for %s: %s", endpoint.serial, result)
        self._raise_if_connect_failed(endpoint.serial, result)
        return endpoint.serial

    def disconnect(self, serial: str) -> None:
        self._run([self.settings.adb_path, "disconnect", serial], timeout=10)

    def shell(self, serial: str, *parts: str, timeout: int = 30) -> str:
        return self._run([self.settings.adb_path, "-s", serial, "shell", *parts], timeout=timeout)

    def input_swipe(
        self,
        serial: str,
        *,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 80,
        timeout: int = 10,
    ) -> str:
        return self.shell(
            serial,
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
            timeout=timeout,
        )

    def input_tap(
        self,
        serial: str,
        *,
        x: int,
        y: int,
        timeout: int = 10,
    ) -> str:
        return self.shell(
            serial,
            "input",
            "tap",
            str(x),
            str(y),
            timeout=timeout,
        )

    def wait_for_device(self, serial: str, *, timeout_seconds: int) -> None:
        self._run(
            [self.settings.adb_path, "-s", serial, "wait-for-device"],
            timeout=timeout_seconds,
        )

    def ensure_server(self, *, force_restart: bool = False) -> None:
        with self._server_lock:
            if force_restart:
                self._run([self.settings.adb_path, "kill-server"], timeout=20)
                self._server_started = False
            if self._server_started and not force_restart:
                return
            self._run([self.settings.adb_path, "start-server"], timeout=20)
            self._server_started = True

    def list_devices(self) -> dict[str, str]:
        self.ensure_server()
        output = self._run([self.settings.adb_path, "devices"], timeout=15)
        devices: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("List of devices attached"):
                continue
            if "\t" not in line:
                continue
            serial, state = line.split("\t", 1)
            devices[serial.strip()] = state.strip()
        return devices

    def is_connected(self, serial: str) -> bool:
        state = self.list_devices().get(serial)
        return state == "device"

    def install_apk(
        self,
        serial: str,
        apk_path: str | Path,
        *,
        replace: bool = True,
        grant_permissions: bool = False,
        timeout_seconds: int = 600,
    ) -> str:
        resolved = Path(apk_path)
        if not resolved.exists():
            raise AdbError(f"APK not found: {resolved}")
        command = [self.settings.adb_path, "-s", serial, "install"]
        if replace:
            command.append("-r")
        if grant_permissions:
            command.append("-g")
        command.append(str(resolved))
        return self._run(command, timeout=timeout_seconds)

    def _run(self, command: list[str], *, timeout: int) -> str:
        env = os.environ.copy()
        adb_vendor_keys = self._resolve_adb_vendor_keys()
        if adb_vendor_keys:
            env["ADB_VENDOR_KEYS"] = adb_vendor_keys

        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=False,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"adb not found: {self.settings.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = self._decode_output(exc.stderr).strip()
            stdout = self._decode_output(exc.stdout).strip()
            raise AdbError(
                f"Command failed: {' '.join(command)}\nstdout={stdout}\nstderr={stderr}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
        stdout = self._decode_output(completed.stdout).strip()
        stderr = self._decode_output(completed.stderr).strip()
        return stdout or stderr or ""

    @staticmethod
    def _raise_if_connect_failed(serial: str, output: str) -> None:
        normalized = output.lower()
        failed_markers = (
            "failed to connect",
            "unable to connect",
            "cannot connect",
            "connection refused",
            "no route to host",
            "timed out",
        )
        if any(marker in normalized for marker in failed_markers):
            raise AdbError(f"adb connect failed for {serial}: {output}")

    def _resolve_adb_vendor_keys(self) -> str | None:
        if self.settings.adb_vendor_keys:
            return self.settings.adb_vendor_keys

        adb_path = Path(self.settings.adb_path)
        if not adb_path.is_absolute():
            adb_path = Path.cwd() / adb_path

        candidate = adb_path.parent / "adbkey"
        if candidate.exists():
            return str(candidate.resolve())

        return None

    @staticmethod
    def _decode_output(raw: bytes | str | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        for encoding in ("utf-8", "gbk", "utf-16"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _has_authentication_failure(message: str) -> bool:
        normalized = message.lower()
        return "failed to authenticate" in normalized or "authentication failed" in normalized
