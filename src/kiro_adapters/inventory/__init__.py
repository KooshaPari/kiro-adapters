"""Session inventory: find, identify and read the agent sessions on this host.

This is the "open process detection" layer. It answers one question for
every consumer app -- Paperclip, KiroCrew, Tracera, AgilePlus, Discord, Orca,
OmniRoute, Khostty -- and answers it the same way each time:

*which agent sessions exist right now, what is each one, and what can
actually be done with it.*

Every adapter here is **read-only**. Nothing in this package starts a
daemon, spawns an engine, resumes a session, or writes to a harness store.
That is a hard boundary, not a style preference: the ACP path can start the
jcode daemon as a side effect, so a discovery call that is allowed to do
that is a host mutation disguised as a read.

The one capability this package deliberately does *not* offer is control.
Control belongs to the harness's own protocol (ACP) and to the broker's
lease, and it is declared per session with an explicit reason when it is
unavailable.
"""

from __future__ import annotations

from .model import (  # noqa: F401
    ALL_CAPABILITIES,
    CAP_DETACH,
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_INPUT,
    CAP_INTERRUPT,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_OBSERVE_STREAM,
    CAP_PERMISSIONS,
    CAP_READ_HISTORY,
    CAP_RESUME,
    CAP_TERMINAL_RESIZE,
    LAUNCH_HEADLESS,
    LAUNCH_INTERACTIVE,
    LAUNCH_UNKNOWN,
    MAX_HISTORY_LIMIT,
    SOURCE_CODEX_APP_SERVER,
    SOURCE_CODEX_FS,
    SOURCE_FORGE_FS,
    SOURCE_HERDR_PANE,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    SOURCE_KIROCREW_LEDGER,
    SOURCE_SHARECLI_PROC,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WORKING,
    Capability,
    ProcessInstance,
    SessionEnvelope,
    envelope_sort_key,
    make_session_uid,
    merge_capabilities,
    now_iso,
    summarize,
)

# The registry is the one entry point consumers should use: it fans out over
# every adapter below and never raises, never starts a daemon and never spawns
# a probe process.
from .registry import (  # noqa: F401
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SESSION_LIMIT,
    KNOWN_SOURCES,
    MAX_SESSION_LIMIT,
    available_sources,
    enumerate_sessions,
    find_session,
    inventory_summary,
    parse_session_uid,
    session_history,
)

__all__ = [
    "Capability",
    "ProcessInstance",
    "SessionEnvelope",
    "envelope_sort_key",
    "make_session_uid",
    "merge_capabilities",
    "now_iso",
    "summarize",
    "MAX_HISTORY_LIMIT",
    "KNOWN_SOURCES",
    "available_sources",
    "enumerate_sessions",
    "find_session",
    "inventory_summary",
    "parse_session_uid",
    "session_history",
]
