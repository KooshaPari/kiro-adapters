"""Command line surface for the Kiro adapters.

Every subcommand returns the same :class:`~kiro_adapters.result.AdapterResult`
the MCP tools return, so the shell and an MCP client see identical semantics:
exit code 0 when the result is ok, 1 when it is not, and the same summary and
hints either way.

``--json`` prints the whole result as JSON for machine consumption; the default
human output is the one-line summary, the answer when there is one, and one
``hint:`` line per remediation.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional, Sequence

from . import __version__
from . import result as R
from .kirocode import KiroCodeAdapter
from .kirocrew import KiroCrewAdapter

_CREW: Optional[KiroCrewAdapter] = None
_CODE: Optional[KiroCodeAdapter] = None


def _crew() -> KiroCrewAdapter:
    global _CREW
    if _CREW is None:
        _CREW = KiroCrewAdapter()
    return _CREW


def _code() -> KiroCodeAdapter:
    global _CODE
    if _CODE is None:
        _CODE = KiroCodeAdapter()
    return _CODE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiro-adapter",
        description="Drive Kiro Crew and the Kiro coding CLI from the shell.",
    )
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the full result as JSON")
    parser.add_argument("--version", dest="show_version", action="store_true",
                        help="print the adapter version and exit")

    # The same flag is accepted after the subcommand. SUPPRESS keeps the
    # subparser default from overwriting a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="as_json", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("status", parents=[common],
                   help="gateway, auth mode, KAS engine and agent status")
    sub.add_parser("agents", parents=[common], help="list agent definitions")
    sub.add_parser("doctor", parents=[common], help="run the kirocrew doctor")
    sub.add_parser("kas", parents=[common], help="KAS engine install state")
    sub.add_parser("spawn-list", parents=[common], help="list spawned subagents")
    sub.add_parser("kirocode-status", parents=[common], help="Kiro coding CLI status")

    dashboard = sub.add_parser("dashboard", parents=[common],
                               help="dashboard URL (the token stays redacted)")
    dashboard.add_argument("--ttl", default=None, help="dashboard token TTL")

    ask = sub.add_parser("ask", parents=[common],
                         help="send one message to the Kiro Crew agent")
    ask.add_argument("message", help="message to send")
    ask.add_argument("--agent", default=None, help="agent name")
    ask.add_argument("--model", default=None, help="model override")
    ask.add_argument("--timeout", type=float, default=None, help="timeout in seconds")

    spawn = sub.add_parser("spawn", parents=[common], help="dispatch a Kiro Crew subagent")
    spawn.add_argument("prompt", help="prompt for the subagent")
    spawn.add_argument("--agent", default=None, help="agent name")
    spawn.add_argument("--crew", default=None, help="crew to dispatch into")
    spawn.add_argument("--async", dest="background", action="store_true",
                       help="run without waiting for the subagent")
    spawn.add_argument("--timeout", type=float, default=None, help="timeout in seconds")

    spec = sub.add_parser("run-spec", parents=[common], help="run a spec file")
    spec.add_argument("spec", help="path to the spec file")
    spec.add_argument("--name", default=None, help="run name")
    spec.add_argument("--no-test", dest="no_test", action="store_true",
                      help="skip the test phase")
    spec.add_argument("--timeout", type=float, default=None, help="timeout in seconds")

    kask = sub.add_parser("kirocode-ask", parents=[common],
                          help="send one prompt to the Kiro coding CLI")
    kask.add_argument("prompt", help="prompt to send")
    kask.add_argument("--cwd", default=None, help="working directory")
    kask.add_argument("--timeout", type=float, default=None, help="timeout in seconds")
    return parser


def _timeout_kw(args: argparse.Namespace) -> Dict[str, float]:
    value = getattr(args, "timeout", None)
    return {} if value is None else {"timeout": float(value)}


def _run(args: argparse.Namespace) -> R.AdapterResult:
    command = getattr(args, "command", None)
    if command == "status":
        return _crew().status()
    if command == "agents":
        return _crew().list_agents()
    if command == "doctor":
        return _crew().doctor()
    if command == "kas":
        return _crew().kas_status()
    if command == "spawn-list":
        return _crew().spawn_list()
    if command == "dashboard":
        return _crew().dashboard_url(ttl=getattr(args, "ttl", None))
    if command == "ask":
        return _crew().ask(message=args.message, agent=args.agent, model=args.model,
                           **_timeout_kw(args))
    if command == "spawn":
        return _crew().spawn(prompt=args.prompt, agent=args.agent, crew=args.crew,
                             background=bool(getattr(args, "background", False)),
                             **_timeout_kw(args))
    if command == "run-spec":
        return _crew().run_spec(spec_path=args.spec, name=args.name,
                                no_test=bool(args.no_test), **_timeout_kw(args))
    if command == "kirocode-status":
        return _code().status()
    if command == "kirocode-ask":
        return _code().ask(prompt=args.prompt, cwd=args.cwd, **_timeout_kw(args))
    return R.AdapterResult.failure(
        "kiro", str(command or "cli"), R.ERROR, "unknown command"
    )


def _emit(result: R.AdapterResult, as_json: bool) -> None:
    if as_json:
        print(result.to_json(indent=2))
        return
    print(result.summary())
    data: Dict[str, Any] = result.data if isinstance(result.data, dict) else {}
    answer = data.get("answer")
    if answer:
        print(answer)
    for hint in result.hints or []:
        print("hint: {}".format(hint))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "show_version", False):
        print(__version__)
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        result = _run(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not crash
        result = R.AdapterResult.failure(
            "kiro", args.command, R.ERROR, "{}: {}".format(type(exc).__name__, exc)
        )
    _emit(result, bool(getattr(args, "as_json", False)))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
