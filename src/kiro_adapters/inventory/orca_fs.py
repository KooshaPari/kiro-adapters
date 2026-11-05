"""Orca's agent-hook status document, as a read-only session inventory.

Orca is the third-ranked attachable surface in
``docs/consumer-capability-matrix.md`` because it already writes its state to
disk: ``agent-hooks/endpoint.env`` states the port, transport and token
presence, and ``agent-hooks/last-status.json`` carries Orca's own view of its
agent sessions. The *first* useful read is therefore a plain file read, which
is what this adapter does. No hook is called and no daemon is contacted.

What it can and cannot say is bounded by the file:

* an ``entries`` member is one agent session and becomes one envelope;
* the artifact records no pid, so ``process_instance`` is always ``None`` and
  ``state`` is whatever Orca wrote, never a liveness claim;
* there is no transcript in the file, so ``history`` answers "unsupported"
  rather than pretending the status blob is a conversation.

On the host this adapter was written against, ``entries`` is empty
(``{"version": 2, "entries": {}, "authorityCommitments": {}}``). The *entry*
field names are therefore unproven, and only an allowlist of plausible,
non-secret keys is read. Everything else in an entry -- including any
token-like key -- is ignored by construction instead of being filtered after
the fact.

This module never writes to the Orca profile and never reads the hook token.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import (
    CAP_DETACH,
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_OBSERVE_STREAM,
    CAP_READ_HISTORY,
    CAP_RESUME,
    LAUNCH_UNKNOWN,
    SOURCE_ORCA_FS,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WORKING,
    Capability,
    SessionEnvelope,
    make_session_uid,
    now_iso,
    sanitize_native_id,
)
from .orca_paths import OrcaStoreLayoutMixin, default_orca_dir

# Entry field allowlist. Each tuple is ordered: the first present key wins.
# None of these names can carry a credential, and no other field is read.
_STATE_KEYS = ("state", "status", "phase", "agentState")
_TITLE_KEYS = ("title", "label", "name", "agentName", "agent")
_WORKSPACE_KEYS = ("workspace", "cwd", "workingDirectory", "project", "workspacePath")
_UPDATED_KEYS = ("updatedAt", "updated_at", "lastActivityAt", "at", "timestamp")
_ID_KEYS = ("id", "sessionId", "session_id")

# Orca's state vocabulary is its own. Map what we recognise onto the shared
# envelope vocabulary; anything else stays ``unknown`` rather than being
# passed through as a machine value a consumer might compare against.
_STATE_MAP = {
    "idle": STATE_IDLE,
    "running": STATE_RUNNING,
    "working": STATE_WORKING,
    "busy": STATE_WORKING,
    "blocked": STATE_BLOCKED,
    "waiting": STATE_BLOCKED,
    "done": STATE_DONE,
    "complete": STATE_DONE,
    "completed": STATE_DONE,
    "finished": STATE_DONE,
    "stale": STATE_STALE,
}


def _first_string(source: Dict[str, Any], keys: tuple) -> Optional[str]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_state(value: Optional[str]) -> str:
    if not value:
        return STATE_UNKNOWN
    return _STATE_MAP.get(value.strip().lower(), STATE_UNKNOWN)


def _timestamp_to_iso(value: Any) -> Optional[str]:
    """Epoch seconds, epoch milliseconds or RFC3339 text as RFC3339 UTC.

    Only a plausible epoch is converted; a string is returned unchanged
    because it may already be the RFC3339 value consumers compare against.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number <= 0:
        return None
    # Anything past 1e11 is milliseconds; before that it is seconds.
    seconds = number / 1000.0 if number >= 1e11 else number
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        return None


