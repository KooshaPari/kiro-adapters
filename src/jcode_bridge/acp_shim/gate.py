"""Daemon-precondition gate for adopting a pre-existing jcode/KCode session.

Two fork facts drive this module:

* ``session/new`` and ``session/load`` both call ``connect_daemon``, which calls
  ``ensure_daemon``, which SPAWNS a daemon when none is running
  (src/cli/acp.rs:851-867).
* ``session/list`` is a directory read and never touches the daemon
  (src/cli/acp/discovery.rs:5-60).

So adoption is gated twice and both checks are read-only: the session id must
resolve to an existing ``<id>.json`` under a known session directory, and a
daemon socket must already accept a connect. A connect probe sends no protocol
bytes. If either check fails the shim fails closed; it never starts a daemon.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .protocol import SHIM_PRECONDITION_FAILED, RpcError

SESSION_ID_MAX = 200
_UNSAFE_FRAGMENTS = ("/", "\\", "\x00")


@dataclass(frozen=True)
class DaemonState:
    """Outcome of a read-only liveness probe of the daemon sockets."""

    running: bool
    socket: Optional[str] = None
    checked: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class AdoptionPlan:
    """Evidence that adoption can proceed without starting a daemon."""

    session_id: str
    session_file: str
    cwd: Optional[str]
    daemon: DaemonState


def default_session_dirs() -> Tuple[str, ...]:
    """Session stores to search, most-specific override first."""
    override = os.environ.get("JCODE_ACP_SESSION_DIR", "")
    explicit = [item for item in override.split(os.pathsep) if item]
    home = os.path.expanduser("~")
    return tuple(explicit) + (
        os.path.join(home, ".jcode", "sessions"),
        os.path.join(home, ".kcode", "sessions"),
    )


def default_socket_paths() -> Tuple[str, ...]:
    """Candidate daemon sockets. Existence is required, creation is not."""
    override = os.environ.get("JCODE_ACP_SOCKET", "")
    explicit = [item for item in override.split(os.pathsep) if item]
    home = os.path.expanduser("~")
    return tuple(explicit) + (
        os.path.join(home, ".jcode", "scratch", "jcode.sock"),
        os.path.join(home, ".jcode", "scratch", "kcode.sock"),
        os.path.join(home, ".kcode", "scratch", "kcode.sock"),
    )


def is_safe_session_id(session_id: str) -> bool:
    """Reject ids that could escape the session directory."""
    if not session_id or len(session_id) > SESSION_ID_MAX:
        return False
    if session_id.startswith("."):
        return False
    return not any(fragment in session_id for fragment in _UNSAFE_FRAGMENTS)


def find_session_file(
    session_id: str,
    session_dirs: Optional[Iterable[str]] = None,
) -> Optional[Path]:
    """Return the ``<id>.json`` path for ``session_id`` if it exists."""
    for directory in session_dirs or default_session_dirs():
        candidate = Path(str(directory)) / (session_id + ".json")
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def read_session_summary(path: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """Read only the metadata keys the shim needs. Never the transcript."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cwd = data.get("working_dir") or data.get("workingDir") or data.get("cwd")
    title = (
        data.get("display_title")
        or data.get("displayTitle")
        or data.get("title")
        or data.get("name")
        or session_id
    )
    updated = data.get("updated_at") or data.get("updatedAt") or ""
    return {
        "sessionId": str(data.get("id") or data.get("session_id") or session_id),
        "cwd": cwd if isinstance(cwd, str) else None,
        "title": str(title)[:200],
        "updatedAt": str(updated),
    }


def list_sessions(
    session_dirs: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Enumerate adoptable sessions by reading files. No daemon contact."""
    summaries: List[Dict[str, Any]] = []
    for directory in session_dirs or default_session_dirs():
        try:
            names = sorted(os.listdir(str(directory)))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            session_id = name[:-5]
            if not is_safe_session_id(session_id):
                continue
            summary = read_session_summary(Path(str(directory)) / name, session_id)
            if summary is not None:
                summaries.append(summary)
    summaries.sort(key=lambda item: str(item.get("sessionId", "")))
    return summaries


def probe_daemon(
    socket_paths: Optional[Iterable[str]] = None,
    timeout: float = 0.25,
) -> DaemonState:
    """Report whether a daemon socket is already live.

    Only a connect and an immediate close. No bytes are written, so no
    handshake runs and no daemon is started.
    """
    checked = tuple(str(path) for path in (socket_paths or default_socket_paths()))
    for path in checked:
        try:
            if not os.path.exists(path):
                continue
        except OSError:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(timeout)
        try:
            probe.connect(path)
        except OSError:
            continue
        finally:
            probe.close()
        return DaemonState(
            running=True,
            socket=path,
            checked=checked,
            detail="connect probe succeeded",
        )
    return DaemonState(
        running=False,
        socket=None,
        checked=checked,
        detail="no live daemon socket",
    )


def _fail(precondition: str, message: str, session_id: str, **extra: Any) -> RpcError:
    data: Dict[str, Any] = {
        "precondition": precondition,
        "sessionId": session_id,
        "willSpawnDaemon": False,
    }
    data.update(extra)
    return RpcError(SHIM_PRECONDITION_FAILED, message, data)


def assess(
    session_id: str,
    session_dirs: Optional[Iterable[str]] = None,
    socket_paths: Optional[Iterable[str]] = None,
    probe: Optional[Callable[[Iterable[str]], DaemonState]] = None,
) -> AdoptionPlan:
    """Decide whether ``session_id`` is adoptable. Raises :class:`RpcError`.

    The error is fail-closed: it states the precondition and states that the
    shim will not start a daemon.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not is_safe_session_id(sid):
        raise _fail(
            "invalid_session_id",
            "session id is not a plain session filename stem",
            sid,
        )

    path = find_session_file(sid, session_dirs)
    if path is None:
        raise _fail(
            "unknown_session",
            "no session file for this id; the shim only adopts sessions that "
            "already exist on disk and never creates one",
            sid,
            searched=tuple(str(item) for item in (session_dirs or default_session_dirs())),
        )

    summary = read_session_summary(path, sid) or {}
    prober = probe or probe_daemon
    state = prober(tuple(str(item) for item in (socket_paths or default_socket_paths())))
    if not state.running:
        raise _fail(
            "daemon_not_running",
            "adopting this session requires an already-running jcode daemon; "
            "the shim will not start one. Start the session normally first.",
            sid,
            sessionFile=str(path),
            checkedSockets=state.checked,
        )

    return AdoptionPlan(
        session_id=sid,
        session_file=str(path),
        cwd=summary.get("cwd"),
        daemon=state,
    )
