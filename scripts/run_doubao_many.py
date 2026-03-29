from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    parser = argparse.ArgumentParser(description="Run the same Doubao prompt on configured Wuying instances.")
    parser.add_argument("--prompt", required=True, help="Prompt sent to Doubao")
    parser.add_argument("--max-workers", type=int, default=1, help="Parallel workers, recommended 1-5")
    args = parser.parse_args()

    configure_logging()
    settings = AppSettings.from_env()
    if not settings.instance_ids:
        raise ValueError("No instance configured in WUYING_INSTANCE_IDS.")

    workflow = DoubaoWorkflow(settings)
    max_workers = max(1, min(args.max_workers, 5))
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(workflow.run_once, instance_id=instance_id, prompt=args.prompt): instance_id
            for instance_id in settings.instance_ids[:5]
        }
        for future in as_completed(future_map):
            instance_id = future_map[future]
            try:
                result = future.result()
                results.append(asdict(result))
            except Exception as exc:
                results.append({"instance_id": instance_id, "error": str(exc)})

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
