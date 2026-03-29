from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from wuying.config import DeviceSettings
from wuying.models import AdbEndpoint

logger = logging.getLogger(__name__)


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, settings: DeviceSettings) -> None:
        self.settings = settings

    def connect(self, endpoint: AdbEndpoint) -> str:
        command = [self.settings.adb_path, "connect", endpoint.serial]
        result = self._run(command, timeout=self.settings.adb_connect_timeout_seconds)
        logger.info("adb connect output for %s: %s", endpoint.serial, result)
        return endpoint.serial

    def disconnect(self, serial: str) -> None:
        self._run([self.settings.adb_path, "disconnect", serial], timeout=10)

    def shell(self, serial: str, *parts: str, timeout: int = 30) -> str:
        return self._run([self.settings.adb_path, "-s", serial, "shell", *parts], timeout=timeout)

    def wait_for_device(self, serial: str, *, timeout_seconds: int) -> None:
        self._run(
            [self.settings.adb_path, "-s", serial, "wait-for-device"],
            timeout=timeout_seconds,
        )

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
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"adb not found: {self.settings.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            raise AdbError(
                f"Command failed: {' '.join(command)}\nstdout={stdout}\nstderr={stderr}"
            ) from exc
        return (completed.stdout or completed.stderr or "").strip()

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
