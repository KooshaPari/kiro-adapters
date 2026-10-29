"""The vocabulary the normalized session envelope is built from.

Source identifiers, launch modes, states, capability names and the shared
history-window bound live here rather than beside the envelope itself so that
an adapter can import a single name without pulling in the dataclasses, and so
that two adapters cannot disagree about what a capability is called.

Every name in this module is re-exported from :mod:`.model`; nothing here is
architecture, it is only agreed-upon spelling.
"""

from __future__ import annotations

import time
from typing import Any

# -- source identifiers -----------------------------------------------------
# Namespaced by the thing that actually produced the observation. A consumer
# can always tell a first-hand native source from a second-hand projection.
SOURCE_JCODE_FS = "jcode-fs"
SOURCE_KCODE_FS = "kcode-fs"
SOURCE_CODEX_FS = "codex-fs"
SOURCE_CODEX_APP_SERVER = "codex-app-server"
SOURCE_KIROCREW_LEDGER = "kirocrew-ledger"
SOURCE_HERDR_PANE = "herdr-pane"
SOURCE_SHARECLI_PROC = "sharecli-proc"
SOURCE_FORGE_FS = "forge-fs"

# -- launch modes -----------------------------------------------------------
LAUNCH_INTERACTIVE = "interactive"
LAUNCH_HEADLESS = "headless"
LAUNCH_UNKNOWN = "unknown"

# -- states -----------------------------------------------------------------
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"
STATE_DONE = "done"
STATE_STALE = "stale"
STATE_EXITED = "exited"
STATE_UNKNOWN = "unknown"

# -- capability names -------------------------------------------------------
CAP_DISCOVER_PROCESS = "discover_process"
CAP_DISCOVER_SESSION = "discover_session"
CAP_OBSERVE_SNAPSHOT = "observe_snapshot"
CAP_OBSERVE_STREAM = "observe_stream"
CAP_JOIN_LIVE = "join_same_live_session"
CAP_RESUME = "resume_new_execution"
CAP_INPUT = "input"
CAP_INTERRUPT = "interrupt"
CAP_PERMISSIONS = "permissions"
CAP_TERMINAL_RESIZE = "terminal_resize"
CAP_DETACH = "detach"
CAP_READ_HISTORY = "read_native_history"
CAP_ENROLL = "enroll_existing"

ALL_CAPABILITIES = (
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_OBSERVE_SNAPSHOT,
    CAP_OBSERVE_STREAM,
    CAP_JOIN_LIVE,
    CAP_RESUME,
    CAP_INPUT,
    CAP_INTERRUPT,
    CAP_PERMISSIONS,
    CAP_TERMINAL_RESIZE,
    CAP_DETACH,
    CAP_READ_HISTORY,
    CAP_ENROLL,
)


# Ceiling on ``history(limit=...)`` for every file-backed adapter. The
# transcript slice is caller-selected, which means a caller could ask for the
# whole transcript; one shared constant keeps jcode, kcode and the crew
# adapter from disagreeing about what "bounded" means. It lives here rather
# than in one adapter so the crew adapter does not have to import jcode's
# module to share a bound.
MAX_HISTORY_LIMIT = 200


def clamp_history_limit(value: Any, ceiling: int = MAX_HISTORY_LIMIT) -> int:
    """Clamp a caller-supplied history window into ``[0, ceiling]``.

    A non-numeric, negative or absurd value is not an error: it is a request
    for a window this layer will not honour, so it becomes 0 (or the ceiling)
    rather than raising. That is what makes the bound hold even when the bound
    is applied by three adapters and one registry.
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 0
    if limit <= 0:
        return 0
    return min(limit, ceiling)


def now_iso() -> str:
    """RFC3339 UTC timestamp used for every ``observed_at``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
