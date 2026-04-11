from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wuying.application.platform_registry import PLATFORM_REGISTRY, available_platform_names, get_platform_definition
from wuying.application.runner import pick_default_instance, run_platform_once
from wuying.config import AppSettings
from wuying.logging_utils import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run prompts on selected mobile platform workflows.")
    parser.add_argument(
        "--platform",
        required=True,
        help=f"Platform name, or comma-separated names. Available: {', '.join(available_platform_names())}",
    )
    parser.add_argument("--instance-id", help="Override one instance ID from .env")
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt", help="Prompt sent to the selected app")
    prompt_source.add_argument("--file", help="UTF-8 text file. One non-empty line is one prompt.")
    return parser


def _parse_platforms(raw: str) -> list[str]:
    platforms = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not platforms:
        raise ValueError("No platform configured.")

    for platform in platforms:
        get_platform_definition(platform)
    return platforms


def _load_prompts(args: argparse.Namespace) -> list[str]:
    if args.prompt is not None:
        prompt = args.prompt.strip()
        if not prompt:
            raise ValueError("--prompt cannot be empty.")
        return [prompt]

    prompt_file = Path(args.file)
    prompts = [
        line.strip()
        for line in prompt_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not prompts:
        raise ValueError(f"No prompt found in file: {prompt_file}")
    return prompts


def run_from_cli(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        platforms = _parse_platforms(args.platform)
        prompts = _load_prompts(args)
    except Exception as exc:
        parser.error(str(exc))

    configure_logging()
    settings = AppSettings.from_env()
    instance_id = args.instance_id or pick_default_instance(settings)
    print(
        json.dumps(
            {
                "platforms": platforms,
                "descriptions": {
                    platform: PLATFORM_REGISTRY[platform].description
                    for platform in platforms
                },
                "instance_id": instance_id,
                "manual_adb_endpoint": settings.device.manual_adb_endpoint,
                "adb_path": settings.device.adb_path,
                "prompt_count": len(prompts),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    results = []
    for platform in platforms:
        for prompt in prompts:
            result = run_platform_once(
                settings=settings,
                platform_name=platform,
                prompt=prompt,
                instance_id=instance_id,
            )
            results.append(result.to_dict())

    if len(results) == 1:
        print(json.dumps(results[0], ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0
