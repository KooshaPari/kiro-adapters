"""Envelope construction and capability declarations for the Kiro Crew store.

The ledger names sessions but records no pid, so nothing here claims
liveness: state stays ``unknown`` and ``process_instance`` stays ``None``.
"""

from __future__ import annotations

import time
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
    SOURCE_KIROCREW_LEDGER,
    STATE_UNKNOWN,
    Capability,
    SessionEnvelope,
    make_session_uid,
    now_iso,
    sanitize_native_id,
)


def _epoch_ms_to_iso(value: Any) -> Optional[str]:
    """An epoch-millisecond timestamp as RFC3339 UTC, or None."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(millis / 1000.0))


class KiroCrewStoreEnvelopeMixin:
    """Envelope assembly, metadata projection and capabilities."""

    # -- the inventory -----------------------------------------------------
    def enumerate(self, limit: int = 200) -> List[SessionEnvelope]:
        """Every live session the crew ledger names, as envelopes.

        A session whose metadata line cannot be read is skipped: without the
        metadata there is no id the session itself asserts, and a path name
        alone is not enough to say a session exists right now.
        """
        if not self.exists():
            return []
        live = self._live_sessions()
        observed = now_iso()
        envelopes: List[SessionEnvelope] = []
        self._session_files = {}
        for session_path, at_ms in live.items():
            if len(envelopes) >= limit:
                break
            file_path = self._resolve_session_file(session_path)
            if file_path is None:
                continue
            metadata = self._read_metadata(file_path)
            if metadata is None:
                continue
            native_id, own_id = self._native_id(metadata, session_path)
            native_id = sanitize_native_id(native_id)
            if not native_id:
                continue
            self._session_files[native_id] = file_path
            envelopes.append(
                SessionEnvelope(
                    session_uid=make_session_uid(SOURCE_KIROCREW_LEDGER, native_id),
                    native_session_id=native_id,
                    source=SOURCE_KIROCREW_LEDGER,
                    host_id=self.host_id,
                    namespace_id=self.namespace_id,
                    harness_kind="kirocrew",
                    harness_version=self.harness_version,
                    harness_lineage=self.harness_lineage,
                    process_instance=None,
                    runtime_incarnation=None,
                    launch_mode=LAUNCH_UNKNOWN,
                    workspace=self._workspace(metadata),
                    title=self._title(metadata),
                    state=STATE_UNKNOWN,
                    observed_at=observed,
                    source_updated_at=_epoch_ms_to_iso(at_ms),
                    evidence_ref=self._evidence(session_path, native_id, own_id),
                    capabilities=self._capabilities(),
                    externally_writable=False,
                    parent_session_id=None,
                )
            )
        return envelopes

    def _workspace(self, metadata: Dict[str, Any]) -> Optional[str]:
        """The crew's notion of where the session ran."""
        for key in ("project", "workspace", "cwd"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _title(self, metadata: Dict[str, Any]) -> Optional[str]:
        """A human label, taken from the title or the selected agent."""
        value = metadata.get("title")
        if isinstance(value, str) and value.strip():
            return value.strip()
        context = metadata.get("execution_context")
        if isinstance(context, dict):
            for key in ("selection_name", "template_id"):
                inner = context.get(key)
                if isinstance(inner, str) and inner:
                    return inner
        return None

    def _evidence(
        self, session_path: str, native_id: str, own_id: bool
    ) -> str:
        """Where this envelope came from, in one line.

        The id provenance matters: an id the session asserted is a stronger
        claim than a name taken from a directory.
        """
        origin = "session-id" if own_id else "session-path-basename"
        return "kirocrew-ledger:{}#{}".format(session_path, origin)

    def _capabilities(self) -> Dict[str, Capability]:
        """What the ledger can actually do for one session.

        The ledger is observation only. It names sessions; it cannot control
        them, and it records no pid, so process discovery is not available.
        """
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved(
            "kiro session-index/*.jsonl add/remove ledger"
        )
        caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
            "the crew ledger records no pid; liveness cannot be proven from "
            "the session artifacts"
        )
        caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
            "kiro session jsonl metadata line"
        )
        caps[CAP_READ_HISTORY] = Capability.proved(
            "kiro session jsonl non-metadata lines"
        )
        caps[CAP_OBSERVE_STREAM] = Capability.unsupported(
            "a session file is written between turns; there is no live "
            "stream to attach to"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "the ledger names no live owner; joining a crew session goes "
            "through the crew gateway, not through a file read"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "resuming a crew session is the gateway's job; a file read cannot "
            "restart a crew execution"
        )
        # Enrollment is a write into the crew session registry: it needs the
        # crew gateway's own enroll path, not a read of the file ledger. A
        # read-only observer can name an existing session but cannot enroll
        # it, so this stays unsupported regardless of what the ledger exposes.
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment requires the crew gateway's own session path; the "
            "read-only file ledger can observe a session but cannot enroll an "
            "existing one"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "the file ledger holds no connection; there is nothing to detach"
        )
        return caps

    # -- agents ------------------------------------------------------------
    def agents(self) -> List[str]:
        """The names of the crew's agent definitions.

        Names only. The definitions are not read, and no model is called:
        this is an inventory surface, not a place to start work.
        """
        if not self.agents_dir.is_dir():
            return []
        try:
            return sorted(path.name for path in self.agents_dir.glob("*.json"))
        except OSError:
            return []
