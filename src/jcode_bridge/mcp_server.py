"""MCP stdio server that exposes jcode as a task backend.

Standard library only, newline-delimited JSON-RPC 2.0, same protocol shape as
``kiro_adapters.mcp_server``. Run it as ``jcode-bridge-mcp`` or
``python -m jcode_bridge.mcp_server``.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from kiro_adapters import result as R
from kiro_adapters.redact import redact, redact_obj

from . import __version__
from .api import JcodeApiBackend
from .backend import JcodeBackend

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "jcode-bridge"
MAX_TIMEOUT_SECS = 86400.0

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_BACKEND: Optional[JcodeBackend] = None
_API_BACKEND: Optional[JcodeApiBackend] = None


def backend() -> JcodeBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = JcodeBackend()
    return _BACKEND


def api_backend() -> JcodeApiBackend:
    """The daemon-API backend, kept separate so it can be pointed at a fake."""
    global _API_BACKEND
    if _API_BACKEND is None:
        _API_BACKEND = JcodeApiBackend()
    return _API_BACKEND


def reset_backend() -> None:
    """Drop the cached backend so a test can point it at a fake binary."""
    global _BACKEND, _API_BACKEND
    _BACKEND = None
    _API_BACKEND = None


_PROP_TYPES = {
    "prompt": "string",
    "provider": "string",
    "model": "string",
    "tools": "string",
    "disabled_tools": "string",
    "cwd": "string",
    "timeout_secs": "number",
    "task_id": "string",
    "tail_chars": "number",
    "limit": "number",
    "keep_session": "boolean",
}

_PROP_DESCRIPTIONS = {
    "prompt": "Task text for jcode to carry out.",
    "provider": "Provider name (for example anthropic-api, openai-api).",
    "model": "Model override.",
    "tools": "Comma-separated tool allowlist.",
    "disabled_tools": "Comma-separated tool denylist.",
    "cwd": "Working directory for the task.",
    "timeout_secs": "Timeout in seconds.",
    "task_id": "Task id returned by jcode_task_start.",
    "tail_chars": "How many trailing characters of output to return.",
    "limit": "Maximum number of tasks to list.",
    "keep_session": "Keep the session open for follow-up messages.",
}

# (name, description, property names, required property names)
_TOOLS = [
    ("jcode_health", "Jcode binary path, version and task home.", "", ""),
    (
        "jcode_run",
        "Run one task synchronously through jcode and return its answer.",
        "prompt provider model tools disabled_tools cwd timeout_secs",
        "prompt",
    ),
    (
        "jcode_task_start",
        "Start a detached jcode task and return a task id immediately.",
        "prompt provider model tools disabled_tools cwd timeout_secs",
        "prompt",
    ),
    ("jcode_task_status", "State of a detached task: running, completed, failed, cancelled.", "task_id", "task_id"),
    ("jcode_task_output", "Captured output and answer of a detached task.", "task_id tail_chars", "task_id"),
    ("jcode_task_cancel", "Stop a detached task and its process group.", "task_id", "task_id"),
    ("jcode_task_list", "Most recent detached tasks with their state.", "limit", ""),
    (
        "jcode_api_health",
        "Check the jcode harness API: spawn, handshake and ping without a session.",
        "",
        "",
    ),
    (
        "jcode_api_ask",
        "Run one turn over a live jcode harness-API session and return the answer.",
        "prompt cwd timeout_secs keep_session",
        "prompt",
    ),
    (
        "jcode_api_sessions",
        "List the jcode sessions the harness API can see.",
        "limit timeout_secs",
        "",
    ),
]


def build_tools() -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    for name, description, props, required in _TOOLS:
        properties: Dict[str, Any] = {}
        for prop in props.split():
            properties[prop] = {
                "type": _PROP_TYPES[prop],
                "description": _PROP_DESCRIPTIONS[prop],
            }
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required.split(),
                    "additionalProperties": False,
                },
            }
        )
    return tools


# -- argument helpers -----------------------------------------------------
def _optional_str(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _number(args: Dict[str, Any], key: str) -> Optional[float]:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timeout_kwargs(args: Dict[str, Any]) -> Dict[str, float]:
    value = _number(args, "timeout_secs")
    return {"timeout": value} if value is not None else {}


def _timeout_problem(value: object) -> Optional[str]:
    """Return a message when ``timeout_secs`` breaks the timeout contract.

    A timeout that is not a positive finite number reaches the runner as "no
    limit", so a caller asking for a bounded run would silently get an
    unbounded one. The tool has to say no instead of quietly substituting the
    default.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "timeout_secs must be a number, got {}".format(type(value).__name__)
    number = float(value)
    if not math.isfinite(number):
        return "timeout_secs must be a finite number, got {}".format(number)
    if number <= 0:
        return "timeout_secs must be greater than 0 seconds, got {}".format(number)
    if number > MAX_TIMEOUT_SECS:
        return "timeout_secs must be at most {} seconds, got {}".format(
            int(MAX_TIMEOUT_SECS), number
        )
    return None


