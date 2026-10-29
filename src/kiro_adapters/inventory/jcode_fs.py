"""The jcode / KCode on-disk session store.

jcode keeps one JSON file per session under ``<store>/sessions/`` and three
sibling directories that together make a session identifiable:

``sessions/<native_id>.json``
    The session record. Carries the native id, the working directory, the
    provider and model, the last known pid, the status and the parent
    session id.

``active_pids/<native_id>``
    A file whose entire content is the pid of the process currently serving
    that session. Several sessions can name the same pid.

``client_sessions/<pid>``
    The inverse map from pid to session id. Its presence for a session's pid
    is a *hint* that an interactive TUI client may be bound to that pid. It is
    not proof: a stale entry, a client that has since detached, or a writer
    that recorded the file for another reason all produce the same artifact.

``client-input-session_<short_name>_<...>``
    The input-session artifact family. Its presence is a *hint* that a headless
    input session once existed for that session id. It is not proof of a
    headless launch, and its absence proves nothing about the launch mode.

Neither artifact family is an authority on launch mode. This adapter therefore
reports ``launch_mode == "unknown"`` and ``externally_writable == False`` for
every session it discovers, and surfaces the artifacts separately as
``launch_hints`` evidence. A consumer that wants a mode verdict must probe the
live session through the harness's own protocol, not read these files.

This adapter is strictly read-only. It never starts a daemon, never spawns an
engine, never writes to the store, and never resumes anything. That matters:
the ACP path can start the jcode daemon as a side effect, and a *discovery*
call must not be able to mutate the host.

The store directory is always passed in by the caller. It is never hardcoded
to ``~/.jcode``: the KCode fork's ACP reads ``~/.jcode`` while the fork's own
store is ``~/.kcode``, and the ``jcode`` on PATH may be a different build
than the one that wrote the store. The caller resolves the lineage.

The implementation is split so no single module carries the whole surface:
layout and path confinement live in :mod:`.jcode_paths`, the capped readers
in :mod:`.jcode_readers`, the transcript surface in :mod:`.jcode_history`,
the declarations in :mod:`.jcode_capabilities`, and envelope assembly in
:mod:`.jcode_envelope`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .jcode_capabilities import JcodeStoreCapabilityMixin
from .jcode_envelope import JcodeStoreEnvelopeMixin
from .jcode_history import JcodeStoreHistoryMixin
from .jcode_paths import JcodeStoreLayoutMixin
from .jcode_readers import JcodeStoreReadMixin
from .model import (  # noqa: F401  (re-exported for callers and tests)
    MAX_HISTORY_LIMIT,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
)

# Ceiling on any single JSON record read from the store. A session record is
# the full transcript and can be large, but it is bounded by what the harness
# wrote, not by anything this adapter controls. An attacker (or a corrupt
# writer) can drop a multi-gigabyte file into ``sessions/``, so the read is
# capped and an over-cap record is refused whole rather than parsed from a
# truncated prefix: a half-decoded JSON document would be a lie about the
# record's contents.
DEFAULT_MAX_RECORD_BYTES = 8 * 1024 * 1024

# Ceiling on the number of session records a single enumeration will touch.
# ``sessions/`` is a directory anyone can write to; without a ceiling a store
# with a million records would make one inventory call read a million files.
MAX_SESSION_FILES = 5000

# Ceiling on the number of ``client-input-session_*`` artifacts scanned for a
# launch hint. The scan reads each body, so it is bounded like the record reads.
MAX_INPUT_SESSION_HINTS = 2000


class JcodeStore(
    JcodeStoreLayoutMixin,
    JcodeStoreReadMixin,
    JcodeStoreHistoryMixin,
    JcodeStoreCapabilityMixin,
    JcodeStoreEnvelopeMixin,
):
    """Read-only view of one jcode-family session store."""

    def __init__(
        self,
        store_dir: os.PathLike,
        source: str = SOURCE_JCODE_FS,
        harness_kind: str = "jcode",
        harness_version: Optional[str] = None,
        harness_lineage: Optional[str] = None,
        host_id: str = "unknown",
        namespace_id: str = "unknown",
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        probe_processes: bool = True,
    ) -> None:
        self.store_dir = Path(store_dir)
        self.source = source
        self.harness_kind = harness_kind
        self.harness_version = harness_version
        self.harness_lineage = harness_lineage
        self.host_id = host_id
        self.namespace_id = namespace_id
        # Every JSON record read from this store is capped at this many bytes;
        # an over-cap record is refused instead of parsed.
        self.max_record_bytes = max_record_bytes
        # Liveness probing reads the host process table (``ps`` on POSIX). That
        # is a read-only OS query, but a caller that must not start any
        # subprocess at all -- the registry behind the read-only MCP surface --
        # turns it off and accepts that ``discover_process`` then stays
        # unproven for every session.
        self.probe_processes = probe_processes


def default_store(probe_processes: bool = True) -> JcodeStore:
    """The jcode store for the current user, if one exists.

    ``~/.jcode`` is the upstream layout. The KCode fork keeps ``~/.kcode``
    for its own sessions but its ACP still reads ``~/.jcode``; the caller
    decides which lineage it is talking about, so this helper only picks the
    conventional upstream location.
    """
    return JcodeStore(
        Path.home() / ".jcode",
        source=SOURCE_JCODE_FS,
        probe_processes=probe_processes,
    )


def kcode_store(probe_processes: bool = True) -> JcodeStore:
    """The KCode fork's own store, distinct from ``~/.jcode``."""
    return JcodeStore(
        Path.home() / ".kcode",
        source=SOURCE_KCODE_FS,
        harness_kind="kcode",
        harness_lineage="kcode-fork",
        probe_processes=probe_processes,
    )
