"""Where HERDR's session surface is, and how tightly a read is bounded.

HERDR publishes exactly one read-only structured verb that this reader needs::

``herdr api snapshot``
    Prints the live session snapshot as JSON on stdout. The payload is an
    envelope, not a bare snapshot::

        {"id": "<request-id>",
         "result": {"type": "snapshot",
                    "snapshot": {"version": "0.9.3", "protocol": 22,
                                 "focused_workspace_id": "...",
                                 "workspaces": [...], "tabs": [...],
                                 "panes": [...], "agents": [...],
                                 "layouts": [...]}}}

    Observed on 2026-11-05 against ``herdr 0.9.3`` / protocol ``22``. The
    verb talks to the running server over ``~/.config/herdr/herdr.sock``;
    it is a CLI over a socket, never a plain file read.

``herdr api schema``
    Prints the bundled API schema. Not read here: the reader projects an
    allowlist of fields, so a schema that grows cannot widen the envelope.

``herdr hostops``-style verbs do **not** exist; ``herdr --help`` lists
``api``, ``agent``, ``pane``, ``workspace``, ``tab``, ``session``,
``worktree``, ``notification``, ``config``, ``machine``, ``channel``,
``integration``, ``status`` and ``completion``. The ``agent``/``pane``
families carry control verbs (``agent prompt``, ``pane send-text``), and this
adapter never calls them: a discovery read is not allowed to type into a
pane. Only ``api snapshot`` is invoked.

**Coverage limit, stated here because a consumer must not infer otherwise.**
The snapshot lists agents HERDR itself enrolled -- an agent reaches this list
only by self-reporting into HERDR's socket with the ``HERDR_ENV`` and
``HERDR_PANE_ID`` variables HERDR injects into panes it manages. An agent
started outside HERDR is absent from this snapshot even while its process is
running. This reader therefore reports *herdr-enrolled* sessions, not
"every agent session on the host"; the process lane (``processes``) is the
surface that sees externally started agents.

Everything below is about bounding the read: binary resolution, wall-clock
timeout, byte ceiling on captured stdout, and record count. Nothing here
starts anything at import time, and nothing here writes HERDR config or
state.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping, Optional

# The binary every read goes through. ``HERDR_BIN`` is the documented
# override; without it the PATH lookup decides, and a PATH miss means "this
# host has no HERDR", not "the reader is broken".
BINARY_NAME = "herdr"
BINARY_ENV = "HERDR_BIN"

# Wall-clock cap for one snapshot read. The observed snapshot answered in
# ~1.2s on a host with 13 panes; the cap exists so a wedged server cannot
# hold a consumer's inventory call open.
DEFAULT_TIMEOUT_SECS = 5.0
MAX_TIMEOUT_SECS = 30.0
TIMEOUT_ENV = "HERDR_TIMEOUT_SECS"

# Byte ceiling on captured stdout. A 13-pane snapshot was ~16 KiB; a busy
# host with hundreds of panes still fits in a few hundred KiB. Past the
# ceiling the read is refused rather than parsed, because an unbounded
# stdout is how a probe becomes a memory problem.
MAX_STDOUT_BYTES = 2 * 1024 * 1024

# Record ceiling. HERDR's snapshot is live, so it is naturally small; this
# mirrors the registry's per-call ceiling and ShareCLI's reader.
MAX_RECORDS = 500
DEFAULT_RECORD_LIMIT = 200

# The one verb this reader uses. Kept as a tuple so an argv is built by
# concatenation and never through a shell.
SNAPSHOT_VERB = ("api", "snapshot")

# The snapshot keys this adapter is willing to project. An allowlist, not a
# denylist: a field HERDR adds later -- or one of the ``tokens``/``scroll``
# mappings, whose value types this adapter does not claim to understand --
# defaults to excluded. In particular the per-agent ``tokens`` mapping is
# never projected: whether its ``mach`` value is a machine label or
# something else, an unclassified mapping has no business in an envelope.
PROJECTED_KEYS = (
    "pane_id",
    "agent",
    "agent_status",
    "cwd",
    "terminal_title_stripped",
    "workspace_id",
    "tab_id",
)

# One line a consumer can quote for the enrollment limitation. It rides in
# both the capability detail and ``evidence_ref`` so the caveat survives
# projection into another app's UI.
ENROLLMENT_SCOPE_NOTE = (
    "herdr-enrolled sessions only; agents started outside herdr do not "
    "self-report and are absent from this snapshot"
)

# HERDR's own agent_status words, mapped onto the envelope's vocabulary. A
# word outside this table is not guessed at: the envelope gets ``unknown``
# and the raw claim survives only in ``evidence_ref``. ``working`` was the
# only value observed on 2026-11-05; the rest are the plausible set, and an
# unrecognised value degrades honestly rather than being coerced.
STATE_WORDS = {
    "working": "working",
    "idle": "idle",
    "blocked": "blocked",
    "done": "done",
    "exited": "exited",
    "stale": "stale",
    "running": "running",
    "unknown": "unknown",
}


def resolve_binary(
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The HERDR executable to read through, or ``None``.

    Same precedence as every other CLI-backed reader in this package: an
    explicit constructor argument, then ``HERDR_BIN``, then ``PATH``. An
    explicit value is used as given (tests point it at a fake), but an empty
    string is treated as "unset" rather than as a path.
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
    for candidate in (value, env.get(TIMEOUT_ENV)):
        try:
            seconds = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(seconds, MAX_TIMEOUT_SECS)
    return DEFAULT_TIMEOUT_SECS


def config_dir(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """HERDR's per-user config directory, where its sockets live.

    Used for presence reporting only. The reader never opens the socket
    itself and never parses a socket; ``herdr api snapshot`` is the whole
    surface, because the wire framing is HERDR's to define.
    """
    env = os.environ if environ is None else environ
    try:
        base = env.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
        return root / "herdr"
    except (RuntimeError, OSError):
        return None


def socket_path(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The documented server socket path, for presence reporting only."""
    directory = config_dir(environ)
    return directory / "herdr.sock" if directory is not None else None


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
    "ENROLLMENT_SCOPE_NOTE",
    "MAX_RECORDS",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_SECS",
    "PROJECTED_KEYS",
    "SNAPSHOT_VERB",
    "STATE_WORDS",
    "TIMEOUT_ENV",
    "clamp_limit",
    "config_dir",
    "resolve_binary",
    "resolve_timeout",
    "socket_path",
]
