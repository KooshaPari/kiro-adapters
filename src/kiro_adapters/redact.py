"""Secret redaction for adapter output.

``kirocrew token`` prints a dashboard URL carrying a signed JWT. That URL is a
credential: it must never land in an MCP tool result, a log line or a commit.
Everything an adapter returns passes through :func:`redact` first, and callers
must opt in explicitly (``include_token=True``) to see the raw value.
"""

from __future__ import annotations

import re
from typing import List, Optional, Pattern, Tuple

REDACTED = "<redacted>"

# Ordered: most specific first. Each entry is (pattern, replacement).
_PATTERNS: List[Tuple[Pattern[str], str]] = [
    # JWT (header.payload.signature) — kirocrew dashboard tokens, kiro auth tokens.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        "<jwt:redacted>",
    ),
    # token=... / api_key=... / secret=... query params and assignments.
    (
        re.compile(
            r"(?i)\b(token|access_token|api[_-]?key|secret|password|authorization)"
            r"(\s*[=:]\s*)([A-Za-z0-9._~+/=:-]{8,})"
        ),
        r"\1\2" + REDACTED,
    ),
    # Authorization: Bearer <value>
    (
        re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._~+/=:-]{8,})"),
        r"\1\2" + REDACTED,
    ),
    # Provider-style keys.
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{12,}\b"), "<key:redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<aws-key:redacted>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "<gh-token:redacted>"),
]


def redact(text: Optional[str]) -> str:
    """Return ``text`` with recognised credentials replaced by placeholders."""
    if not text:
        return ""
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_obj(value: object) -> object:
    """Recursively redact every string inside a JSON-like structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    return value


def redact_url(url: Optional[str]) -> str:
    """Redact just the credential-bearing part of a URL, keeping it inspectable."""
    if not url:
        return ""
    return redact(url)
