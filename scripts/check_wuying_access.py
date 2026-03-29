from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wuying.aliyun_api import WuyingApiClient
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging


def main() -> int:
    configure_logging()
    settings = AppSettings.from_env()
    client = WuyingApiClient(settings.aliyun)
    instances = client.list_instances(max_results=20)

    payload = {
        "region_id": settings.aliyun.region_id,
        "endpoint": settings.aliyun.endpoint or f"eds-aic.{settings.aliyun.region_id}.aliyuncs.com",
        "configured_instance_ids": settings.instance_ids,
        "visible_instances": instances,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
