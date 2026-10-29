"""Kiro Crew's own session ledger.

Kiro Crew keeps two cooperating artifacts under ``~/.kiro``:

``session-index/<hash>.jsonl``
    An explicit add/remove ledger. Every line is
    ``{"op": "add"|"remove", "sessionPath": "<path>", "at": <epoch_ms>}``.
    The live set is the set of paths whose *last* op is ``add``; a later
    ``remove`` for the same path retires it. This is the one place where
    Crew states, in its own words, which sessions exist.

``sessions/<hash>/sess_<uuid>/``
    The session itself. The first line of its jsonl is a JSON object with
    ``"_type": "metadata"`` carrying ``execution_context`` (with ``store``,
    ``member_id``, ``selection_kind``, ``template_id``, ``selection_name``,
    ``selection_revision``), ``memory_mode``, ``model``, ``queued_prompts``,
    ``turn_timeout_secs``, ``created_at`` and ``last_consolidated``.

The ledger's ``sessionPath`` is **relative**, not absolute: it reads
``<hash>/sess_<uuid>`` and resolves under ``~/.kiro/sessions``. The same
directory also holds plain session files such as
``~/.kiro/crew/sessions/dashboard_chat-1-<epoch>.jsonl``, which use the
same metadata line shape. Both layouts are resolved here, because the ledger
only ever names a relative path.

This adapter is strictly read-only. It never writes to the ledger, never
touches a session file except to read it, never starts the crew gateway and
never calls a model. The crew's ledger names sessions but records no pid, so
no envelope from this source claims liveness: the state stays ``unknown``
and ``process_instance`` stays ``None``.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    MAX_HISTORY_LIMIT,
    MAX_WORKSPACE_CHARS,
    SOURCE_KIROCREW_LEDGER,
    STATE_UNKNOWN,
    Capability,
    SessionEnvelope,
    clamp_history_limit,
    make_session_uid,
    now_iso,
    sanitize_native_id,
    sanitize_user_text,
)

# Top-level metadata keys that would carry the session's own id. The crew
# metadata line does not always have one; when it does, it beats the file
# name because it is the id the session itself uses.
_ID_KEYS = ("id", "session_id", "sessionId")

# The metadata line is a header, not a turn. History skips it.
_METADATA_TYPE = "metadata"

# A history read never materialises a whole transcript. Only the tail of the
# file is read, and only a bounded number of lines are scanned from it: a
# multi-gigabyte session must not become a multi-gigabyte tool result or a
# multi-gigabyte allocation.
DEFAULT_MAX_HISTORY_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_SCAN_LINES = 20000


def _epoch_ms_to_iso(value: Any) -> Optional[str]:
    """An epoch-millisecond timestamp as RFC3339 UTC, or None."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(millis / 1000.0))


