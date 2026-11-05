"""Where ShareCLI's session surface is, and how tightly a read is bounded.

ShareCLI publishes one machine-readable session surface and no per-session
transcript file:

``sharecli session list``
    Prints a JSON **array** of session descriptors on stdout. It emits JSON by
    default; ``--json`` is not a flag on this verb.
``sharecli session inspect <ID>``
    Prints one descriptor of the same shape.
``sharecli status --json``
    Prints process inventory (``pid``/``family``/``comm``/``state``/RSS). That
    is the process lane, already covered by ``SOURCE_SHARECLI_PROC``; it is not
    this reader's source.

The on-disk ``sessions.sqlite`` under ``~/Library/Application Support/sharecli``
was present but stale relative to the CLI's own answer during discovery, so it
is not read here. Everything below is about bounding the CLI read: binary
resolution, wall-clock timeout, byte ceiling on captured stdout, and record
count. Nothing in this module starts anything at import time.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping, Optional

# The binary every read goes through. ``SHARECLI_BIN`` is the documented
# override; without it the PATH lookup decides, and a PATH miss means "this
# host has no ShareCLI", not "the reader is broken".
BINARY_NAME = "sharecli"
BINARY_ENV = "SHARECLI_BIN"

# Wall-clock cap for one CLI read. ShareCLI answers ``session list`` in well
# under a second on a populated host; the cap exists so a wedged process
# cannot hold a consumer's inventory call open.
DEFAULT_TIMEOUT_SECS = 5.0
MAX_TIMEOUT_SECS = 30.0
TIMEOUT_ENV = "SHARECLI_TIMEOUT_SECS"

# Byte ceiling on captured stdout. A session store with thousands of entries
# still fits in a few hundred KiB; past this the read is refused rather than
# parsed, because an unbounded stdout is how a probe becomes a memory problem.
MAX_STDOUT_BYTES = 1024 * 1024

# Record ceiling. ShareCLI keeps a durable ledger, so its list can be long;
# the registry's own per-call ceiling is 500 and this mirrors it.
MAX_RECORDS = 500
DEFAULT_RECORD_LIMIT = 200

# The verbs this reader uses. Kept as tuples so an argv is built by
# concatenation and never through a shell.
SESSION_VERB = ("session", "list")
INSPECT_VERB = ("session", "inspect")

# The descriptor keys this adapter is willing to project. Anything outside
# this set -- ``resume.argv`` in particular, which can carry flags naming a
# credential, and any tool-private field ShareCLI adds later -- never reaches
# the envelope. An allowlist, not a denylist: a new key defaults to excluded.
PROJECTED_KEYS = ("id", "session_id", "harness", "cwd", "state", "confidence")

# ShareCLI's own state words, mapped onto the envelope's vocabulary. A word
# outside this table is not guessed at: the envelope gets ``unknown`` and the
# raw claim survives only in ``evidence_ref``.
STATE_WORDS = {
    "active": "running",
    "running": "running",
    "idle": "idle",
    "working": "working",
    "stale": "stale",
    "exited": "exited",
    "ended": "exited",
    "done": "done",
}


def resolve_binary(
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The ShareCLI executable to read through, or ``None``.

    Precedence: an explicit constructor argument, then ``SHARECLI_BIN``, then
    ``PATH``. An explicit value is used as given (tests point it at a fake),
    but an empty string is treated as "unset" rather than as a path.
    """
    env = os.environ if environ is None else environ
    for candidate in (explicit, env.get(BINARY_ENV)):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    # A caller-supplied environment means "look only there": a test that says
    # PATH=/nonexistent must not be answered from the real process PATH.
    search_path = None if environ is None else (env.get("PATH") or "")
    try:
        return shutil.which(BINARY_NAME, path=search_path)
    except (OSError, ValueError):
        # A broken PATH is a fact about the host, not an error to raise.
        return None


def resolve_timeout(
    value: object = None, environ: Optional[Mapping[str, str]] = None
) -> float:
    """A usable wall-clock cap, clamped to ``(0, MAX_TIMEOUT_SECS]``.

    A non-numeric or non-positive value falls back to the default instead of
    producing a timeout of zero, which would make every read fail.
    """
    env = os.environ if environ is None else environ
    candidates = [value, env.get(TIMEOUT_ENV)]
    for candidate in candidates:
        try:
            seconds = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(seconds, MAX_TIMEOUT_SECS)
    return DEFAULT_TIMEOUT_SECS


def app_support_dir(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """ShareCLI's per-user state directory on this platform, if resolvable.

    Only used for presence reporting and evidence. The reader never parses the
    sqlite ledger, because the CLI accepts an explicit ``--db`` and is the
    authority on which store it is answering from.
    """
    env = os.environ if environ is None else environ
    try:
        if os.name == "nt":  # pragma: no cover - platform dependent
            base = env.get("LOCALAPPDATA") or env.get("APPDATA")
            return Path(base) / "sharecli" if base else None
        if os.sys.platform == "darwin":  # type: ignore[attr-defined]
            return Path.home() / "Library" / "Application Support" / "sharecli"
        base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return Path(base) / "sharecli"
    except (RuntimeError, OSError):
        return None


def clamp_limit(value: object, ceiling: int = MAX_RECORDS) -> int:
    """A positive record limit, clamped to the module ceiling."""
    try:
        limit = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RECORD_LIMIT
    if limit <= 0:
        return 0
    return min(limit, ceiling)


__all__ = [
    "BINARY_ENV",
    "BINARY_NAME",
    "DEFAULT_RECORD_LIMIT",
    "DEFAULT_TIMEOUT_SECS",
    "INSPECT_VERB",
    "MAX_RECORDS",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_SECS",
    "PROJECTED_KEYS",
    "SESSION_VERB",
    "STATE_WORDS",
    "TIMEOUT_ENV",
    "app_support_dir",
    "clamp_limit",
    "resolve_binary",
    "resolve_timeout",
]
