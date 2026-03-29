from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wuying.models import DoubaoSelectors, SelectorSpec


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_optional(name, str(default)).lower()
    return raw in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get_optional(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_csv(name: str) -> list[str]:
    raw = _get_optional(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_wuying_endpoint(region_id: str, explicit_endpoint: str) -> str:
    if explicit_endpoint:
        return explicit_endpoint

    # Per the official eds-aic endpoint document, the public control-plane
    # endpoints currently exposed are cn-shanghai and ap-southeast-1.
    # Mainland China business regions such as cn-hangzhou still use the
    # Shanghai endpoint together with BizRegionId.
    if region_id == "ap-southeast-1":
        return "eds-aic.ap-southeast-1.aliyuncs.com"
    return "eds-aic.cn-shanghai.aliyuncs.com"


def _parse_selectors(name: str, fallback: list[SelectorSpec]) -> list[SelectorSpec]:
    raw = _get_optional(name)
    if not raw:
        return fallback

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON.") from exc

    if not isinstance(items, list):
        raise ValueError(f"{name} must be a JSON list.")

    selectors: list[SelectorSpec] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{name} must contain JSON objects only.")
        selectors.append(SelectorSpec.from_mapping(item))
    return selectors


@dataclass(slots=True)
class AliyunSettings:
    access_key_id: str | None
    access_key_secret: str | None
    region_id: str
    endpoint: str
    key_pair_id: str | None
    auto_attach_key_pair: bool


@dataclass(slots=True)
class DeviceSettings:
    adb_path: str
    adb_vendor_keys: str | None
    adb_connect_timeout_seconds: int
    adb_ready_timeout_seconds: int
    start_adb_via_api: bool
    manual_adb_endpoint: str | None


@dataclass(slots=True)
class DoubaoSettings:
    package_name: str
    launch_activity: str | None
    response_timeout_seconds: int
    response_settle_seconds: int
    output_dir: Path
    selectors: DoubaoSelectors


@dataclass(slots=True)
class AppSettings:
    aliyun: AliyunSettings
    device: DeviceSettings
    doubao: DoubaoSettings
    instance_ids: list[str]

    @classmethod
    def from_env(cls) -> "AppSettings":
        _load_dotenv_if_present()
        region_id = _get_optional("WUYING_REGION_ID", "cn-shanghai")
        explicit_endpoint = _get_optional("WUYING_ENDPOINT")
        aliyun_endpoint = _resolve_wuying_endpoint(region_id, explicit_endpoint)
        manual_adb_endpoint = _get_optional("WUYING_MANUAL_ADB_ENDPOINT") or None
        access_key_id = _get_optional("ALIBABA_CLOUD_ACCESS_KEY_ID") or None
        access_key_secret = _get_optional("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or None

        if not manual_adb_endpoint:
            if not access_key_id:
                raise ValueError("Missing required environment variable: ALIBABA_CLOUD_ACCESS_KEY_ID")
            if not access_key_secret:
                raise ValueError("Missing required environment variable: ALIBABA_CLOUD_ACCESS_KEY_SECRET")

        input_defaults = [
            SelectorSpec(text_contains="问点什么"),
            SelectorSpec(description_contains="问点什么"),
            SelectorSpec(class_name="android.widget.EditText"),
        ]
        enter_chat_defaults = [
            SelectorSpec(resource_id="com.larus.nova:id/right_img", description_contains="创建新对话"),
            SelectorSpec(text="聊聊新话题"),
            SelectorSpec(text="豆包"),
        ]
        new_chat_defaults = [
            SelectorSpec(resource_id="com.larus.nova:id/right_img", description_contains="创建新对话"),
            SelectorSpec(text="聊聊新话题"),
        ]
        chat_back_defaults = [
            SelectorSpec(resource_id="com.larus.nova:id/back_icon", description="返回"),
            SelectorSpec(resource_id="com.larus.nova:id/back_icon"),
        ]
        switch_to_text_input_defaults = [
            SelectorSpec(resource_id="com.larus.nova:id/action_input", description="文本输入"),
            SelectorSpec(resource_id="com.larus.nova:id/action_input"),
            SelectorSpec(text="按住说话"),
        ]
        send_defaults = [
            SelectorSpec(description="发送"),
            SelectorSpec(text="发送"),
        ]

        return cls(
            aliyun=AliyunSettings(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                region_id=region_id,
                endpoint=aliyun_endpoint,
                key_pair_id=_get_optional("WUYING_KEY_PAIR_ID") or None,
                auto_attach_key_pair=_get_bool("WUYING_AUTO_ATTACH_KEY_PAIR", False),
            ),
            device=DeviceSettings(
                adb_path=_get_optional("ADB_PATH", "adb"),
                adb_vendor_keys=_get_optional("ADB_VENDOR_KEYS") or None,
                adb_connect_timeout_seconds=_get_int("ADB_CONNECT_TIMEOUT_SECONDS", 30),
                adb_ready_timeout_seconds=_get_int("ADB_READY_TIMEOUT_SECONDS", 120),
                start_adb_via_api=_get_bool("WUYING_START_ADB_VIA_API", True),
                manual_adb_endpoint=manual_adb_endpoint,
            ),
            doubao=DoubaoSettings(
                package_name=_get_optional("DOUBAO_PACKAGE_NAME", "com.larus.nova"),
                launch_activity=_get_optional("DOUBAO_LAUNCH_ACTIVITY") or None,
                response_timeout_seconds=_get_int("DOUBAO_RESPONSE_TIMEOUT_SECONDS", 120),
                response_settle_seconds=_get_int("DOUBAO_RESPONSE_SETTLE_SECONDS", 4),
                output_dir=Path(_get_optional("DOUBAO_OUTPUT_DIR", "data/runs")),
                selectors=DoubaoSelectors(
                    new_chat_selectors=_parse_selectors(
                        "DOUBAO_NEW_CHAT_SELECTORS_JSON",
                        new_chat_defaults,
                    ),
                    chat_back_selectors=_parse_selectors(
                        "DOUBAO_CHAT_BACK_SELECTORS_JSON",
                        chat_back_defaults,
                    ),
                    enter_chat_selectors=_parse_selectors(
                        "DOUBAO_ENTER_CHAT_SELECTORS_JSON",
                        enter_chat_defaults,
                    ),
                    switch_to_text_input_selectors=_parse_selectors(
                        "DOUBAO_SWITCH_TO_TEXT_INPUT_SELECTORS_JSON",
                        switch_to_text_input_defaults,
                    ),
                    input_selectors=_parse_selectors("DOUBAO_INPUT_SELECTORS_JSON", input_defaults),
                    send_selectors=_parse_selectors("DOUBAO_SEND_SELECTORS_JSON", send_defaults),
                    response_selectors=_parse_selectors("DOUBAO_RESPONSE_SELECTORS_JSON", []),
                ),
            ),
            instance_ids=_get_csv("WUYING_INSTANCE_IDS"),
        )