class KiroCrewStore:
    """Read-only view of the Kiro Crew session ledger."""

    def __init__(
        self,
        kiro_dir: os.PathLike,
        host_id: str = "unknown",
        namespace_id: str = "unknown",
        harness_version: Optional[str] = None,
        harness_lineage: Optional[str] = None,
    ) -> None:
        self.kiro_dir = Path(kiro_dir)
        self.host_id = host_id
        self.namespace_id = namespace_id
        self.harness_version = harness_version
        self.harness_lineage = harness_lineage
        # native id -> session file. Filled by enumerate() so history() can
        # answer without re-reading the whole ledger.
        self._session_files: Dict[str, Path] = {}

    # -- layout ------------------------------------------------------------
    @property
    def index_dir(self) -> Path:
        return self.kiro_dir / "session-index"

    @property
    def sessions_dir(self) -> Path:
        return self.kiro_dir / "sessions"

    @property
    def crew_sessions_dir(self) -> Path:
        return self.kiro_dir / "crew" / "sessions"

    @property
    def agents_dir(self) -> Path:
        return self.kiro_dir / "agents"

    def exists(self) -> bool:
        return self.index_dir.is_dir()

    # -- ledger ------------------------------------------------------------
    def _replay_ledger(self) -> Dict[str, Tuple[bool, int]]:
        """Replay the ledger once, into ``path -> (live, at_ms)``.

        Ops are replayed in file order and then line order. Each index file
        is keyed by its own workspace hash, so paths do not normally collide
        across files; when they do, the later op wins, which is the ledger's
        own semantics. A path that was added and later removed is retired,
        even though its add op is still in the file.
        """
        state: Dict[str, Tuple[bool, int]] = {}
        if not self.index_dir.is_dir():
            return state
        try:
            files = sorted(self.index_dir.glob("*.jsonl"))
        except OSError:
            return state
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    # A half-written line must not take the ledger down.
                    continue
                if not isinstance(record, dict):
                    continue
                session_path = record.get("sessionPath")
                if not isinstance(session_path, str) or not session_path:
                    continue
                try:
                    at = int(record.get("at", 0))
                except (TypeError, ValueError):
                    at = 0
                state[session_path] = (record.get("op") == "add", at)
        return state

    def _live_sessions(self) -> Dict[str, int]:
        """The live set: paths whose last op was an ``add``."""
        return {
            path: at
            for path, (live, at) in self._replay_ledger().items()
            if live
        }

    # -- session files -----------------------------------------------------
    def _contained(self, root: Path, candidate: Path) -> Optional[Path]:
        """Resolve ``candidate`` and keep it only if it stays under ``root``.

        The same rule the jcode store applies to a session record: compare
        realpaths, so a ``..`` component and a symlink that leaves the store
        are both refused, and only after resolution, because a link's own
        name is innocent. ``root`` itself is allowed.
        """
        try:
            root_real = Path(os.path.realpath(root))
            resolved = Path(os.path.realpath(candidate))
        except (OSError, ValueError, RuntimeError):
            # A null byte, an absurdly long path, or a link loop.
            return None
        if resolved == root_real or root_real in resolved.parents:
            return resolved
        return None

    def _resolve_session_file(self, session_path: str) -> Optional[Path]:
        """Resolve a ledger-relative ``sessionPath`` to a readable file.

        The ledger's paths are relative to ``~/.kiro/sessions``. Two other
        roots are tried because the crew also writes plain session files
        under ``~/.kiro/crew/sessions`` with the same metadata shape.

        The ledger only ever names a relative path, so an absolute path is
        refused rather than honoured: it is not a crew session path, and
        honouring it would let the ledger name ``/etc/passwd``. Every
        candidate, including a file inside a session directory, must resolve
        inside the store.
        """
        try:
            candidate = Path(session_path)
        except (TypeError, ValueError):
            return None
        if not session_path or candidate.is_absolute():
            return None
        if candidate.anchor:
            # A drive-relative or rooted path that is not is_absolute() on
            # this platform still is not a path relative to the store.
            return None
        roots: List[Path] = [
            self.sessions_dir,
            self.crew_sessions_dir,
            self.kiro_dir,
        ]
        for root in roots:
            resolved = self._contained(root, root / candidate)
            if resolved is None:
                continue
            if resolved.is_file():
                return resolved
            if resolved.is_dir():
                # A session directory holds exactly one session jsonl. Take
                # the newest one when there is more than one, but only if it
                # is itself still inside the store.
                try:
                    entries = sorted(resolved.glob("*.jsonl"))
                except OSError:
                    continue
                for entry in reversed(entries):
                    contained = self._contained(self.kiro_dir, entry)
                    if contained is not None and contained.is_file():
                        return contained
        return None

    def _read_metadata(self, path: Path) -> Optional[Dict[str, Any]]:
        """The metadata line of a session file, or None.

        Only the first line is read. It is the header; the rest of the file
        is the conversation and belongs to an explicit history request.
        """
        try:
            with path.open("r", encoding="utf-8") as fh:
                first = fh.readline()
        except (OSError, UnicodeDecodeError):
            return None
        if not first.strip():
            return None
        try:
            record = json.loads(first)
        except (ValueError, TypeError):
            return None
        if not isinstance(record, dict):
            return None
        return record

    def _native_id(self, metadata: Dict[str, Any], session_path: str) -> Tuple[str, bool]:
        """The session's own id, or the path basename when it has none.

        The second element says which one it was, so the envelope can say
        so in its evidence.
        """
        for key in _ID_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value, True
        return Path(session_path).name, False

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

    # -- history -----------------------------------------------------------
    def _tail_lines(
        self, path: Path, max_bytes: int, max_scan_lines: int
    ) -> List[str]:
        """The last ``max_scan_lines`` complete lines of a bounded tail.

        The file is never materialised: the read starts at ``size -
        max_bytes`` and reads at most ``max_bytes``. When the window does not
        begin at byte zero its first line is a fragment of a record that
        began before the window, so it is dropped rather than parsed into a
        half-record.
        """
        byte_budget = max(1, int(max_bytes))
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - byte_budget)
            fh.seek(start)
            data = fh.read(byte_budget)
        # A tail window can split a multibyte character; replacement keeps
        # the decode total instead of raising on one broken boundary byte.
        lines = data.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        if max_scan_lines > 0 and len(lines) > max_scan_lines:
            lines = lines[-max_scan_lines:]
        return lines

    def history(
        self,
        session_id: str,
        limit: int = 20,
        max_bytes: int = DEFAULT_MAX_HISTORY_BYTES,
        max_scan_lines: int = DEFAULT_MAX_SCAN_LINES,
    ) -> Dict[str, Any]:
        """A bounded, redacted slice of one crew session.

        The metadata line is skipped: it is a header, not a turn. At most
        ``max_bytes`` of the tail is read and at most ``max_scan_lines``
        lines are scanned, so a huge transcript costs a fixed amount of work.
        Only the last ``limit`` conversation lines are returned, and every
        string in them passes through the redaction helper so a credential
        that appeared in a prompt does not escape into a tool result.
        """
        from ..redact import redact_obj

        session_id = sanitize_native_id(session_id)

        path = self._session_files.get(session_id)
        if path is None:
            path = self._find_session_file(session_id)
        if path is None:
            return {
                "ok": False,
                "native_session_id": session_id,
                "detail": "no readable crew session for id {!r}".format(session_id),
                "events": [],
            }
        try:
            lines = self._tail_lines(path, max_bytes, max_scan_lines)
        except (OSError, ValueError) as exc:
            # The path and the OS message are user- and host-derived text:
            # bound and redact them before they reach a tool result.
            return {
                "ok": False,
                "native_session_id": session_id,
                "detail": "could not read {}: {}".format(
                    sanitize_user_text(str(path), MAX_WORKSPACE_CHARS),
                    sanitize_user_text(str(exc), MAX_WORKSPACE_CHARS),
                ),
                "events": [],
            }
        keep = clamp_history_limit(limit)
        events: "deque[Dict[str, Any]]" = deque(maxlen=keep if keep else 1)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("_type") == _METADATA_TYPE:
                continue
            events.append(
                {
                    "type": record.get("type") or record.get("_type"),
                    "timestamp": record.get("timestamp")
                    or record.get("created_at"),
                    "payload": redact_obj(record),
                }
            )
        window = list(events) if keep else []
        return {
            "ok": True,
            "native_session_id": session_id,
            "source": SOURCE_KIROCREW_LEDGER,
            "path": sanitize_user_text(str(path), MAX_WORKSPACE_CHARS),
            "returned": len(window),
            "events": window,
        }

    def _find_session_file(self, session_id: str) -> Optional[Path]:
        """Find a session file by id, without an enumerate() first."""
        live = self._live_sessions()
        for session_path in live:
            file_path = self._resolve_session_file(session_path)
            if file_path is None:
                continue
            metadata = self._read_metadata(file_path)
            if metadata is None:
                continue
            native_id, _own = self._native_id(metadata, session_path)
            if native_id == session_id:
                self._session_files[session_id] = file_path
                return file_path
        return None

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


def default_store() -> KiroCrewStore:
    """The Kiro Crew store for the current user, if one exists."""
    return KiroCrewStore(Path.home() / ".kiro")
