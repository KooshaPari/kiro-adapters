"""Zero-dependency MCP (Model Context Protocol) stdio server for the Kiro adapters.

JSON-RPC 2.0 over stdio: one JSON object per line in, one per line out. No
third-party package is involved, so the server runs anywhere CPython 3.9+ runs.

* stdout carries protocol frames only — every diagnostic goes to stderr.
* No tool can crash the server: a failing adapter call becomes an
  AdapterResult failure rendered as tool text.
* The dashboard token is never exposed (include_token is never set).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__
from . import result as R
from .inventory import registry
from .kirocode import KiroCodeAdapter
from .kirocrew import KiroCrewAdapter

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "kiro-adapters"
INVENTORY_TOOL_NAMES = ["agent_sessions_list", "agent_session_get",
                        "agent_session_history", "agent_sources"]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_CREW: Optional[KiroCrewAdapter] = None
_CODE: Optional[KiroCodeAdapter] = None

# Opt-in attestation: when this env var names a file, every tool invocation
# appends one redacted JSON line. Unset/empty means no file and no work at all.
AUDIT_LOG_ENV = "KIRO_INVENTORY_AUDIT_LOG"

# Opt-in for CLI-backed inventory sources. Default OFF: the four read-only
# inventory tools promise "a discovery call is a file read". ShareCLI is
# CLI-backed by necessity, so folding it in silently would let one agent turn
# spawn a subprocess from an MCP tool call -- a different risk class from
# reading a file. It is enabled only by this env flag, never by a tool
# argument, so the model cannot turn it on for itself.
CLI_SOURCES_ENV = "KIRO_INVENTORY_ALLOW_CLI_SOURCES"


def _cli_sources_enabled() -> bool:
    """Whether the operator opted the inventory surface into CLI sources."""
    return os.environ.get(CLI_SOURCES_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


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


_SERVER_MODE = "full"


def _coerce_mode(mode=None, inventory_only=False, **kwargs) -> str:
    if inventory_only is True:
        return "inventory"
    if mode is None:
        for key in ("inventory", "read_only", "readonly", "safe",
                    "inventory_only", "inventory-mode"):
            if kwargs.get(key) is not None:
                mode = kwargs.get(key)
                break
    if mode is None:
        return _SERVER_MODE
    if isinstance(mode, bool):
        return "inventory" if mode else "full"
    text = str(mode).strip().lower().replace("_", "-")
    if text in ("",):
        return _SERVER_MODE
    if text in ("inventory", "inventory-only", "read-only", "readonly",
                "safe", "true", "1", "yes", "on"):
        return "inventory"
    if text in ("full", "default", "all", "false", "0", "no", "off"):
        return "full"
    if ("inventory" in text or "read" in text or "safe" in text):
        return "inventory"
    return "full"


def _allowed_tool_names(mode=None, inventory_only=False, **kwargs) -> set:
    resolved = _coerce_mode(mode, inventory_only=inventory_only, **kwargs)
    if resolved == "inventory":
        return set(INVENTORY_TOOL_NAMES)
    return {row[0] for row in _TOOLS}


def set_server_mode(mode) -> str:
    global _SERVER_MODE
    _SERVER_MODE = _coerce_mode(mode)
    return _SERVER_MODE


# -- tool descriptors -----------------------------------------------------
# Property types are shared by name; "harness" is the only enum.
_PROP_TYPES = {
    "agent": "string", "background": "boolean", "crew": "string", "cwd": "string",
    "harness": "string", "include_stale": "boolean", "limit": "integer",
    "message": "string", "model": "string", "name": "string",
    "no_test": "boolean", "prompt": "string", "session_uid": "string",
    "spec_path": "string", "timeout_secs": "number", "ttl": "string",
}
_ENUM_PROPS = {"harness": ["kirocrew", "kirocode"]}
_PROP_DESCRIPTIONS = {
    "agent": "Agent name to use.", "background": "Run without waiting.",
    "crew": "Crew to dispatch into.", "cwd": "Working directory.",
    "harness": "Harness that runs the prompt.",
    "include_stale": "Include sessions whose heartbeat is missing.",
    "limit": "Maximum number of items to return.",
    "message": "Message to send.",
    "model": "Model override.", "name": "Run name.",
    "no_test": "Skip the test phase.", "prompt": "Prompt to send.",
    "session_uid": "Session uid as '<source>:<native_session_id>'.",
    "spec_path": "Path to the spec file.", "timeout_secs": "Timeout in seconds.",
    "ttl": "Dashboard token TTL.",
}

# (tool name, description, property names, required property names)
_TOOLS = [
    ("kirocrew_status", "Kiro Crew gateway, auth mode, KAS engine state and agent count.",
     "", ""),
    ("kirocrew_agents", "List the agent definitions in ~/.kiro/agents (no model call).",
     "", ""),
    ("kirocrew_ask", "Send one chat message to the Kiro Crew agent and return its answer.",
     "message agent model timeout_secs", "message"),
    ("kirocrew_spawn", "Dispatch a Kiro Crew subagent with a prompt, optionally in background.",
     "prompt agent crew background timeout_secs", "prompt"),
    ("kirocrew_spawn_list", "List Kiro Crew subagents that have been spawned.", "", ""),
    ("kirocrew_run_spec", "Run an autonomous Kiro Crew task described by a spec file.",
     "spec_path name no_test timeout_secs", "spec_path"),
    ("kirocrew_dashboard_url", "Kiro Crew dashboard URL; the access token is always redacted.",
     "ttl", ""),
    ("kirocode_status", "Kiro coding CLI path, version and probed capabilities.", "", ""),
    ("kirocode_ask", "Send one prompt to the Kiro coding CLI and return its output.",
     "prompt cwd timeout_secs", "prompt"),
    ("kiro_dispatch", "Dispatch one prompt to either Kiro Crew or the Kiro coding CLI.",
     "harness prompt agent timeout_secs", "harness prompt"),
    # Read-only session inventory. These only read harness stores: no daemon
    # is contacted, no harness binary is run and no session is resumed.
    # ShareCLI is the one CLI-backed source; it is OFF unless the operator sets
    # KIRO_INVENTORY_ALLOW_CLI_SOURCES (never via a tool argument).
    ("agent_sessions_list",
     "List agent sessions detectable on this host (read-only; no process started. "
     "CLI-backed sources such as ShareCLI are excluded unless "
     "KIRO_INVENTORY_ALLOW_CLI_SOURCES is set on the server).",
     "limit include_stale", ""),
    ("agent_session_get", "Return one detected agent session by its session uid.",
     "session_uid", "session_uid"),
    ("agent_session_history", "Bounded, redacted history slice for one session uid.",
     "session_uid limit", "session_uid"),
    ("agent_sources", "Session stores this host exposes for agent-session detection.",
     "", ""),
]


def build_tools(mode=None, inventory_only=False, **kwargs) -> List[Dict[str, Any]]:
    """Return the tool descriptors advertised by ``tools/list``.

    In inventory-only mode only the four read-only inventory tools are
    advertised; every dispatch/ask/spawn tool is both hidden and rejected.
    """
    allowed = _allowed_tool_names(mode, inventory_only=inventory_only, **kwargs)
    tools: List[Dict[str, Any]] = []
    for name, description, props, required in _TOOLS:
        if name not in allowed:
            continue
        properties: Dict[str, Any] = {}
        for prop in props.split():
            spec: Dict[str, Any] = {"type": _PROP_TYPES[prop]}
            spec["description"] = _PROP_DESCRIPTIONS[prop]
            if prop in _ENUM_PROPS:
                spec["enum"] = list(_ENUM_PROPS[prop])
            properties[prop] = spec
        tools.append({
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required.split(),
                "additionalProperties": False,
            },
        })
    return tools


# -- argument helpers -----------------------------------------------------
def _timeout_kwargs(args: Dict[str, Any]) -> Dict[str, float]:
    value = args.get("timeout_secs")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {}
    return {"timeout": float(value)}


def _optional_str(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _optional_int(args: Dict[str, Any], key: str) -> Optional[int]:
    """An integer argument, or ``None`` when absent or not a real integer."""
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# -- tool handlers --------------------------------------------------------
def _dispatch(args: Dict[str, Any]) -> R.AdapterResult:
    harness = str(args.get("harness", ""))
    prompt = str(args.get("prompt", ""))
    if harness == "kirocrew":
        return _crew().ask(message=prompt, agent=_optional_str(args, "agent"),
                           **_timeout_kwargs(args))
    if harness == "kirocode":
        return _code().ask(prompt=prompt, **_timeout_kwargs(args))
    return R.AdapterResult.failure(
        "kiro", "dispatch", R.UNSUPPORTED, "unknown harness: {}".format(harness or "<empty>")
    )


def _agent_sessions_list(args: Dict[str, Any]) -> R.AdapterResult:
    """Read-only list of the sessions detectable on this host."""
    include_stale = args.get("include_stale")
    if not isinstance(include_stale, bool):
        include_stale = True
    envelopes = registry.enumerate_sessions(
        limit=_optional_int(args, "limit"),
        include_stale=include_stale,
        include_cli=_cli_sources_enabled(),
    )
    return R.AdapterResult.success(
        "inventory",
        "agent_sessions_list",
        detail="{} session(s) detected".format(len(envelopes)),
        data={
            "count": len(envelopes),
            "include_stale": include_stale,
            "sessions": [envelope.to_dict() for envelope in envelopes],
        },
    )


def _agent_session_get(args: Dict[str, Any]) -> R.AdapterResult:
    """One session envelope, by uid."""
    session_uid = _optional_str(args, "session_uid")
    if session_uid is None:
        return R.AdapterResult.failure(
            "inventory", "agent_session_get", R.ERROR, "session_uid is required"
        )
    envelope = registry.find_session(session_uid, include_cli=_cli_sources_enabled())
    if envelope is None:
        return R.AdapterResult.failure(
            "inventory",
            "agent_session_get",
            R.ERROR,
            "no detected session matches {}".format(session_uid),
            hints=["call agent_sessions_list to see the uids this host exposes"],
        )
    return R.AdapterResult.success(
        "inventory",
        "agent_session_get",
        detail="session found",
        data={"session": envelope.to_dict()},
    )


def _agent_session_history(args: Dict[str, Any]) -> R.AdapterResult:
    """Bounded, redacted history slice for one uid (never a whole transcript)."""
    session_uid = _optional_str(args, "session_uid")
    if session_uid is None:
        return R.AdapterResult.failure(
            "inventory", "agent_session_history", R.ERROR, "session_uid is required"
        )
    limit = _optional_int(args, "limit")
    data = registry.session_history(
        session_uid,
        limit=registry.DEFAULT_HISTORY_LIMIT if limit is None else limit,
        include_cli=_cli_sources_enabled(),
    )
    if not data.get("ok"):
        return R.AdapterResult.failure(
            "inventory",
            "agent_session_history",
            R.ERROR,
            str(data.get("detail") or "history is not available"),
            data=data,
        )
    return R.AdapterResult.success(
        "inventory",
        "agent_session_history",
        detail="{} item(s) returned".format(data.get("returned", 0)),
        data=data,
    )


def _agent_sources(args: Dict[str, Any]) -> R.AdapterResult:
    """The stores this host exposes for session detection."""
    sources = registry.available_sources(include_cli=_cli_sources_enabled())
    present = sum(1 for source in sources if source.get("present"))
    return R.AdapterResult.success(
        "inventory",
        "agent_sources",
        detail="{}/{} source store(s) present".format(present, len(sources)),
        data={"count": len(sources), "sources": sources},
    )


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], R.AdapterResult]] = {
    "kirocrew_status": lambda _: _crew().status(include_kas=True),
    "kirocrew_agents": lambda _: _crew().list_agents(),
    "kirocrew_spawn_list": lambda _: _crew().spawn_list(),
    "kirocode_status": lambda _: _code().status(),
    "kirocrew_dashboard_url": lambda a: _crew().dashboard_url(ttl=_optional_str(a, "ttl")),
    "kirocrew_ask": lambda a: _crew().ask(
        message=str(a.get("message", "")), agent=_optional_str(a, "agent"),
        model=_optional_str(a, "model"), **_timeout_kwargs(a)
    ),
    "kirocrew_spawn": lambda a: _crew().spawn(
        prompt=str(a.get("prompt", "")), agent=_optional_str(a, "agent"),
        crew=_optional_str(a, "crew"), background=bool(a.get("background")),
        **_timeout_kwargs(a)
    ),
    "kirocrew_run_spec": lambda a: _crew().run_spec(
        spec_path=str(a.get("spec_path", "")), name=_optional_str(a, "name"),
        no_test=bool(a.get("no_test")), **_timeout_kwargs(a)
    ),
    "kirocode_ask": lambda a: _code().ask(
        prompt=str(a.get("prompt", "")), cwd=_optional_str(a, "cwd"), **_timeout_kwargs(a)
    ),
    "kiro_dispatch": _dispatch,
    "agent_sessions_list": _agent_sessions_list,
    "agent_session_get": _agent_session_get,
    "agent_session_history": _agent_session_history,
    "agent_sources": _agent_sources,
}


def _audit_event(event: str, **fields: Any) -> None:
    """Append one redacted attestation line when ``KIRO_INVENTORY_AUDIT_LOG`` is set.

    Every failure is swallowed: telemetry must never break the server.
    """
    path = os.environ.get(AUDIT_LOG_ENV, "").strip()
    if not path:
        return
    try:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": event,
        }
        record.update(fields)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 - drop the record, never the call
        pass


def _audit_start(mode: str, argv: Sequence[str]) -> None:
    """Record that this server was SPAWNED, independently of any tool call.

    Why this exists: a ``tool`` record proves a call, which requires the model to
    choose the tool. It cannot distinguish "the server is mounted but the model
    never called it" from "the server was never mounted". A ``start`` record
    settles that: if the process ran at all, the host mounted it.

    Carries the mode and the argv only - both are operator-supplied launch
    arguments, never user data.
    """
    _audit_event(
        "start",
        pid=os.getpid(),
        mode=mode,
        argv=[str(a) for a in argv],
    )


def _audit_tool_call(name: str, result: R.AdapterResult) -> None:
    """Append one redacted attestation line when ``KIRO_INVENTORY_AUDIT_LOG`` is set.

    The record carries the tool name, its success flag, the response size and
    nothing else: no arguments, no results, no paths, no user data.
    """
    _audit_event(
        "tool",
        tool=name,
        ok=bool(result.ok),
        result_bytes=len(result.to_json().encode("utf-8")),
    )


def _execute_tool(name: str, arguments: Optional[Dict[str, Any]],
                  mode: str) -> R.AdapterResult:
    """Resolve and run one tool without auditing it."""
    if name not in _allowed_tool_names(mode):
        return R.AdapterResult.failure(
            name.split("_", 1)[0], name, R.ERROR,
            "tool not available in inventory-only mode: {}".format(name)
        )
    handler = _HANDLERS.get(name)
    if handler is None:
        return R.AdapterResult.failure(
            name.split("_", 1)[0], name, R.ERROR, "unknown tool: {}".format(name)
        )
    try:
        return handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the server
        return R.AdapterResult.failure(
            name.split("_", 1)[0], name, R.ERROR, "{}: {}".format(type(exc).__name__, exc)
        )


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None,
              mode=None, inventory_only=False, **kwargs) -> R.AdapterResult:
    """Run one tool. Never raises: failures come back as an error result.

    Non-allowlisted tools are rejected even when a caller invokes them by
    name (they are not merely hidden from ``tools/list``). Every invocation
    is eligible for one redacted attestation line; see ``_audit_tool_call``.
    """
    resolved = _coerce_mode(mode, inventory_only=inventory_only, **kwargs)
    result = _execute_tool(name, arguments, resolved)
    _audit_tool_call(name, result)
    return result


# -- JSON-RPC plumbing ----------------------------------------------------
def _result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(message: Dict[str, Any], mode=None, inventory_only=False,
                    **kwargs) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message.

    Returns the response object, or ``None`` for notifications (which by
    definition get no reply). ``mode`` selects the tool surface; unknown or
    non-allowlisted tool calls are rejected rather than dispatched.
    """
    resolved = _coerce_mode(mode, inventory_only=inventory_only, **kwargs)
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: message must be an object")
    msg_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: missing method")
    # Notifications never get a response.
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": build_tools(mode=resolved)})
    if method == "tools/call":
        return _tools_call(msg_id, message, mode=resolved)
    return _error(msg_id, METHOD_NOT_FOUND, "Method not found: {}".format(method))


