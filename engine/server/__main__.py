"""``python -m engine.server`` — start the visualization server."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from engine.diagnostics.levels import TraceLevel
from engine.server.app import API_PREFIX, create_app
from engine.server.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine.server",
        description="Serve a ChenDB workspace over HTTP and WebSocket.",
    )
    parser.add_argument("--host", default=None, help="bind address")
    parser.add_argument("--port", type=int, default=None, help="bind port")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="directory holding database files (created if missing)",
    )
    parser.add_argument(
        "--trace",
        default=None,
        choices=[level.name.lower() for level in TraceLevel],
        help="trace level new databases open at",
    )
    parser.add_argument(
        "--reload", action="store_true", help="restart on source changes"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            "The visualization server needs the optional server extras:\n"
            "    pip install -e '.[server]'\n"
            "The engine itself has no dependencies and still works: "
            "try `python -m engine`.",
            file=sys.stderr,
        )
        return 1

    config = load_config()
    if args.workspace is not None:
        config = replace(config, workspace=args.workspace)
    if args.host is not None:
        config = replace(config, host=args.host)
    if args.port is not None:
        config = replace(config, port=args.port)
    if args.trace is not None:
        config = replace(config, default_trace_level=TraceLevel[args.trace.upper()])

    print(f"ChenDB server on http://{config.host}:{config.port}{API_PREFIX}")
    print(f"  docs      http://{config.host}:{config.port}{API_PREFIX}/docs")
    print(f"  workspace {config.workspace_path}")

    if args.reload:
        # Reload mode needs an import string, so config comes from the
        # environment in the reloaded child process.
        import os

        os.environ.setdefault("CHENDB_WORKSPACE", str(config.workspace))
        uvicorn.run(
            "engine.server.app:app",
            host=config.host,
            port=config.port,
            reload=True,
        )
    else:
        uvicorn.run(create_app(config), host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
