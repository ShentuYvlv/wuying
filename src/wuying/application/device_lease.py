from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


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
        path = self._path_for(device_id)
        self._cleanup_stale_lock(path)
        payload = DeviceLeaseRecord(
            device_id=device_id,
            owner=owner,
            created_at=datetime.now(tz=UTC).isoformat(),
            pid=os.getpid(),
        )

        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = self.read(device_id)
            holder = existing.owner if existing is not None else "<unknown>"
            raise DeviceLeaseError(f"Device {device_id} is already leased by {holder}.") from exc

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2))

    def release_many(self, device_ids: list[str], *, owner: str | None = None) -> None:
        for device_id in sorted(set(device_ids)):
            self.release(device_id, owner=owner)

    def release(self, device_id: str, *, owner: str | None = None) -> None:
        path = self._path_for(device_id)
        if not path.exists():
            return
        if owner is not None:
            existing = self.read(device_id)
            if existing is not None and existing.owner != owner:
                return
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def read(self, device_id: str) -> DeviceLeaseRecord | None:
        path = self._path_for(device_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return DeviceLeaseRecord(
            device_id=str(payload.get("device_id") or device_id),
            owner=str(payload.get("owner") or ""),
            created_at=str(payload.get("created_at") or ""),
            pid=int(payload.get("pid") or 0),
        )

    def _cleanup_stale_lock(self, path: Path) -> None:
        if not path.exists():
            return
        if self.stale_after_seconds <= 0:
            return
        age_seconds = datetime.now(tz=UTC).timestamp() - path.stat().st_mtime
        if age_seconds > self.stale_after_seconds:
            try:
                path.unlink()
            except OSError:
                return

    def _path_for(self, device_id: str) -> Path:
        return self.root_dir / f"{device_id}.lock"


__all__ = ["DeviceLeaseError", "DeviceLeaseManager", "DeviceLeaseRecord"]
