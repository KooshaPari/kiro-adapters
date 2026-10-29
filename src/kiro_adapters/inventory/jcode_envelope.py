"""Envelope construction for the jcode file store.

This is the one place that turns a parsed record plus the process/hint
evidence into a :class:`SessionEnvelope`. It is deliberately conservative:
a hint never becomes a launch-mode verdict, and a failed process probe never
becomes a death.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import processes
from .model import (
    LAUNCH_UNKNOWN,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNKNOWN,
    SessionEnvelope,
    make_session_uid,
    now_iso,
    sanitize_native_id,
)

# The source's own "Active" string. jcode uses a handful of these; anything
# else is reported as unknown rather than mapped to a guess.
_STATUS_MAP = {
    "Active": STATE_RUNNING,
    "Running": STATE_RUNNING,
    "Idle": STATE_IDLE,
    "Done": STATE_IDLE,
    "Exited": STATE_EXITED,
}


class JcodeStoreEnvelopeMixin:
    """Hints, state and envelope assembly for the jcode file store."""

    def _launch_mode(
        self,
        native_id: str,
        pid: Optional[int],
        client_hints: Dict[int, str],
        input_hints: set,
    ) -> str:
        """The launch mode this source can *prove*.

        The file store proves none. ``client_sessions`` and
        ``client-input-session_*`` are hints, not evidence of mode, so every
        session is reported as ``unknown`` rather than inheriting a guess from
        an artifact that may be stale or written for another reason.
        """
        return LAUNCH_UNKNOWN

    def _launch_hints(
        self,
        native_id: str,
        pid: Optional[int],
        client_hints: Dict[int, str],
        input_hints: set,
    ) -> tuple:
        """Non-authoritative artifacts that bear on the launch mode.

        Returned as evidence strings so a consumer can weigh them itself. They
        must never be upgraded to a ``launch_mode`` verdict by this adapter.
        """
        hints: list = []
        if pid is not None and pid in client_hints:
            hints.append(
                "hint:client_sessions/{} present (interactive client may be attached)".format(
                    pid
                )
            )
        elif pid is not None and (self.client_sessions_dir / str(pid)).exists():
            hints.append(
                "hint:client_sessions/{} present (interactive client may be attached)".format(
                    pid
                )
            )
        for client_pid, client_session in client_hints.items():
            if client_session == native_id and (
                pid is None or client_pid != pid
            ):
                hints.append(
                    "hint:client_sessions/{} names this session (interactive client may be attached)".format(
                        client_pid
                    )
                )
        if native_id in input_hints:
            hints.append(
                "hint:client-input-session_* artifact names this session "
                "(input session existed)"
            )
        return tuple(hints)

    def _state(
        self,
        record: Dict[str, Any],
        pid: Optional[int],
        process_instance,
        verdict: Optional[str] = None,
    ) -> str:
        """The session state this source can actually justify.

        ``process_instance`` is only present when the pid could be bound to a
        process (pid + start identity + boot id). When the platform reports a
        live pid but no start identity, no instance is emitted and the state
        is decided by ``verdict`` instead, so "alive but unidentifiable" is
        never mislabelled "exited", and a failed probe is never read as a
        death.
        """
        if pid is None:
            # No active_pids entry. The session may have exited, or the store
            # may be from a previous boot. "stale" is the honest word: a
            # missing heartbeat makes a session stale, not necessarily exited.
            return STATE_STALE
        if process_instance is None:
            if verdict == processes.GONE:
                # The pid was absent from a process table that was read.
                return STATE_EXITED
            if verdict != processes.ALIVE:
                # The probe could not answer. A failed probe is not evidence
                # of exit, so the state stays unknown.
                return STATE_UNKNOWN
            # Alive, but its start identity is not readable here, so no
            # process identity is emitted. The process exists, so the state
            # may not be "exited".
        mapped = _STATUS_MAP.get(str(record.get("status", "")))
        return mapped or STATE_RUNNING

    # -- the inventory -----------------------------------------------------
    def enumerate(self) -> List[SessionEnvelope]:
        """Every session this store knows about, as envelopes.

        Sessions with no provable native id are skipped entirely: a pid
        alone is not an identity.
        """
        if not self.exists():
            return []
        client_hints = self._client_binding_hints()
        input_hints = self._input_session_hints()
        observed = now_iso()

        # Pass 1: read the records, but only the cheap identity fields. The
        # `messages` array is the full transcript and can be megabytes, so it
        # is never read during enumeration -- only on an explicit history
        # request.
        records: List[tuple] = []
        pids: List[int] = []
        for path in self._session_files():
            record = self._read_record(path, projection_only=True)
            if record is None:
                continue
            native_id = record.get("id")
            if not isinstance(native_id, str) or not native_id:
                # Fall back to the file name, which is the native id.
                native_id = path.stem
            # A native id becomes a session uid, an evidence ref and (on
            # failure) a detail string: control characters must not survive.
            native_id = sanitize_native_id(native_id)
            if not native_id:
                continue
            pid = self._pid_for(native_id)
            if pid is not None:
                pids.append(pid)
            records.append((native_id, record, pid))

        # Pass 2: one batched process-table read for every pid we care about.
        # Skipped entirely when the caller forbade spawning a probe process.
        if self.probe_processes:
            live = processes.describe_many(pids)
            # Pids the platform can see but cannot bind to a process (no start
            # identity) get no process identity on the envelope. Ask for a
            # liveness verdict so "gone" and "could not tell" stay distinct:
            # a pid file is a claimed pointer, not proof of a process.
            unbound = [pid for pid in pids if pid not in live]
            verdicts = processes.liveness_many(unbound) if unbound else {}
        else:
            live = {}
            verdicts = {}

        envelopes: List[SessionEnvelope] = []
        for native_id, record, pid in records:
            process_instance = live.get(pid) if pid else None
            verdict = verdicts.get(pid) if pid else None
            launch_mode = self._launch_mode(native_id, pid, client_hints, input_hints)
            launch_hints = self._launch_hints(
                native_id, pid, client_hints, input_hints
            )
            # The record was actually read and parsed for this session (an
            # unreadable one is skipped above), so the read outcome that the
            # capability declaration cites is the observed one.
            capabilities = self._capabilities(pid, process_instance, record_read=True)
            envelopes.append(
                SessionEnvelope(
                    session_uid=make_session_uid(self.source, native_id),
                    native_session_id=native_id,
                    source=self.source,
                    host_id=self.host_id,
                    namespace_id=self.namespace_id,
                    harness_kind=self.harness_kind,
                    harness_version=self.harness_version,
                    harness_lineage=self.harness_lineage,
                    process_instance=process_instance,
                    runtime_incarnation=None,
                    launch_mode=launch_mode,
                    launch_hints=launch_hints,
                    workspace=record.get("working_dir"),
                    title=record.get("title"),
                    state=self._state(record, pid, process_instance, verdict),
                    observed_at=observed,
                    source_updated_at=record.get("updated_at"),
                    evidence_ref="jcode-fs:{}".format(native_id),
                    capabilities=capabilities,
                    # Not provable from the file store. A client artifact may
                    # name this pid, but nothing here proves a human can type
                    # into the session, so this stays False until a live probe
                    # says otherwise.
                    externally_writable=False,
                    parent_session_id=record.get("parent_id"),
                )
            )
        return envelopes
