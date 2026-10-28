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
import json
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__
from . import result as R
from .kirocode import KiroCodeAdapter
from .kirocrew import KiroCrewAdapter

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "kiro-adapters"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

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


# -- tool descriptors -----------------------------------------------------
# Property types are shared by name; "harness" is the only enum.
_PROP_TYPES = {
    "agent": "string", "background": "boolean", "crew": "string", "cwd": "string",
    "harness": "string", "message": "string", "model": "string", "name": "string",
    "no_test": "boolean", "prompt": "string", "spec_path": "string",
    "timeout_secs": "number", "ttl": "string",
}
_ENUM_PROPS = {"harness": ["kirocrew", "kirocode"]}
_PROP_DESCRIPTIONS = {
    "agent": "Agent name to use.", "background": "Run without waiting.",
    "crew": "Crew to dispatch into.", "cwd": "Working directory.",
    "harness": "Harness that runs the prompt.", "message": "Message to send.",
    "model": "Model override.", "name": "Run name.",
    "no_test": "Skip the test phase.", "prompt": "Prompt to send.",
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
]


def build_tools() -> List[Dict[str, Any]]:
    """Return the tool descriptors advertised by ``tools/list``."""
    tools: List[Dict[str, Any]] = []
    for name, description, props, required in _TOOLS:
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


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], R.AdapterResult]] = {
    "kirocrew_status": lambda _: _crew().status(),
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
}


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> R.AdapterResult:
    """Run one tool. Never raises: failures come back as an error result."""
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


# -- JSON-RPC plumbing ----------------------------------------------------
def _result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message.

    Returns the response object, or ``None`` for notifications (which by
    definition get no reply).
    """
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
        return _result(msg_id, {"tools": build_tools()})
    if method == "tools/call":
        return _tools_call(msg_id, message)
    return _error(msg_id, METHOD_NOT_FOUND, "Method not found: {}".format(method))


def _tools_call(msg_id: Any, message: Dict[str, Any]) -> Dict[str, Any]:
    params = message.get("params")
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "Invalid params: tools/call needs a params object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(msg_id, INVALID_PARAMS, "Invalid params: missing tool name")
    if name not in _HANDLERS:
        return _error(msg_id, INVALID_PARAMS, "Invalid params: unknown tool '{}'".format(name))
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(msg_id, INVALID_PARAMS, "Invalid params: arguments must be an object")
    result = call_tool(name, arguments)
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


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read JSON-RPC frames from stdin until EOF."""
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
            response = handle_message(message)
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
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.selftest:
        sys.stdout.write(json.dumps({"tools": build_tools()}, indent=2) + "\n")
        sys.stdout.flush()
        return 0
    try:
        return serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
