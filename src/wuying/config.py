from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wuying.models import ChatAppSelectors, SelectorSpec


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


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


def _build_chat_app_settings(
    *,
    env_prefix: str,
    platform_name: str,
    default_package_name: str,
    default_launch_activity: str | None,
    default_output_dir: str,
    default_response_timeout_seconds: int,
    default_response_settle_seconds: int,
    default_message_list_resource_id: str | None,
    default_selectors: ChatAppSelectors,
) -> "ChatAppSettings":
    prefix = env_prefix.upper()
    return ChatAppSettings(
        platform_name=platform_name,
        package_name=_get_optional(f"{prefix}_PACKAGE_NAME", default_package_name),
        launch_activity=_get_optional(f"{prefix}_LAUNCH_ACTIVITY", default_launch_activity or "") or None,
        response_timeout_seconds=_get_int(
            f"{prefix}_RESPONSE_TIMEOUT_SECONDS",
            default_response_timeout_seconds,
        ),
        response_settle_seconds=_get_int(
            f"{prefix}_RESPONSE_SETTLE_SECONDS",
            default_response_settle_seconds,
        ),
        message_list_resource_id=_get_optional(
            f"{prefix}_MESSAGE_LIST_RESOURCE_ID",
            default_message_list_resource_id or "",
        )
        or None,
        output_dir=Path(_get_optional(f"{prefix}_OUTPUT_DIR", default_output_dir)),
        selectors=ChatAppSelectors(
            new_chat_selectors=_parse_selectors(
                f"{prefix}_NEW_CHAT_SELECTORS_JSON",
                default_selectors.new_chat_selectors,
            ),
            chat_back_selectors=_parse_selectors(
                f"{prefix}_CHAT_BACK_SELECTORS_JSON",
                default_selectors.chat_back_selectors,
            ),
            enter_chat_selectors=_parse_selectors(
                f"{prefix}_ENTER_CHAT_SELECTORS_JSON",
                default_selectors.enter_chat_selectors,
            ),
            switch_to_text_input_selectors=_parse_selectors(
                f"{prefix}_SWITCH_TO_TEXT_INPUT_SELECTORS_JSON",
                default_selectors.switch_to_text_input_selectors,
            ),
            reference_expand_selectors=_parse_selectors(
                f"{prefix}_REFERENCE_EXPAND_SELECTORS_JSON",
                default_selectors.reference_expand_selectors,
            ),
            input_selectors=_parse_selectors(
                f"{prefix}_INPUT_SELECTORS_JSON",
                default_selectors.input_selectors,
            ),
            send_selectors=_parse_selectors(
                f"{prefix}_SEND_SELECTORS_JSON",
                default_selectors.send_selectors,
            ),
            response_selectors=_parse_selectors(
                f"{prefix}_RESPONSE_SELECTORS_JSON",
                default_selectors.response_selectors,
            ),
        ),
    )


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
class ChatAppSettings:
    platform_name: str
    package_name: str
    launch_activity: str | None
    response_timeout_seconds: int
    response_settle_seconds: int
    message_list_resource_id: str | None
    output_dir: Path
    selectors: ChatAppSelectors


