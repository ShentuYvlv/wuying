from __future__ import annotations

import logging
import time
from functools import cached_property
from typing import Any

from wuying.config import AliyunSettings
from wuying.models import AdbEndpoint

logger = logging.getLogger(__name__)


class WuyingApiError(RuntimeError):
    pass


class WuyingApiClient:
    def __init__(self, settings: AliyunSettings) -> None:
        self.settings = settings

    @cached_property
    def _models(self) -> Any:
        try:
            from alibabacloud_eds_aic20230930 import models as eds_models
        except ImportError as exc:
            raise WuyingApiError(
                "Missing Alibaba Cloud SDK. Install requirements.txt first."
            ) from exc
        return eds_models

    @cached_property
    def client(self) -> Any:
        if not self.settings.access_key_id or not self.settings.access_key_secret:
            raise WuyingApiError(
                "Alibaba Cloud AccessKey is not configured. This is fine in manual ADB mode, "
                "but API-driven operations require ALIBABA_CLOUD_ACCESS_KEY_ID and "
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET."
            )
        try:
            from alibabacloud_eds_aic20230930.client import Client as EDSAICClient
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError as exc:
            raise WuyingApiError(
                "Missing Alibaba Cloud SDK. Install requirements.txt first."
            ) from exc

        config = open_api_models.Config(
            access_key_id=self.settings.access_key_id,
            access_key_secret=self.settings.access_key_secret,
        )
        config.endpoint = self.settings.endpoint or f"eds-aic.{self.settings.region_id}.aliyuncs.com"
        return EDSAICClient(config)

    def _unwrap_body(self, response: Any) -> Any:
        return getattr(response, "body", response)

    def _first_attr(self, obj: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    def describe_instance(self, instance_id: str) -> dict[str, Any]:
        request = self._models.DescribeAndroidInstancesRequest(
            android_instance_ids=[instance_id],
            max_results=1,
            biz_region_id=self.settings.region_id,
        )
        body = self._unwrap_body(self.client.describe_android_instances(request))
        items = self._first_attr(body, "instance_model", "InstanceModel", default=[]) or []
        if not items:
            raise WuyingApiError(
                f"Instance not found: {instance_id} in region {self.settings.region_id}. "
                "Check WUYING_REGION_ID, confirm the instance ID is correct, and make sure the "
                "AccessKey belongs to the same Alibaba Cloud account that owns the instance."
            )

        model = items[0]
        return {
            "instance_id": self._first_attr(model, "android_instance_id", "AndroidInstanceId"),
            "status": self._first_attr(model, "android_instance_status", "AndroidInstanceStatus"),
            "key_pair_id": self._first_attr(model, "key_pair_id", "KeyPairId"),
            "region_id": self._first_attr(model, "region_id", "RegionId"),
            "network_type": self._first_attr(model, "network_type", "NetworkType"),
        }

    def list_instances(self, *, max_results: int = 20) -> list[dict[str, Any]]:
        request = self._models.DescribeAndroidInstancesRequest(
            max_results=max_results,
            biz_region_id=self.settings.region_id,
        )
        body = self._unwrap_body(self.client.describe_android_instances(request))
        items = self._first_attr(body, "instance_model", "InstanceModel", default=[]) or []
        results: list[dict[str, Any]] = []
        for model in items:
            results.append(
                {
                    "instance_id": self._first_attr(model, "android_instance_id", "AndroidInstanceId"),
                    "instance_name": self._first_attr(model, "android_instance_name", "AndroidInstanceName"),
                    "status": self._first_attr(model, "android_instance_status", "AndroidInstanceStatus"),
                    "region_id": self._first_attr(model, "region_id", "RegionId"),
                    "key_pair_id": self._first_attr(model, "key_pair_id", "KeyPairId"),
                }
            )
        return results

    def start_instance_if_needed(self, instance_id: str, *, timeout_seconds: int = 300) -> None:
        info = self.describe_instance(instance_id)
        status = str(info["status"]).upper()
        if status == "RUNNING":
            return

        if status not in {"STOPPED", "BACKUP_FAILED", "RECOVER_FAILED"}:
            raise WuyingApiError(
                f"Instance {instance_id} is not in a startable state. Current status: {status}"
            )

        logger.info("Starting instance %s", instance_id)
        request = self._models.StartAndroidInstanceRequest(android_instance_ids=[instance_id])
        self.client.start_android_instance(request)
        self.wait_for_instance_status(instance_id, "RUNNING", timeout_seconds=timeout_seconds)

    def wait_for_instance_status(
        self,
        instance_id: str,
        expected_status: str,
        *,
        timeout_seconds: int,
        poll_interval: int = 5,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        expected = expected_status.upper()
        while time.monotonic() < deadline:
            status = str(self.describe_instance(instance_id)["status"]).upper()
            if status == expected:
                return
            logger.info("Waiting for instance %s to become %s, current=%s", instance_id, expected, status)
            time.sleep(poll_interval)
        raise WuyingApiError(f"Timed out waiting for instance {instance_id} to become {expected}.")

    def attach_key_pair_if_needed(self, instance_id: str) -> None:
        if not self.settings.key_pair_id:
            return

        info = self.describe_instance(instance_id)
        current_key_pair_id = info["key_pair_id"]
        if current_key_pair_id == self.settings.key_pair_id:
            return

        if not self.settings.auto_attach_key_pair:
            raise WuyingApiError(
                f"Instance {instance_id} is not bound to key pair {self.settings.key_pair_id}. "
                "Set WUYING_AUTO_ATTACH_KEY_PAIR=true or bind it manually."
            )

        logger.info("Attaching key pair %s to %s", self.settings.key_pair_id, instance_id)
        request = self._models.AttachKeyPairRequest(
            key_pair_id=self.settings.key_pair_id,
            instance_ids=[instance_id],
        )
        self.client.attach_key_pair(request)

    def start_instance_adb(self, instance_id: str) -> None:
        logger.info("Starting ADB for instance %s", instance_id)
        request = self._models.StartInstanceAdbRequest(instance_ids=[instance_id])
        try:
            self.client.start_instance_adb(request)
        except Exception as exc:
            raise WuyingApiError(
                "Failed to call StartInstanceAdb. If you already enabled ADB in the console, "
                "set WUYING_START_ADB_VIA_API=false or fill WUYING_MANUAL_ADB_ENDPOINT=host:port "
                "to bypass the API call."
            ) from exc

    def get_adb_endpoint(self, instance_id: str) -> AdbEndpoint:
        request = self._models.ListInstanceAdbAttributesRequest(instance_ids=[instance_id], max_results=10)
        body = self._unwrap_body(self.client.list_instance_adb_attributes(request))
        items = self._first_attr(body, "data", "Data", default=[]) or []
        if not items:
            raise WuyingApiError(f"No ADB endpoint returned for instance {instance_id}.")

        item = items[0]
        external_ip = self._first_attr(item, "external_ip", "ExternalIp")
        external_port = self._first_attr(item, "external_port", "ExternalPort")
        internal_ip = self._first_attr(item, "internal_ip", "InternalIp")
        internal_port = self._first_attr(item, "internal_port", "InternalPort")

        if external_ip and external_port:
            return AdbEndpoint(
                instance_id=instance_id,
                host=external_ip,
                port=self._extract_first_port(external_port),
                source="external",
            )

        if internal_ip and internal_port:
            return AdbEndpoint(
                instance_id=instance_id,
                host=internal_ip,
                port=self._extract_first_port(internal_port),
                source="internal",
            )

        raise WuyingApiError(f"ADB endpoint fields are empty for instance {instance_id}.")

    def ensure_adb_ready(self, instance_id: str, *, timeout_seconds: int) -> AdbEndpoint:
        manual_endpoint = self._parse_manual_adb_endpoint(instance_id)
        if manual_endpoint is not None:
            logger.info("Using manual ADB endpoint for %s via %s", instance_id, manual_endpoint.serial)
            return manual_endpoint

        self.start_instance_if_needed(instance_id, timeout_seconds=timeout_seconds)
        self.attach_key_pair_if_needed(instance_id)

        if self.settings.start_adb_via_api:
            self.start_instance_adb(instance_id)
        else:
            logger.info("Skipping StartInstanceAdb due to WUYING_START_ADB_VIA_API=false")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                endpoint = self.get_adb_endpoint(instance_id)
            except WuyingApiError:
                time.sleep(3)
                continue
            logger.info("ADB ready for %s via %s", instance_id, endpoint.serial)
            return endpoint

        raise WuyingApiError(f"Timed out waiting for ADB endpoint for instance {instance_id}.")

    @staticmethod
    def _extract_first_port(raw: str) -> int:
        head = str(raw).split("/")[0].strip()
        return int(head)

    def _parse_manual_adb_endpoint(self, instance_id: str) -> AdbEndpoint | None:
        raw = getattr(self.settings, "manual_adb_endpoint", None)
        if not raw:
            return None
        if ":" not in raw:
            raise WuyingApiError(
                "WUYING_MANUAL_ADB_ENDPOINT must use host:port format, for example 1.2.3.4:5555"
            )
        host, port_text = raw.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError as exc:
            raise WuyingApiError("WUYING_MANUAL_ADB_ENDPOINT port must be an integer.") from exc
        return AdbEndpoint(instance_id=instance_id, host=host.strip(), port=port, source="manual")
