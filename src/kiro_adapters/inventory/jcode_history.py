"""Bounded, redacted transcript reads for one jcode-family session store."""

from __future__ import annotations

from typing import Any, Dict

from .model import (
    MAX_WORKSPACE_CHARS,
    clamp_history_limit,
    sanitize_native_id,
    sanitize_user_text,
)


class JcodeStoreHistoryMixin:
    """The ``history`` surface: a bounded, redacted transcript slice."""

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
