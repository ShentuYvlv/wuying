from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class DeviceLeaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceLeaseRecord:
    device_id: str
    owner: str
    created_at: str
    pid: int

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "owner": self.owner,
            "created_at": self.created_at,
            "pid": self.pid,
        }


class DeviceLeaseManager:
    def __init__(self, root_dir: Path, *, stale_after_seconds: int = 7200) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.stale_after_seconds = stale_after_seconds

    def acquire_many(self, device_ids: list[str], *, owner: str) -> None:
        acquired: list[str] = []
        try:
            for device_id in sorted(set(device_ids)):
                self.acquire(device_id, owner=owner)
                acquired.append(device_id)
        except Exception:
            self.release_many(acquired, owner=owner)
            raise

    def acquire(self, device_id: str, *, owner: str) -> None:
        self._cleanup_stale_locks(device_id)

        existing = self.read(device_id)
        if existing is not None:
            raise DeviceLeaseError(f"Device {device_id} is already leased by {existing.owner}.")

        payload = DeviceLeaseRecord(
            device_id=device_id,
            owner=owner,
            created_at=datetime.now(tz=UTC).isoformat(),
            pid=os.getpid(),
        )
        path = self._path_for_owner(device_id, owner)

        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = self.read(device_id)
            holder = existing.owner if existing is not None else owner
            raise DeviceLeaseError(f"Device {device_id} is already leased by {holder}.") from exc

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2))

    def release_many(self, device_ids: list[str], *, owner: str | None = None) -> None:
        for device_id in sorted(set(device_ids)):
            self.release(device_id, owner=owner)

    def release(self, device_id: str, *, owner: str | None = None) -> None:
        for path in self._iter_candidate_paths(device_id):
            record = self._read_path(path, fallback_device_id=device_id)
            if owner is not None:
                if record is None or record.owner != owner:
                    continue
            self._unlink_best_effort(path)

    def read(self, device_id: str) -> DeviceLeaseRecord | None:
        active_records: list[tuple[float, DeviceLeaseRecord]] = []
        for path in self._iter_candidate_paths(device_id):
            record = self._read_path(path, fallback_device_id=device_id)
            if record is None:
                continue
            if self._is_stale_lock(path, record):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            active_records.append((mtime, record))

        if not active_records:
            return None

        active_records.sort(key=lambda item: item[0], reverse=True)
        return active_records[0][1]

    def _cleanup_stale_locks(self, device_id: str) -> None:
        for path in self._iter_candidate_paths(device_id):
            record = self._read_path(path, fallback_device_id=device_id)
            if record is None:
                continue
            if self._is_stale_lock(path, record):
                self._unlink_best_effort(path)

    def _iter_candidate_paths(self, device_id: str) -> list[Path]:
        prefix = self._device_prefix(device_id)
        paths = sorted(self.root_dir.glob(f"{prefix}__*.lock"))
        legacy_path = self._legacy_path_for(device_id)
        if legacy_path.exists():
            paths.append(legacy_path)
        return paths

    def _path_for_owner(self, device_id: str, owner: str) -> Path:
        return self.root_dir / f"{self._device_prefix(device_id)}__{self._owner_key(owner)}.lock"

    @staticmethod
    def _device_prefix(device_id: str) -> str:
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", device_id).strip("._")
        if not safe_stem:
            safe_stem = "device"
        safe_stem = safe_stem[:40]
        digest = hashlib.sha1(device_id.encode("utf-8")).hexdigest()[:12]
        return f"{safe_stem}_{digest}"

    @staticmethod
    def _owner_key(owner: str) -> str:
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", owner).strip("._")
        if not safe_stem:
            safe_stem = "owner"
        safe_stem = safe_stem[:48]
        digest = hashlib.sha1(owner.encode("utf-8")).hexdigest()[:8]
        return f"{safe_stem}_{digest}"

    def _legacy_path_for(self, device_id: str) -> Path:
        return self.root_dir / f"{device_id}.lock"

    def _is_stale_lock(self, path: Path, record: DeviceLeaseRecord) -> bool:
        if record.pid > 0 and not self._pid_exists(record.pid):
            return True
        if self.stale_after_seconds > 0:
            try:
                age_seconds = datetime.now(tz=UTC).timestamp() - path.stat().st_mtime
            except OSError:
                return False
            if age_seconds > self.stale_after_seconds:
                return True
        return False

    @staticmethod
    def _read_path(path: Path, *, fallback_device_id: str) -> DeviceLeaseRecord | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return DeviceLeaseRecord(
            device_id=str(payload.get("device_id") or fallback_device_id),
            owner=str(payload.get("owner") or ""),
            created_at=str(payload.get("created_at") or ""),
            pid=int(payload.get("pid") or 0),
        )

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _unlink_best_effort(path: Path) -> None:
        for _ in range(10):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.1)
            except OSError:
                time.sleep(0.1)
        logger.debug("Failed to remove device lease lock: %s", path)


__all__ = ["DeviceLeaseError", "DeviceLeaseManager", "DeviceLeaseRecord"]
