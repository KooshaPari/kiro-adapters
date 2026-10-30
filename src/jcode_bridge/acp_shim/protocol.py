"""Newline-delimited JSON-RPC 2.0 framing for the ACP adoption shim.

The fork reads ACP with ``read_line`` (src/cli/acp.rs:316-322), one JSON object
per line, so that is the wire format here. This module holds the framing, the
error codes and a scrubber. It never keeps a transcript body: callers pass in
only method names and already-derived error text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, TextIO

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Shim-owned codes. The request itself is well formed; a documented
# precondition cannot be met, or the upstream link is gone.
SHIM_PRECONDITION_FAILED = -32010
SHIM_UPSTREAM_UNAVAILABLE = -32011

MAX_LINE_CHARS = 8 * 1024 * 1024

_SECRET_PATTERNS = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=:-]{8,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|secret|password)(\s*[=:]\s*)[^\s\"']{4,}"),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{12,}\b"),
)


class RpcError(Exception):
    """A JSON-RPC error that is safe to send to a client."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_payload(self, request_id: Any) -> Dict[str, Any]:
        error: Dict[str, Any] = {
            "code": self.code,
            "message": scrub(self.message)[:2000],
        }
        if self.data:
            error["data"] = scrub_obj(self.data)
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


class BlockedUpstreamCall(Exception):
    """A caller tried to send a method the shim must never forward."""

    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


def scrub(text: Optional[str]) -> str:
    """Return ``text`` with credential-shaped substrings replaced."""
    if not text:
        return ""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out


def scrub_obj(value: Any) -> Any:
    """Recursively scrub every string in a JSON-like structure."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(key)): scrub_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(item) for item in value]
    return value


def result_payload(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_payload(request_id: Any, error: RpcError) -> Dict[str, Any]:
    return error.to_payload(request_id)


def read_message(stream: TextIO) -> Optional[Dict[str, Any]]:
    """Read one JSON object line. Returns ``None`` at EOF.

    Blank lines are skipped. A malformed line raises :class:`RpcError` with
    ``PARSE_ERROR``; the caller answers and keeps reading, so one bad line does
    not end the stream.
    """
    while True:
        line = stream.readline(MAX_LINE_CHARS)
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except ValueError:
            raise RpcError(PARSE_ERROR, "Invalid JSON-RPC payload")
        if not isinstance(message, dict):
            raise RpcError(INVALID_REQUEST, "JSON-RPC payload must be an object")
        return message


def write_message(stream: TextIO, payload: Dict[str, Any]) -> None:
    """Write one compact JSON line and flush. Never inspects ``payload``."""
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=False) + "\n")
    stream.flush()


def method_label(message: Dict[str, Any]) -> str:
    """Return a log-safe label for a message. Bodies are never included."""
    method = message.get("method")
    if isinstance(method, str) and method:
        return scrub(method)[:80]
    return "<response>"


def is_notification(message: Dict[str, Any]) -> bool:
    return "id" not in message


def error_codes() -> List[int]:
    """Stable listing for docs and tests."""
    return [
        PARSE_ERROR,
        INVALID_REQUEST,
        METHOD_NOT_FOUND,
        INVALID_PARAMS,
        INTERNAL_ERROR,
        SHIM_PRECONDITION_FAILED,
        SHIM_UPSTREAM_UNAVAILABLE,
    ]
