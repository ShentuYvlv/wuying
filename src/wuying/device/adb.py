from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
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
        with self._acquire_process_lock(
            self._connect_lock_name(endpoint.serial),
            timeout_seconds=self.settings.adb_connect_timeout_seconds + 30,
        ):
            if self.is_connected(endpoint.serial):
                logger.info("ADB serial %s is already connected; skipping adb connect", endpoint.serial)
                return endpoint.serial

            command = [self.settings.adb_path, "connect", endpoint.serial]
            last_error: AdbError | None = None
            max_attempts = max(1, self.settings.adb_connect_retry_count + 1)

            for attempt in range(1, max_attempts + 1):
                try:
                    result = self._run(command, timeout=self.settings.adb_connect_timeout_seconds)
                    logger.info("adb connect output for %s: %s", endpoint.serial, result)
                    self._raise_if_connect_failed(endpoint.serial, result)
                    if self._confirm_connected(endpoint.serial):
                        return endpoint.serial
                    state = self.list_devices().get(endpoint.serial, "")
                    if state in {"authorizing", "unauthorized"}:
                        raise self._authorization_error(endpoint.serial, state)
                    raise AdbError(f"adb connect did not reach device state for {endpoint.serial}: state={state or 'missing'}")
                except AdbError as exc:
                    last_error = exc
                    if self._confirm_connected(endpoint.serial):
                        logger.warning(
                            "adb connect reported failure for %s, but the serial became available in adb devices; reusing it",
                            endpoint.serial,
                        )
                        return endpoint.serial
                    if self._has_authentication_failure(str(exc)):
                        logger.warning(
                            "adb connect authentication failed for %s; restarting adb server with configured vendor keys and retrying",
                            endpoint.serial,
                        )
                        self.ensure_server(force_restart=True)
                    if attempt >= max_attempts:
                        break
                    logger.warning(
                        "adb connect failed: serial=%s attempt=%s/%s error=%s",
                        endpoint.serial,
                        attempt,
                        max_attempts - 1,
                        exc,
                    )
                    time.sleep(self.settings.adb_connect_retry_interval_seconds)

            if last_error is not None:
                raise last_error
            raise AdbError(f"adb connect failed for {endpoint.serial}: unknown error")

    def disconnect(self, serial: str) -> None:
        with self._acquire_process_lock(self._connect_lock_name(serial), timeout_seconds=20):
            self._run([self.settings.adb_path, "disconnect", serial], timeout=10)

    def shell(self, serial: str, *parts: str, timeout: int = 30) -> str:
        command = [self.settings.adb_path, "-s", serial, "shell", *parts]
        try:
            return self._run(command, timeout=timeout)
        except AdbError as exc:
            if not self._is_recoverable_shell_failure(str(exc)):
                raise
            logger.warning("ADB shell failed for %s; reconnecting serial and retrying once: %s", serial, exc)
            self._reconnect_serial(serial)
            return self._run(command, timeout=timeout)

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
        deadline = time.monotonic() + timeout_seconds
        last_state = ""
        last_error = ""
        while time.monotonic() < deadline:
            try:
                state = self.list_devices().get(serial, "")
            except AdbError as exc:
                last_error = str(exc)
                state = ""
            last_state = state
            if state == "device":
                probe_timeout = max(5, min(15, int(deadline - time.monotonic())))
                try:
                    self._run(
                        [self.settings.adb_path, "-s", serial, "shell", "echo", "ready"],
                        timeout=probe_timeout,
                    )
                    return
                except AdbError as exc:
                    last_error = str(exc)
            elif state in {"authorizing", "unauthorized"}:
                raise self._authorization_error(serial, state)
            time.sleep(self.settings.adb_connect_confirm_interval_seconds)

        detail = last_error or f"last_state={last_state or 'missing'}"
        raise AdbError(f"Device did not become ready within {timeout_seconds}s: serial={serial}; {detail}")

    @staticmethod
    def _authorization_error(serial: str, state: str) -> AdbError:
        return AdbError(
            f"ADB device is not authorized: serial={serial}, state={state}. "
            "Check cloud phone status, billing, key pair binding, and ADB_VENDOR_KEYS."
        )

    def ensure_server(self, *, force_restart: bool = False) -> None:
        with self._server_lock:
            with self._acquire_process_lock("adb-server", timeout_seconds=30):
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

    def _confirm_connected(self, serial: str) -> bool:
        attempts = max(1, self.settings.adb_connect_confirm_retries + 1)
        for attempt in range(attempts):
            if self.is_connected(serial):
                return True
            if attempt >= attempts - 1:
                break
            time.sleep(self.settings.adb_connect_confirm_interval_seconds)
        return False

    def _reconnect_serial(self, serial: str) -> None:
        try:
            self._run([self.settings.adb_path, "disconnect", serial], timeout=10)
        except AdbError as exc:
            logger.debug("adb disconnect before reconnect ignored for %s: %s", serial, exc)

        endpoint = self._endpoint_from_serial(serial)
        if endpoint is None:
            self.ensure_server(force_restart=True)
            return
        self.connect(endpoint)

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

    @staticmethod
    def _is_recoverable_shell_failure(message: str) -> bool:
        normalized = message.lower()
        return any(
            marker in normalized
            for marker in (
                "command timed out",
                "device offline",
                "device unauthorized",
                "device not found",
                "closed",
                "connection reset",
                "cannot connect",
            )
        )

    @staticmethod
    def _endpoint_from_serial(serial: str) -> AdbEndpoint | None:
        if ":" not in serial:
            return None
        host, port_raw = serial.rsplit(":", 1)
        try:
            port = int(port_raw)
        except ValueError:
            return None
        if not host:
            return None
        return AdbEndpoint(instance_id=serial, host=host, port=port, source="reconnect")

    def _acquire_process_lock(self, name: str, *, timeout_seconds: int):
        return _DirectoryLock(
            self._lock_root() / name,
            timeout_seconds=timeout_seconds,
            stale_after_seconds=max(120, timeout_seconds * 2),
        )

    def _lock_root(self) -> Path:
        root = self.settings.device_lease_dir.parent / "locks"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _connect_lock_name(serial: str) -> str:
        safe_serial = re.sub(r"[^A-Za-z0-9._-]+", "_", serial).strip("._")
        return f"adb-connect-{safe_serial or 'unknown'}"


class _DirectoryLock:
    def __init__(self, path: Path, *, timeout_seconds: int, stale_after_seconds: int) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds

    def __enter__(self) -> "_DirectoryLock":
        deadline = time.monotonic() + max(1, self.timeout_seconds)
        while True:
            try:
                self.path.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError:
                self._clear_if_stale()
                if time.monotonic() >= deadline:
                    raise AdbError(f"Timed out waiting for adb process lock: {self.path}")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def _clear_if_stale(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return
        age_seconds = time.time() - stat.st_mtime
        if age_seconds <= self.stale_after_seconds:
            return
        shutil.rmtree(self.path, ignore_errors=True)
