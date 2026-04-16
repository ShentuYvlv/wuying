from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from wuying.config import AppSettings


class DevicePoolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceTarget:
    device_id: str
    instance_id: str
    adb_endpoint: str | None
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "adb_endpoint": self.adb_endpoint,
            "enabled": self.enabled,
        }


class DevicePool:
    def __init__(self, devices: list[DeviceTarget]) -> None:
        self._devices = devices
        self._by_id = {device.device_id: device for device in devices}

    @property
    def devices(self) -> list[DeviceTarget]:
        return list(self._devices)

    def enabled_devices(self) -> list[DeviceTarget]:
        return [device for device in self._devices if device.enabled]

    def get(self, device_id: str) -> DeviceTarget:
        try:
            return self._by_id[device_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._by_id))
            raise DevicePoolError(f"Unknown device_id: {device_id}. Available: {available}") from exc

    def select(self, device_ids: list[str]) -> list[DeviceTarget]:
        selected = [self.get(device_id) for device_id in device_ids]
        disabled = [device.device_id for device in selected if not device.enabled]
        if disabled:
            raise DevicePoolError(f"Selected disabled devices: {', '.join(disabled)}")
        return selected


def load_device_pool(settings: AppSettings) -> DevicePool:
    path = settings.device.device_pool_file
    if path is None or not path.exists():
        return DevicePool([])

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DevicePoolError(f"Invalid JSON in device pool file: {path}") from exc

    if not isinstance(data, list):
        raise DevicePoolError(f"Device pool file must contain a JSON list: {path}")

    devices: list[DeviceTarget] = []
    seen: set[str] = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise DevicePoolError(f"Device pool item #{index} must be a JSON object.")

        device_id = str(item.get("device_id") or "").strip()
        instance_id = str(item.get("instance_id") or "").strip()
        adb_endpoint = str(item.get("adb_endpoint") or "").strip() or None
        enabled = bool(item.get("enabled", True))

        if not device_id:
            raise DevicePoolError(f"Device pool item #{index} is missing device_id.")
        if device_id in seen:
            raise DevicePoolError(f"Duplicate device_id in pool: {device_id}")
        if not instance_id:
            raise DevicePoolError(f"Device pool item {device_id} is missing instance_id.")
        if enabled and adb_endpoint is None and settings.aliyun.access_key_id is None:
            raise DevicePoolError(
                f"Enabled device {device_id} is missing adb_endpoint, and no Alibaba Cloud AccessKey is configured."
            )

        seen.add(device_id)
        devices.append(
            DeviceTarget(
                device_id=device_id,
                instance_id=instance_id,
                adb_endpoint=adb_endpoint,
                enabled=enabled,
            )
        )

    return DevicePool(devices)


def resolve_execution_devices(
    settings: AppSettings,
    *,
    requested_device_ids: list[str] | None = None,
    legacy_instance_id: str | None = None,
    default_to_all_pool_devices: bool = False,
) -> list[DeviceTarget]:
    pool = load_device_pool(settings)
    enabled_devices = pool.enabled_devices()

    if requested_device_ids:
        return pool.select(requested_device_ids)

    if legacy_instance_id:
        return [_build_transient_device(settings, instance_id=legacy_instance_id)]

    if enabled_devices:
        if default_to_all_pool_devices:
            return enabled_devices
        return [enabled_devices[0]]

    return [_build_transient_device(settings, instance_id=None)]


def _build_transient_device(settings: AppSettings, *, instance_id: str | None) -> DeviceTarget:
    resolved_instance_id = instance_id or _pick_default_instance_id(settings)
    return DeviceTarget(
        device_id=resolved_instance_id,
        instance_id=resolved_instance_id,
        adb_endpoint=settings.device.manual_adb_endpoint,
        enabled=True,
    )


def _pick_default_instance_id(settings: AppSettings) -> str:
    if not settings.instance_ids:
        raise DevicePoolError("No instance configured. Set WUYING_INSTANCE_IDS or use config/device_pool.json.")
    return settings.instance_ids[0]


__all__ = [
    "DevicePool",
    "DevicePoolError",
    "DeviceTarget",
    "load_device_pool",
    "resolve_execution_devices",
]