def _positive_int_problem(value: object, key: str) -> Optional[str]:
    """Return a message when a count argument is not a positive whole number.

    A count that is silently truncated or clamped hides a caller mistake, so
    the tool reports it rather than guessing what was meant.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "{} must be an integer, got {}".format(key, type(value).__name__)
    number = float(value)
    if not number.is_integer():
        return "{} must be an integer, got {}".format(key, value)
    if number <= 0:
        return "{} must be greater than 0, got {}".format(key, int(number))
    return None


# Which numeric arguments each tool accepts, so a bad value is named instead
# of being dropped and replaced by a default.
_ARG_RULES: Dict[str, Dict[str, str]] = {
    "jcode_run": {"timeout_secs": "timeout"},
    "jcode_task_start": {"timeout_secs": "timeout"},
    "jcode_task_output": {"tail_chars": "positive_int"},
    "jcode_task_list": {"limit": "positive_int"},
    "jcode_api_ask": {"timeout_secs": "timeout"},
    "jcode_api_sessions": {"timeout_secs": "timeout", "limit": "positive_int"},
}


def _validate_arguments(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Return a message describing the first invalid argument of a tool call."""
    rules = _ARG_RULES.get(name)
    if not rules:
        return None
    for key, rule in rules.items():
        value = args.get(key)
        if value is None:
            continue
        if rule == "timeout":
            message = _timeout_problem(value)
        else:
            message = _positive_int_problem(value, key)
        if message is not None:
            return message
    return None


def _task_kwargs(args: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "provider": _optional_str(args, "provider"),
        "model": _optional_str(args, "model"),
        "tools": _optional_str(args, "tools"),
        "disabled_tools": _optional_str(args, "disabled_tools"),
        "cwd": _optional_str(args, "cwd"),
    }
    kwargs.update(_timeout_kwargs(args))
    return kwargs


def _task_id(args: Dict[str, Any]) -> str:
    value = args.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task_id is required")
    return value


# -- handlers -------------------------------------------------------------
def _output(args: Dict[str, Any]) -> R.AdapterResult:
    tail_chars = _number(args, "tail_chars")
    if tail_chars is None:
        return backend().output(_task_id(args))
    return backend().output(_task_id(args), tail_chars=int(tail_chars))


def _api_ask(args: Dict[str, Any]) -> R.AdapterResult:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    kwargs: Dict[str, Any] = {"prompt": prompt}
    cwd = _optional_str(args, "cwd")
    if cwd is not None:
        kwargs["working_dir"] = cwd
    keep = args.get("keep_session")
    if isinstance(keep, bool):
        kwargs["keep_session"] = keep
    kwargs.update(_timeout_kwargs(args))
    return api_backend().ask(**kwargs)


def _api_sessions(args: Dict[str, Any]) -> R.AdapterResult:
    limit = _number(args, "limit")
    kwargs: Dict[str, Any] = {}
    if limit is not None:
        kwargs["limit"] = int(limit)
    kwargs.update(_timeout_kwargs(args))
    return api_backend().sessions(**kwargs)


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], R.AdapterResult]] = {
    "jcode_health": lambda _: backend().health(),
    "jcode_run": lambda a: backend().run(str(a.get("prompt", "")), **_task_kwargs(a)),
    "jcode_task_start": lambda a: backend().start(str(a.get("prompt", "")), **_task_kwargs(a)),
    "jcode_task_status": lambda a: backend().status(_task_id(a)),
    "jcode_task_output": _output,
    "jcode_task_cancel": lambda a: backend().cancel(_task_id(a)),
    "jcode_task_list": lambda a: backend().list_tasks(int(_number(a, "limit") or 20)),
    "jcode_api_health": lambda _: api_backend().health(),
    "jcode_api_ask": _api_ask,
    "jcode_api_sessions": _api_sessions,
}


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> R.AdapterResult:
    """Run one tool. Never raises: failures come back as an error result."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return R.AdapterResult.failure(
            "jcode", name, R.ERROR, "unknown tool: {}".format(name)
        )
    args = arguments or {}
    try:
        problem = _validate_arguments(name, args)
        if problem is not None:
            return R.AdapterResult.failure("jcode", name, R.ERROR, problem)
        return handler(args)
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the server
        return R.AdapterResult.failure(
            "jcode", name, R.ERROR, "{}: {}".format(type(exc).__name__, exc)
        )


# -- JSON-RPC -------------------------------------------------------------
def _result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: message must be an object")
    msg_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(msg_id, INVALID_REQUEST, "Invalid Request: missing method")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        )
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
    payload = redact_obj(result.to_dict())
    return _result(
        msg_id,
        {
            "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "isError": not result.ok,
        },
    )


def _log(message: str) -> None:
    sys.stderr.write("[jcode-bridge-mcp] {}\n".format(redact(message)))
    sys.stderr.flush()


def serve(stdin: Any = None, stdout: Any = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            stdout.write(json.dumps(_error(None, PARSE_ERROR, "Parse error: {}".format(exc))) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_message(message)
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            _log("handler crashed: {}: {}".format(type(exc).__name__, exc))
            response = _error(message.get("id"), INTERNAL_ERROR, str(exc))
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if "--help" in args or "-h" in args:
        sys.stdout.write(
            "jcode-bridge-mcp: MCP stdio server exposing jcode as a task backend.\n"
            "Tools: {}\n".format(", ".join(tool["name"] for tool in build_tools()))
        )
        return 0
    if "--selftest" in args:
        sys.stdout.write(json.dumps({"tools": build_tools()}, indent=2) + "\n")
        return 0
    _log("ready ({} tools)".format(len(_TOOLS)))
    return serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
