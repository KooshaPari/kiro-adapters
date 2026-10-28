"""One result shape for every Kiro harness.

Every adapter method returns an :class:`AdapterResult`. Callers never have to
know whether the underlying transport was a CLI invocation, a gateway HTTP call
or a local file read: the result carries an explicit ``code`` so "not
installed", "blocked by readiness" and "the model said no" are distinguishable
without string-matching stderr.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Result codes. These are part of the public contract (see docs/ADAPTER-CONTRACT.md).
OK = "OK"
NOT_INSTALLED = "NOT_INSTALLED"
BLOCKED = "BLOCKED"
TIMEOUT = "TIMEOUT"
ERROR = "ERROR"
UNSUPPORTED = "UNSUPPORTED"

MAX_STREAM_CHARS = 20_000


def truncate(text: Optional[str], limit: int = MAX_STREAM_CHARS) -> str:
    """Bound a captured stream so a runaway agent cannot flood the caller."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated {} chars]".format(len(text) - limit)


@dataclass
class AdapterResult:
    """JSON-safe outcome of one adapter action."""

    ok: bool
    harness: str
    action: str
    code: str = OK
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    hints: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    # -- constructors -----------------------------------------------------
    @classmethod
    def success(
        cls,
        harness: str,
        action: str,
        detail: str = "",
        data: Optional[Dict[str, Any]] = None,
        stdout: str = "",
        stderr: str = "",
        duration_ms: int = 0,
        hints: Optional[List[str]] = None,
    ) -> "AdapterResult":
        return cls(
            ok=True,
            harness=harness,
            action=action,
            code=OK,
            detail=detail,
            data=data or {},
            stdout=truncate(stdout),
            stderr=truncate(stderr),
            duration_ms=duration_ms,
            hints=list(hints or []),
        )

    @classmethod
    def failure(
        cls,
        harness: str,
        action: str,
        code: str,
        detail: str,
        data: Optional[Dict[str, Any]] = None,
        stdout: str = "",
        stderr: str = "",
        duration_ms: int = 0,
        hints: Optional[List[str]] = None,
    ) -> "AdapterResult":
        return cls(
            ok=False,
            harness=harness,
            action=action,
            code=code,
            detail=detail,
            data=data or {},
            stdout=truncate(stdout),
            stderr=truncate(stderr),
            duration_ms=duration_ms,
            hints=list(hints or []),
        )

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "harness": self.harness,
            "action": self.action,
            "code": self.code,
            "detail": self.detail,
            "data": self.data,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "hints": self.hints,
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=str)

    def summary(self) -> str:
        """One-line status used by the CLI and by MCP text content."""
        head = "{}:{} {}".format(self.harness, self.action, "ok" if self.ok else self.code)
        if self.detail:
            return "{} - {}".format(head, self.detail)
        return head