@dataclass(slots=True)
class AppSettings:
    aliyun: AliyunSettings
    device: DeviceSettings
    doubao: ChatAppSettings
    deepseek: ChatAppSettings
    kimi: ChatAppSettings
    qianwen: ChatAppSettings
    yuanbao: ChatAppSettings
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

        doubao_defaults = ChatAppSelectors(
            new_chat_selectors=[
                SelectorSpec(resource_id="com.larus.nova:id/right_img", description_contains="创建新对话"),
                SelectorSpec(text="聊聊新话题"),
            ],
            chat_back_selectors=[
                SelectorSpec(resource_id="com.larus.nova:id/back_icon", description="返回"),
                SelectorSpec(resource_id="com.larus.nova:id/back_icon"),
            ],
            enter_chat_selectors=[
                SelectorSpec(resource_id="com.larus.nova:id/right_img", description_contains="创建新对话"),
                SelectorSpec(text="聊聊新话题"),
                SelectorSpec(text="豆包"),
            ],
            switch_to_text_input_selectors=[
                SelectorSpec(resource_id="com.larus.nova:id/action_input", description="文本输入"),
                SelectorSpec(resource_id="com.larus.nova:id/action_input"),
                SelectorSpec(text="按住说话"),
            ],
            reference_expand_selectors=[
                SelectorSpec(resource_id="com.larus.nova:id/ll_reference_title"),
                SelectorSpec(resource_id="com.larus.nova:id/tv_reference_title"),
                SelectorSpec(text_contains="关键词"),
                SelectorSpec(text_contains="资料"),
                SelectorSpec(description_contains="关键词"),
                SelectorSpec(description_contains="资料"),
            ],
            input_selectors=[
                SelectorSpec(text_contains="问点什么"),
                SelectorSpec(description_contains="问点什么"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            send_selectors=[
                SelectorSpec(description="发送"),
                SelectorSpec(text="发送"),
            ],
            response_selectors=[],
        )

        deepseek_defaults = ChatAppSelectors(
            new_chat_selectors=[
                SelectorSpec(description_contains="开启新对话"),
                SelectorSpec(description_contains="新对话"),
                SelectorSpec(text="新对话"),
            ],
            chat_back_selectors=[
                SelectorSpec(description="返回"),
                SelectorSpec(text="返回"),
            ],
            enter_chat_selectors=[
                SelectorSpec(class_name="android.widget.EditText"),
                SelectorSpec(text_contains="新对话"),
                SelectorSpec(description_contains="新对话"),
            ],
            switch_to_text_input_selectors=[
                SelectorSpec(description_contains="文本输入"),
                SelectorSpec(text_contains="文本输入"),
                SelectorSpec(text_contains="按住说话"),
            ],
            reference_expand_selectors=[
                SelectorSpec(text_contains="关键词"),
                SelectorSpec(text_contains="资料"),
                SelectorSpec(description_contains="关键词"),
                SelectorSpec(description_contains="资料"),
            ],
            input_selectors=[
                SelectorSpec(class_name="android.widget.EditText"),
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(description_contains="发消息"),
            ],
            send_selectors=[
                SelectorSpec(description="发送"),
                SelectorSpec(description_contains="发送"),
                SelectorSpec(text="发送"),
                SelectorSpec(text_contains="发送"),
            ],
            response_selectors=[],
        )

        kimi_defaults = ChatAppSelectors(
            new_chat_selectors=[
                SelectorSpec(description="开启新会话"),
                SelectorSpec(description_contains="开启新会话"),
                SelectorSpec(text="新会话"),
            ],
            chat_back_selectors=[
                SelectorSpec(description="导航按钮"),
                SelectorSpec(description_contains="返回"),
            ],
            enter_chat_selectors=[
                SelectorSpec(class_name="android.widget.EditText"),
                SelectorSpec(text="Kimi"),
            ],
            switch_to_text_input_selectors=[
                SelectorSpec(description="切换至文字输入"),
                SelectorSpec(description_contains="切换至文字输入"),
            ],
            reference_expand_selectors=[],
            input_selectors=[
                SelectorSpec(text_contains="尽管问"),
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            send_selectors=[
                SelectorSpec(description="发送讯息"),
                SelectorSpec(description_contains="发送讯息"),
                SelectorSpec(description="发送消息"),
                SelectorSpec(description_contains="发送消息"),
            ],
            response_selectors=[],
        )

        qianwen_defaults = ChatAppSelectors(
            new_chat_selectors=[
                SelectorSpec(description_contains="新建"),
                SelectorSpec(description_contains="新对话"),
                SelectorSpec(text_contains="新对话"),
            ],
            chat_back_selectors=[
                SelectorSpec(description_contains="返回"),
                SelectorSpec(text="返回"),
            ],
            enter_chat_selectors=[
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            switch_to_text_input_selectors=[
                SelectorSpec(description_contains="文本输入"),
                SelectorSpec(text_contains="按住说话"),
            ],
            reference_expand_selectors=[],
            input_selectors=[
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            send_selectors=[
                SelectorSpec(description="发送"),
                SelectorSpec(description_contains="发送"),
                SelectorSpec(text="发送"),
                SelectorSpec(text_contains="发送"),
            ],
            response_selectors=[],
        )

        yuanbao_defaults = ChatAppSelectors(
            new_chat_selectors=[
                SelectorSpec(text="新建对话"),
                SelectorSpec(text_contains="新建"),
                SelectorSpec(text_contains="新对话"),
            ],
            chat_back_selectors=[
                SelectorSpec(resource_id="ic_navigation_show"),
                SelectorSpec(description_contains="返回"),
                SelectorSpec(text="返回"),
            ],
            enter_chat_selectors=[
                SelectorSpec(resource_id="com.tencent.hunyuan.app.chat:id/edConversationInput"),
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            switch_to_text_input_selectors=[
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(description_contains="文本输入"),
            ],
            reference_expand_selectors=[],
            input_selectors=[
                SelectorSpec(resource_id="com.tencent.hunyuan.app.chat:id/edConversationInput"),
                SelectorSpec(text_contains="发消息"),
                SelectorSpec(text_contains="按住说话"),
                SelectorSpec(class_name="android.widget.EditText"),
            ],
            send_selectors=[
                SelectorSpec(resource_id="com.tencent.hunyuan.app.chat:id/fl_slot_send_stop"),
                SelectorSpec(description="发送"),
                SelectorSpec(description_contains="发送"),
                SelectorSpec(text="发送"),
                SelectorSpec(text_contains="发送"),
            ],
            response_selectors=[],
        )

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
            doubao=_build_chat_app_settings(
                env_prefix="DOUBAO",
                platform_name="doubao",
                default_package_name="com.larus.nova",
                default_launch_activity=None,
                default_output_dir="data/runs",
                default_response_timeout_seconds=120,
                default_response_settle_seconds=4,
                default_message_list_resource_id="com.larus.nova:id/message_list",
                default_selectors=doubao_defaults,
            ),
            deepseek=_build_chat_app_settings(
                env_prefix="DEEPSEEK",
                platform_name="deepseek",
                default_package_name="com.deepseek.chat",
                default_launch_activity="com.deepseek.chat.MainActivity",
                default_output_dir="data/runs",
                default_response_timeout_seconds=120,
                default_response_settle_seconds=4,
                default_message_list_resource_id=None,
                default_selectors=deepseek_defaults,
            ),
            kimi=_build_chat_app_settings(
                env_prefix="KIMI",
                platform_name="kimi",
                default_package_name="com.moonshot.kimichat",
                default_launch_activity="com.moonshot.kimichat.MainActivity",
                default_output_dir="data/runs",
                default_response_timeout_seconds=120,
                default_response_settle_seconds=4,
                default_message_list_resource_id=None,
                default_selectors=kimi_defaults,
            ),
            qianwen=_build_chat_app_settings(
                env_prefix="QIANWEN",
                platform_name="qianwen",
                default_package_name="com.aliyun.tongyi",
                default_launch_activity="com.ucpro.MainActivity",
                default_output_dir="data/runs",
                default_response_timeout_seconds=120,
                default_response_settle_seconds=4,
                default_message_list_resource_id=None,
                default_selectors=qianwen_defaults,
            ),
            yuanbao=_build_chat_app_settings(
                env_prefix="YUANBAO",
                platform_name="yuanbao",
                default_package_name="com.tencent.hunyuan.app.chat",
                default_launch_activity="com.tencent.hunyuan.app.chat.home.v2.YBHomeActivityV2",
                default_output_dir="data/runs",
                default_response_timeout_seconds=120,
                default_response_settle_seconds=4,
                default_message_list_resource_id="com.tencent.hunyuan.app.chat:id/chat_recycler_view",
                default_selectors=yuanbao_defaults,
            ),
            instance_ids=_get_csv("WUYING_INSTANCE_IDS"),
        )
