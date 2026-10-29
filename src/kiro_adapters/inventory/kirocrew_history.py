"""Bounded, redacted transcript reads for the Kiro Crew session inventory.

A history read never materialises a whole transcript: only the tail of the
file is read, and only a bounded number of lines are scanned from it.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import (
    MAX_WORKSPACE_CHARS,
    SOURCE_KIROCREW_LEDGER,
    clamp_history_limit,
    sanitize_native_id,
    sanitize_user_text,
)

# The metadata line is a header, not a turn. History skips it.
_METADATA_TYPE = "metadata"

# A history read never materialises a whole transcript. Only the tail of the
# file is read, and only a bounded number of lines are scanned from it: a
# multi-gigabyte session must not become a multi-gigabyte tool result or a
# multi-gigabyte allocation.
DEFAULT_MAX_HISTORY_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_SCAN_LINES = 20000


class KiroCrewStoreHistoryMixin:
    """The ``history`` surface: a bounded, redacted crew-session slice."""

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
