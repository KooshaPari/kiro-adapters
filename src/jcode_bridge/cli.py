"""CLI for the jcode backend bridge.

Mirrors the MCP tool surface so the same calls can be made from a shell, a cron
job, or another agent that only knows how to run commands.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import __version__
from .api import JcodeApiBackend
from .backend import JcodeBackend


def _emit(result, as_json: bool) -> int:
    if as_json:
        sys.stdout.write(result.to_json(indent=2) + "\n")
    else:
        sys.stdout.write(result.summary() + "\n")
        answer = (result.data or {}).get("answer")
        if answer:
            sys.stdout.write(str(answer) + "\n")
        for hint in result.hints:
            sys.stdout.write("hint: {}\n".format(hint))
    return 0 if result.ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jcode-bridge",
        description="Expose jcode as a task backend (sync run, detached tasks).",
    )
    parser.add_argument("--json", action="store_true", help="emit the full result envelope")
    parser.add_argument("--version", action="version", version="jcode-bridge {}".format(__version__))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="jcode binary, version and task home")
    sub.add_parser("list", help="recent detached tasks").add_argument("--limit", type=int, default=20)

    api_health = sub.add_parser(
        "api-health", help="check the jcode harness API (handshake and ping)"
    )
    api_health.add_argument("--timeout", type=float, default=30.0)

    api_ask = sub.add_parser(
        "api-ask", help="run one turn over a live jcode harness-API session"
    )
    api_ask.add_argument("prompt", help="prompt text")
    api_ask.add_argument("--cwd", help="working directory")
    api_ask.add_argument("--timeout", type=float, default=900.0, help="seconds")
    api_ask.add_argument(
        "--keep-session", action="store_true", help="keep the session for follow-ups"
    )

    api_sessions = sub.add_parser(
        "api-sessions", help="list sessions visible to the harness API"
    )
    api_sessions.add_argument("--limit", type=int, default=20)
    api_sessions.add_argument("--timeout", type=float, default=30.0)

    def add_task_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("prompt", help="task text")
        target.add_argument("--provider", help="provider name")
        target.add_argument("--model", help="model override")
        target.add_argument("--tools", help="comma-separated tool allowlist")
        target.add_argument("--disabled-tools", help="comma-separated tool denylist")
        target.add_argument("--cwd", help="working directory")
        target.add_argument("--timeout", type=float, default=900.0, help="seconds")

    add_task_flags(sub.add_parser("run", help="run one task synchronously"))
    add_task_flags(sub.add_parser("start", help="start a detached task"))

    sub.add_parser("status", help="state of a detached task").add_argument("task_id")
    sub.add_parser("cancel", help="cancel a detached task").add_argument("task_id")
    output = sub.add_parser("output", help="output of a detached task")
    output.add_argument("task_id")
    output.add_argument("--tail-chars", type=int, default=20000)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    backend = JcodeBackend()
    api = JcodeApiBackend()

    if args.command == "health":
        return _emit(backend.health(), args.json)
    if args.command == "list":
        return _emit(backend.list_tasks(args.limit), args.json)
    if args.command == "status":
        return _emit(backend.status(args.task_id), args.json)
    if args.command == "cancel":
        return _emit(backend.cancel(args.task_id), args.json)
    if args.command == "output":
        return _emit(backend.output(args.task_id, args.tail_chars), args.json)

    if args.command == "api-health":
        return _emit(api.health(timeout=args.timeout), args.json)
    if args.command == "api-ask":
        return _emit(
            api.ask(
                args.prompt,
                working_dir=args.cwd,
                timeout=args.timeout,
                keep_session=args.keep_session,
            ),
            args.json,
        )
    if args.command == "api-sessions":
        return _emit(
            api.sessions(limit=args.limit, timeout=args.timeout), args.json
        )

    common = dict(
        provider=args.provider,
        model=args.model,
        tools=args.tools,
        disabled_tools=args.disabled_tools,
        cwd=args.cwd,
        timeout=args.timeout,
    )
    if args.command == "run":
        return _emit(backend.run(args.prompt, **common), args.json)
    if args.command == "start":
        return _emit(backend.start(args.prompt, **common), args.json)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
