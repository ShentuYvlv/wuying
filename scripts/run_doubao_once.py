from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wuying.config import AppSettings
from wuying.logging_utils import configure_logging
from wuying.workflows import DoubaoWorkflow


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Doubao task on a single Wuying cloud phone instance.")
    parser.add_argument("--instance-id", help="Override one instance ID from .env")
    parser.add_argument("--prompt", required=True, help="Prompt sent to Doubao")
    args = parser.parse_args()

    configure_logging()
    settings = AppSettings.from_env()
    instance_id = args.instance_id or _pick_default_instance(settings)

    workflow = DoubaoWorkflow(settings)
    result = workflow.run_once(instance_id=instance_id, prompt=args.prompt)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def _pick_default_instance(settings: AppSettings) -> str:
    if not settings.instance_ids:
        raise ValueError("No instance configured. Set WUYING_INSTANCE_IDS or pass --instance-id.")
    return settings.instance_ids[0]


if __name__ == "__main__":
    raise SystemExit(main())
