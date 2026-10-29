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
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import processes
from .model import (
    CAP_DETACH,
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_READ_HISTORY,
    CAP_RESUME,
    LAUNCH_UNKNOWN,
    MAX_HISTORY_LIMIT,
    MAX_WORKSPACE_CHARS,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNKNOWN,
    Capability,
    SessionEnvelope,
    clamp_history_limit,
    make_session_uid,
    now_iso,
    sanitize_native_id,
)

# Fields the adapter projects from a session record. Everything else in the
# record (notably `messages`, which is the full transcript, and
# `env_snapshots`) is deliberately NOT part of the inventory projection.
_PROJECTED_FIELDS = (
    "id",
    "parent_id",
    "title",
    "created_at",
    "updated_at",
    "provider_key",
    "model",
    "working_dir",
    "short_name",
    "status",
    "last_pid",
    "last_active_at",
    "is_debug",
    "saved",
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


def _read_text_capped(path: Path, max_bytes: int) -> Optional[str]:
    """Read a UTF-8 text file, refusing anything larger than ``max_bytes``.

    Fails closed. A file over the cap, a read error and an undecodable body
    all return ``None``: there is no partial success state for a record,
    because a truncated JSON body cannot be trusted to describe the session.
    """
    if max_bytes <= 0:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if len(data) > max_bytes:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class JcodeStore:
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

    # -- layout ------------------------------------------------------------
    @property
    def sessions_dir(self) -> Path:
        return self.store_dir / "sessions"

    @property
    def active_pids_dir(self) -> Path:
        return self.store_dir / "active_pids"

    @property
    def client_sessions_dir(self) -> Path:
        return self.store_dir / "client_sessions"

    @property
    def input_sessions_dir(self) -> Path:
        return self.store_dir

    def exists(self) -> bool:
        return self.sessions_dir.is_dir()

    # -- reads -------------------------------------------------------------
    def _session_files(self) -> List[Path]:
        if not self.sessions_dir.is_dir():
            return []
        try:
            # Bounded: stop after MAX_SESSION_FILES matches instead of
            # materialising an arbitrarily large directory listing.
            files: List[Path] = []
            for path in self.sessions_dir.glob("*.json"):
                files.append(path)
                if len(files) >= MAX_SESSION_FILES:
                    break
            return sorted(files)
        except OSError:
            return []

    def _read_record(
        self, path: Path, projection_only: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Parse a session record.

        With ``projection_only`` the transcript (``messages``) and the
        environment snapshots are dropped from the result. Enumerating a
        store means touching thousands of records and the transcript is the
        bulk of each one, so it is only parsed when someone actually asks
        for the history.

        The read itself is capped at ``max_record_bytes``. A record larger
        than that is refused (``None``), which makes it invisible to the
        inventory and makes ``history`` fail closed rather than return a
        partially parsed transcript.
        """
        text = _read_text_capped(path, self.max_record_bytes)
        if text is None:
            return None
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            # A half-written record must not take the whole inventory down.
            return None
        if not isinstance(record, dict):
            return None
        if projection_only:
            return {key: record.get(key) for key in _PROJECTED_FIELDS}
        return record

    def _pid_for(self, native_id: str) -> Optional[int]:
        path = self._active_pid_path(native_id)
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not text:
            return None
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return None

    def _active_pid_path(self, native_id: Any) -> Optional[Path]:
        """The active-pid path for a native id, confined to ``active_pids/``.

        The id here is not always the file name: it can come from the ``id``
        field *inside* a session record, which is attacker-writable content.
        So the same two escapes as ``_session_record_path`` are refused --
        a traversal or absolute path in the id, and an innocent-looking name
        that is a symlink resolving outside the directory.
        """
        if not self._is_single_file_name(native_id):
            return None
        expected_name = native_id
        try:
            root = Path(os.path.realpath(self.active_pids_dir))
            resolved = Path(os.path.realpath(self.active_pids_dir / expected_name))
        except (OSError, ValueError):
            return None
        if resolved != root / expected_name:
            return None
        if not resolved.is_file():
            return None
        return resolved

    def _client_binding_hints(self) -> Dict[int, str]:
        """pid -> session id, read from ``client_sessions/<pid>``.

        These entries are hints that an interactive client *may* be attached.
        They are never treated as proof of an interactive launch: the file can
        outlive the client, and its presence does not establish that a human
        can still type into the session.
        """
        result: Dict[int, str] = {}
        if not self.client_sessions_dir.is_dir():
            return result
        try:
            entries = list(self.client_sessions_dir.iterdir())
        except OSError:
            return result
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                pid = int(entry.name)
            except ValueError:
                continue
            try:
                session_id = entry.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if session_id:
                result[pid] = session_id
        return result

    def _input_session_hints(self) -> set:
        """Session ids named by a ``client-input-session_*`` artifact.

        The file name embeds the session's short name and its id suffix; the
        file body also carries the session id under a ``session`` key when the
        writer included it. Both are tried. A match is a hint that an input
        session once existed, not proof that this session was launched
        headless, and a non-match proves nothing.
        """
        found = set()
        try:
            # Bounded: the body of every artifact is read, so the number of
            # artifacts examined is capped rather than taken from the directory.
            entries = []
            for entry in self.store_dir.glob("client-input-session_*"):
                entries.append(entry)
                if len(entries) >= MAX_INPUT_SESSION_HINTS:
                    break
        except OSError:
            return found
        for entry in entries:
            if not entry.is_file():
                continue
            body = _read_text_capped(entry, self.max_record_bytes)
            if body is None:
                continue
            session_id = None
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    for key in ("session", "session_id", "sessionId"):
                        value = data.get(key)
                        if isinstance(value, str) and value:
                            session_id = value
                            break
            except (ValueError, TypeError):
                session_id = None
            if session_id:
                found.add(session_id)
        return found

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

    def _capabilities(
        self,
        pid: Optional[int],
        process_instance,
        record_read: bool = False,
    ) -> Dict[str, Capability]:
        """What this source can actually do for one session.

        Everything here is read-only. Control capabilities are declared
        unsupported with a reason, because the file store cannot control a
        session -- only the ACP adapter can, and only on a host where the
        harness supports it.

        ``record_read`` is the *observed* outcome of reading this session's
        record. The capabilities that depend on that read (snapshot, history,
        enroll) are only proved when the read actually succeeded: a refused or
        capped read must not leave a probe behind claiming it saw the record.
        """
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved("jcode sessions/*.json")
        # Process discovery is a probe, not an inference. It is only proved
        # when this enumeration actually bound a pid to a process start
        # identity (``describe_many``/``liveness_many``). A pid file that the
        # process table could not bind proves nothing, so it stays
        # unsupported with the reason that was observed.
        if process_instance is not None:
            caps[CAP_DISCOVER_PROCESS] = Capability.proved(
                "process table bound pid {} to a start identity".format(
                    getattr(process_instance, "pid", "?")
                )
            )
        elif pid is None:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "no pid is recorded for this session, so no process was "
                "probed"
            )
        elif not self.probe_processes:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "process probing is disabled for this read; the pid file is "
                "a claimed pointer, not proof of a process"
            )
        else:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "pid {} could not be bound to a process start identity; the "
                "pid file is a claimed pointer, not proof of a process".format(
                    pid
                )
            )
        if record_read:
            caps[CAP_READ_HISTORY] = Capability.proved(
                "jcode sessions/<native_id>.json messages[]",
            )
            caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
                "jcode sessions/<native_id>.json projected fields"
            )
        else:
            refused = (
                "the session record read was refused or capped, so the record "
                "was never observed"
            )
            caps[CAP_READ_HISTORY] = Capability.unsupported(refused)
            caps[CAP_OBSERVE_SNAPSHOT] = Capability.unsupported(refused)
        # Enrollment is a write into the harness's own session registry: it
        # needs the harness session path, not a read of the file store. A
        # read-only observer can never enroll an existing session, so this
        # stays unsupported regardless of whether the record was observed.
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment requires the harness's own session path; the "
            "read-only file store can observe a session but cannot enroll an "
            "existing one"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "the file store can see a session but cannot resume it; resume "
            "must go through the harness's own resume path with a pinned "
            "binary path"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "joining a live session requires the harness protocol (ACP); the "
            "file store is observation only"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "the file store holds no connection; there is nothing to detach"
        )
        return caps

    # -- history -----------------------------------------------------------
    @staticmethod
    def _is_single_file_name(native_id: Any) -> bool:
        """True when the id is one path component and nothing else.

        The id is caller-supplied and is spliced into a file name, so a value
        carrying a separator, a NUL, or the traversal names would let a caller
        name a file outside ``sessions/``.
        """
        if not isinstance(native_id, str) or not native_id:
            return False
        if os.path.isabs(native_id):
            return False
        if native_id in (".", ".."):
            return False
        if "\x00" in native_id:
            return False
        if os.sep in native_id:
            return False
        if os.altsep and os.altsep in native_id:
            return False
        return os.path.basename(native_id) == native_id

    def _session_record_path(self, native_id: Any) -> Optional[Path]:
        """The record path for a native id, confined to ``sessions/``.

        Two escapes are refused: a traversal in the id itself, and a
        ``<id>.json`` that is a symlink pointing outside the store. The second
        is only visible after resolution -- the link's own name is innocent --
        so the resolved path must equal the realpath of the ``sessions/``
        directory joined with the expected file name.
        """
        if not self._is_single_file_name(native_id):
            return None
        expected_name = native_id + ".json"
        try:
            sessions_root = Path(os.path.realpath(self.sessions_dir))
            resolved = Path(os.path.realpath(self.sessions_dir / expected_name))
        except (OSError, ValueError):
            return None
        if resolved != sessions_root / expected_name:
            return None
        if not resolved.is_file():
            return None
        return resolved

    def history(self, native_id: str, limit: int = 20) -> Dict[str, Any]:
        """A bounded, redacted slice of one session's transcript.

        The transcript lives in the session record's ``messages`` array. Only
        the last ``limit`` messages are returned, each truncated, and every
        string passes through the redaction helper so a credential that
        appeared in a prompt does not escape into a tool result.

        ``limit`` is clamped to ``[0, MAX_HISTORY_LIMIT]``: a caller-selected
        window is a bound, and an unbounded window is not a bound.
        """
        from ..redact import redact_obj
        from .model import sanitize_user_text

        native_id = sanitize_native_id(native_id)
        limit = clamp_history_limit(limit)

        path = self._session_record_path(native_id)
        if path is None:
            # Refused before any read: the id is a path, or the record is a
            # symlink leaving the store. Neither is a session identity.
            return {
                "ok": False,
                "native_session_id": native_id,
                "detail": "native session id is not a session record inside sessions/",
                "messages": [],
            }
        record = self._read_record(path)
        if record is None:
            return {
                "ok": False,
                "native_session_id": native_id,
                "detail": "no readable session record at {}".format(
                    sanitize_user_text(str(path), MAX_WORKSPACE_CHARS)
                ),
                "messages": [],
            }
        messages = record.get("messages")
        if not isinstance(messages, list):
            return {
                "ok": False,
                "native_session_id": native_id,
                "detail": "session record has no messages array",
                "messages": [],
            }
        window = messages[-limit:] if limit > 0 else []
        projected = []
        for message in window:
            if not isinstance(message, dict):
                continue
            projected.append(
                {
                    "id": message.get("id"),
                    "role": message.get("role"),
                    "display_role": message.get("display_role"),
                    "timestamp": message.get("timestamp"),
                    "content": redact_obj(message.get("content")),
                }
            )
        return {
            "ok": True,
            "native_session_id": native_id,
            "source": self.source,
            "total_messages": len(messages),
            "returned": len(projected),
            "messages": projected,
        }


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
