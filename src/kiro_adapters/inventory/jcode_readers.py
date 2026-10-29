"""Bounded readers over one jcode-family session store.

Everything here is a read: the record files in ``sessions/``, the pid
pointers in ``active_pids/``, and the two hint artifact families. Each read
is capped, and none of them writes, spawns or resumes anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._path_safety import read_text_capped

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


class JcodeStoreReadMixin:
    """Capped, fail-closed readers for the jcode file store."""

    def _session_files(self) -> List[Path]:
        # Imported here, not at module scope, so a caller that lowers the
        # ceiling on ``jcode_fs.MAX_SESSION_FILES`` is honoured.
        from .jcode_fs import MAX_SESSION_FILES

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
        text = read_text_capped(path, self.max_record_bytes)
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
        # Imported here so a caller that lowers the ceiling on
        # ``jcode_fs.MAX_INPUT_SESSION_HINTS`` is honoured.
        from .jcode_fs import MAX_INPUT_SESSION_HINTS

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
            body = read_text_capped(entry, self.max_record_bytes)
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