class OrcaStore(OrcaStoreLayoutMixin):
    """Read-only view of Orca's agent-hook status document."""

    def __init__(
        self,
        orca_dir,
        host_id: str = "unknown",
        namespace_id: str = "unknown",
        harness_version: Optional[str] = None,
        harness_lineage: Optional[str] = None,
    ) -> None:
        self.orca_dir = Path(orca_dir)
        self.host_id = host_id
        self.namespace_id = namespace_id
        self._declared_version = harness_version
        self.harness_lineage = harness_lineage

    # -- inventory ---------------------------------------------------------
    def enumerate(self, limit: int = 200) -> List[SessionEnvelope]:
        """One envelope per ``entries`` member, bounded by ``limit``.

        A missing, unreadable, oversized or malformed document yields the
        empty list. That is deliberate: a status file we cannot parse is not
        evidence of any session.
        """
        if not self.exists():
            return []
        document = self.read_last_status()
        if document is None:
            return []
        entries = document.get("entries")
        if not isinstance(entries, dict):
            return []

        observed = now_iso()
        version = self._version(document)
        envelopes: List[SessionEnvelope] = []
        for entry_key, entry in entries.items():
            if len(envelopes) >= limit:
                break
            if not isinstance(entry_key, str) or not isinstance(entry, dict):
                continue
            native_id = sanitize_native_id(_first_string(entry, _ID_KEYS) or entry_key)
            if not native_id:
                continue
            envelopes.append(
                SessionEnvelope(
                    session_uid=make_session_uid(SOURCE_ORCA_FS, native_id),
                    native_session_id=native_id,
                    source=SOURCE_ORCA_FS,
                    host_id=self.host_id,
                    namespace_id=self.namespace_id,
                    harness_kind="orca",
                    harness_version=version,
                    harness_lineage=self.harness_lineage,
                    process_instance=None,
                    runtime_incarnation=None,
                    launch_mode=LAUNCH_UNKNOWN,
                    workspace=_first_string(entry, _WORKSPACE_KEYS),
                    title=_first_string(entry, _TITLE_KEYS),
                    state=_normalize_state(_first_string(entry, _STATE_KEYS)),
                    observed_at=observed,
                    source_updated_at=_timestamp_to_iso(
                        next(
                            (
                                entry[key]
                                for key in _UPDATED_KEYS
                                if key in entry
                            ),
                            None,
                        )
                    ),
                    evidence_ref="orca-agent-hooks:last-status.json#entries",
                    capabilities=self._capabilities(),
                    externally_writable=True,
                    parent_session_id=None,
                )
            )
        return envelopes

    # -- evidence ----------------------------------------------------------
    def _version(self, document: Dict[str, Any]) -> Optional[str]:
        if self._declared_version:
            return self._declared_version
        version = document.get("version")
        if isinstance(version, (int, float, str)) and not isinstance(version, bool):
            return "document-version:{}".format(version)
        return None

    def endpoint(self) -> Dict[str, Any]:
        """The hook endpoint's non-secret description (port/transport/env).

        Delegates to :meth:`OrcaStoreLayoutMixin.endpoint_env`, which never
        parses the token value. Kept as a named method so evidence consumers
        do not reach into the layout mixin directly.
        """
        return self.endpoint_env()

    # -- capabilities ------------------------------------------------------
    def _capabilities(self) -> Dict[str, Capability]:
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved(
            "orca agent-hooks/last-status.json entries mapping"
        )
        caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
            "the status document records no pid and no start identity; a pid "
            "alone would not identify a session anyway"
        )
        caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
            "orca agent-hooks/last-status.json last-known snapshot"
        )
        caps[CAP_READ_HISTORY] = Capability.unsupported(
            "last-status.json carries status, not a transcript; reading the "
            "conversation would require a hook call this adapter does not make"
        )
        caps[CAP_OBSERVE_STREAM] = Capability.unsupported(
            "a file read is a snapshot; live streaming would need the local "
            "agent-hook endpoint, which is out of scope for a file source"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "joining a live Orca session is the hook endpoint's job; a status "
            "file cannot take ownership of a session"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "resuming is the Orca app's job; a file read cannot restart it"
        )
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment needs the hook endpoint's own path; a read-only file "
            "observer cannot enroll an existing session"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "a file read holds no connection; there is nothing to detach"
        )
        return caps

    # -- history -----------------------------------------------------------
    def history(self, native_id: str, limit: int = 20) -> Dict[str, Any]:
        """No transcript exists in this artifact, so say so plainly."""
        return {
            "ok": False,
            "source": SOURCE_ORCA_FS,
            "native_session_id": native_id,
            "limit": limit,
            "messages": [],
            "detail": (
                "orca's last-status.json is a status snapshot and holds no "
                "conversation; this file adapter does not call the hook "
                "endpoint"
            ),
        }


def default_store() -> OrcaStore:
    """The Orca profile for the current user, if one exists."""
    return OrcaStore(default_orca_dir())