def _tools_call(msg_id: Any, message: Dict[str, Any], mode=None) -> Dict[str, Any]:
    resolved = _coerce_mode(mode)
    params = message.get("params")
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "Invalid params: tools/call needs a params object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(msg_id, INVALID_PARAMS, "Invalid params: missing tool name")
    if name not in _allowed_tool_names(resolved):
        return _error(msg_id, INVALID_PARAMS,
                      "Invalid params: tool not available in this server mode: '{}'".format(name))
    if name not in _HANDLERS:
        return _error(msg_id, INVALID_PARAMS, "Invalid params: unknown tool '{}'".format(name))
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(msg_id, INVALID_PARAMS, "Invalid params: arguments must be an object")
    result = call_tool(name, arguments, mode=resolved)
    return _result(msg_id, {
        "content": [{"type": "text", "text": result.to_json()}],
        "isError": not result.ok,
    })


def _log(message: str) -> None:
    sys.stderr.write("[kiro-adapter-mcp] {}\n".format(message))
    sys.stderr.flush()


def _write(stream: Any, payload: Dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=str) + "\n")
    stream.flush()


def serve(stdin: Any = None, stdout: Any = None, mode=None,
         inventory_only=False, **kwargs) -> int:
    """Read JSON-RPC frames from stdin until EOF."""
    global _SERVER_MODE
    resolved = _coerce_mode(mode, inventory_only=inventory_only, **kwargs)
    _SERVER_MODE = resolved
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    while True:
        line = source.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _write(sink, _error(None, PARSE_ERROR, "Parse error: {}".format(exc)))
            continue
        try:
            response = handle_message(message, mode=resolved)
        except Exception as exc:  # noqa: BLE001 - the server must survive
            _log("internal error: {}".format(exc))
            response = _error(None, INTERNAL_ERROR, "Internal error: {}".format(exc))
        if response is not None:
            _write(sink, response)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kiro-adapter-mcp",
        description="Zero-dependency MCP stdio server exposing the Kiro adapters as tools.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="print the tools/list payload as JSON and exit")
    parser.add_argument("--mode", choices=["full", "inventory", "inventory-only",
                                           "read-only", "readonly"],
                        default=None,
                        help="tool surface to expose (default: full)")
    parser.add_argument("--inventory-only", dest="inventory_only",
                        action="store_true",
                        help="expose only the four read-only inventory tools")
    parser.add_argument("--read-only", dest="read_only", action="store_true",
                        help="alias for --inventory-only")
    args = parser.parse_args(list(argv) if argv is not None else None)
    resolved_mode = "inventory" if (args.inventory_only or args.read_only) else args.mode
    # Attest that the server was SPAWNED. This is emitted before any protocol
    # handling so a host that mounts the server and never calls a tool still
    # leaves a record - the only way to tell "mounted but uncalled" apart from
    # "never mounted".
    _audit_start(str(resolved_mode), list(argv) if argv is not None else sys.argv[1:])
    if args.selftest:
        sys.stdout.write(json.dumps({"tools": build_tools(mode=resolved_mode)}, indent=2) + "\n")
        sys.stdout.flush()
        return 0
    try:
        return serve(mode=resolved_mode)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
