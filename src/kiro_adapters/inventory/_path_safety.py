"""Path confinement and bounded-read helpers shared by the file adapters.

Every artifact these adapters read is attacker-writable. A session id can
carry a traversal or a NUL, a record name can be a symlink that resolves out
of the store, and a record body can be arbitrarily large. Those three
refusals are implemented once, here, so the jcode and Kiro Crew stores cannot
drift apart on a security rule.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional


def read_text_capped(path: Path, max_bytes: int) -> Optional[str]:
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


def is_single_file_name(native_id: Any) -> bool:
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


def exact_child(root: Path, name: str) -> Optional[Path]:
    """``root / name`` resolved, but only when it is exactly that child.

    An innocent-looking name can be a symlink that resolves outside ``root``;
    that is only visible after resolution, so the resolved path must equal
    the realpath of ``root`` joined with ``name``.
    """
    try:
        root_real = Path(os.path.realpath(root))
        resolved = Path(os.path.realpath(root / name))
    except (OSError, ValueError):
        return None
    if resolved != root_real / name:
        return None
    return resolved


def contained_path(root: Path, candidate: Path) -> Optional[Path]:
    """Resolve ``candidate`` and keep it only if it stays under ``root``.

    Realpaths are compared, so a ``..`` component and a symlink that leaves
    the store are both refused, and only after resolution, because a link's
    own name is innocent. ``root`` itself is allowed.
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
