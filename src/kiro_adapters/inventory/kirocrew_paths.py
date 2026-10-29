"""Store layout and session-file confinement for the Kiro Crew inventory.

A ledger ``sessionPath`` is relative and attacker-writable, so every candidate
is resolved and kept only when it stays inside the store. A session directory
holds exactly one session jsonl; when it holds more, the newest contained one
wins.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._path_safety import contained_path

# Top-level metadata keys that would carry the session's own id. The crew
# metadata line does not always have one; when it does, it beats the file
# name because it is the id the session itself uses.
_ID_KEYS = ("id", "session_id", "sessionId")


class KiroCrewStoreLayoutMixin:
    """The crew store's directories, and ledger path -> file resolution."""

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

    # -- session files -----------------------------------------------------
    def _contained(self, root: Path, candidate: Path) -> Optional[Path]:
        """Resolve ``candidate`` and keep it only if it stays under ``root``.

        The same rule the jcode store applies to a session record: compare
        realpaths, so a ``..`` component and a symlink that leaves the store
        are both refused, and only after resolution, because a link's own
        name is innocent. ``root`` itself is allowed.
        """
        return contained_path(root, candidate)

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
