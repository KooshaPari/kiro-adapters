"""Projection-sanitising helpers for the normalized session envelope.

A title, a working directory and a native id are all user-derived text. The
envelope is a projection consumed by other applications (tool results,
dashboards, log lines), so raw content never travels in it: credentials are
redacted, control characters are removed so source data cannot forge a log
line or a failure detail, and each field is length-bounded.

The three sanitising modes are deliberately different, and the difference is
the point:

* user text is redacted, word-collapsed and ellipsised;
* a native id is word-collapsed and bounded but not redacted, because it is an
  identifier that must still match the source;
* a machine-state value is only control-stripped and bounded, because
  consumers compare it by exact equality and any other treatment would change
  what the value means.
"""

from __future__ import annotations

import re
from typing import Optional

from ..redact import redact

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE_RUN = re.compile(r"[ ]{2,}")

MAX_TITLE_CHARS = 200
MAX_WORKSPACE_CHARS = 512
MAX_NATIVE_ID_CHARS = 256
# Environment-derived fields (host, namespace, harness version, executable
# path) are still text that came from somewhere we do not control, so they are
# redacted and bounded like any other user-derived string.
MAX_ENV_FIELD_CHARS = 256
# Machine-state fields (state, launch_mode, source_updated_at) must stay safe
# to compare by equality, so they are only stripped of control characters and
# length-bounded. They are never word-collapsed or redacted, which would change
# a value a consumer compares against.
MAX_MACHINE_STATE_CHARS = 64


def strip_control_chars(value: str) -> str:
    """Remove control characters, keeping printable text intact."""
    return _CONTROL_CHARS.sub("", value)


def sanitize_native_id(value: Optional[str]) -> str:
    """A native id safe to place in a projection or in a failure detail.

    Control characters (newlines, NUL, ESC) are removed rather than escaped:
    such an id is not a legitimate file name, and letting one through would
    let source data forge log lines and detail strings. Returns ``""`` when
    nothing printable remains, which callers must treat as "no identity".
    """
    if not value:
        return ""
    cleaned = strip_control_chars(value)
    cleaned = _SPACE_RUN.sub(" ", cleaned).strip()
    if len(cleaned) > MAX_NATIVE_ID_CHARS:
        cleaned = cleaned[:MAX_NATIVE_ID_CHARS]
    return cleaned


def sanitize_user_text(value: Optional[str], limit: int) -> Optional[str]:
    """Redact and bound user-derived text such as a title or a directory."""
    if value is None:
        return None
    cleaned = redact(strip_control_chars(value))
    cleaned = _SPACE_RUN.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 1)].rstrip() + "\u2026"
    return cleaned


def sanitize_machine_value(value: Optional[str], limit: int) -> Optional[str]:
    """Make a machine-state string safe without changing what it means.

    ``state``, ``launch_mode`` and ``source_updated_at`` are compared by exact
    equality by consumers, so they are not redacted, word-collapsed or
    ellipsised. Control characters are still removed: a newline or ESC in a
    machine field could forge a log line exactly as it could in a title.
    """
    if value is None:
        return None
    cleaned = strip_control_chars(value)
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned
