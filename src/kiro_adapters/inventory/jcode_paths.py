"""Store layout and path confinement for one jcode-family session store.

The layout half of :class:`~kiro_adapters.inventory.jcode_fs.JcodeStore`:
where each artifact family lives, and how a caller-supplied native id is
confined to a single file inside the directory that owns it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ._path_safety import exact_child, is_single_file_name


class JcodeStoreLayoutMixin:
    """The store's directories, and id -> path resolution inside them."""

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

    @staticmethod
    def _is_single_file_name(native_id: Any) -> bool:
        """True when the id is one path component and nothing else."""
        return is_single_file_name(native_id)

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
        resolved = exact_child(self.active_pids_dir, native_id)
        if resolved is None:
            return None
        if not resolved.is_file():
            return None
        return resolved

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
        resolved = exact_child(self.sessions_dir, native_id + ".json")
        if resolved is None:
            return None
        if not resolved.is_file():
            return None
        return resolved
