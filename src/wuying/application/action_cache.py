from __future__ import annotations

import json
import re
from pathlib import Path

Bounds = tuple[int, int, int, int]


class ActionBoundsCache:
    def __init__(self, root_dir: Path | str = ".runtime/action_bounds") -> None:
        self.root_dir = Path(root_dir)

    def get(
        self,
        *,
        platform: str,
        package_name: str,
        serial: str,
        window_size: tuple[int, int],
        action: str,
    ) -> Bounds | None:
        data = self._read(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        raw = data.get(action)
        if not isinstance(raw, list) or len(raw) != 4:
            return None
        try:
            left, top, right, bottom = (int(item) for item in raw)
        except (TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def set(
        self,
        *,
        platform: str,
        package_name: str,
        serial: str,
        window_size: tuple[int, int],
        action: str,
        bounds: Bounds,
    ) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        data = self._read(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        data[action] = list(bounds)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def delete(
        self,
        *,
        platform: str,
        package_name: str,
        serial: str,
        window_size: tuple[int, int],
        action: str,
    ) -> None:
        path = self._path(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        data = self._read(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        if action not in data:
            return
        data.pop(action, None)
        if data:
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _read(
        self,
        *,
        platform: str,
        package_name: str,
        serial: str,
        window_size: tuple[int, int],
    ) -> dict[str, object]:
        path = self._path(platform=platform, package_name=package_name, serial=serial, window_size=window_size)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _path(
        self,
        *,
        platform: str,
        package_name: str,
        serial: str,
        window_size: tuple[int, int],
    ) -> Path:
        width, height = window_size
        key = self._safe_key(f"{platform}_{package_name}_{serial}_{width}x{height}")
        return self.root_dir / f"{key}.json"

    @staticmethod
    def _safe_key(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "default"
