"""Console entry point: run the adoption shim on stdin/stdout.

Example:

    python -m jcode_bridge.acp_shim --upstream kcode acp \\
        --session-dir ~/.jcode/sessions --socket ~/.jcode/scratch/jcode.sock

Nothing here starts a daemon. ``--upstream`` names the ACP front end, and the
gate refuses any ``session/load`` without an already-live daemon socket.
"""

from __future__ import annotations

import argparse
import os
from typing import List, Optional

from .capabilities import MODE_READ_ONLY
from .runtime import run, stdio_upstream_factory
from .server import ShimConfig


def _split(values: Optional[List[str]]) -> tuple:
    if not values:
        return ()
    return tuple(value for value in values if value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jcode-acp-adopt",
        description="ACP adoption shim for an existing jcode/KCode session.",
    )
    parser.add_argument(
        "--upstream",
        nargs="+",
        default=["kcode", "acp"],
        metavar="ARGV",
        help="ACP front end argv (default: kcode acp). Not a daemon.",
    )
    parser.add_argument(
        "--session-dir",
        action="append",
        default=[],
        help="Session store to search. Repeatable. Defaults to ~/.jcode/sessions.",
    )
    parser.add_argument(
        "--socket",
        action="append",
        default=[],
        help="Daemon socket to probe. Repeatable. Never created.",
    )
    parser.add_argument(
        "--mode",
        default=MODE_READ_ONLY,
        choices=[MODE_READ_ONLY],
        help="Advertised mode. The shim supports read-only only.",
    )
    parser.add_argument(
        "--forward-mode",
        action="store_true",
        help="Also forward session/set_mode, degrading on upstream -32601.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Upstream request timeout in seconds (default: 60).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = ShimConfig(
        upstream_argv=tuple(args.upstream),
        session_dirs=_split(args.session_dir),
        socket_paths=_split(args.socket),
        mode=args.mode,
        forward_mode=bool(args.forward_mode),
        upstream_timeout=args.timeout,
    )
    os.environ["JCODE_ACP_SHIM"] = "1"
    return run(config=config, upstream_factory=stdio_upstream_factory)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
