from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import uvicorn

from wuying.interfaces.cli import run_from_cli
from wuying.interfaces.install_apks import run_install_from_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified entrypoint for Wuying crawler.")
    parser.add_argument("command", choices=["app", "api", "install-apks"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        build_parser().print_help()
        return 2

    command = raw_argv[0]
    forwarded = raw_argv[1:]

    if command == "app":
        return run_from_cli(forwarded)

    if command == "install-apks":
        return run_install_from_cli(forwarded)

    if command == "api":
        api_parser = argparse.ArgumentParser(description="Run FastAPI service.")
        api_parser.add_argument("--host", default="0.0.0.0")
        api_parser.add_argument("--port", type=int, default=8000)
        api_parser.add_argument("--reload", action="store_true")
        args = api_parser.parse_args(forwarded)
        uvicorn.run(
            "wuying.interfaces.api:app",
            host=args.host,
            port=args.port,
            reload=bool(args.reload),
        )
        return 0

    build_parser().parse_args(raw_argv)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
