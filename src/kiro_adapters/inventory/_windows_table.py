"""The Windows half of the process table: ``tasklist``, and its liveness probe.

Windows cannot use ``kill -0``: CPython accepts only ``CTRL_C_EVENT``,
``CTRL_BREAK_EVENT`` and ``SIGTERM`` there, so signal 0 raises ``OSError``, and
``os.kill(pid, 0)`` is not a probe at all. This module therefore owns its own
liveness answer, and it is deliberately conservative: anything other than a
definitive "no tasks match" or a definitive pid match is ``UNKNOWN``, never
``GONE``.

It also owns the Windows snapshot, which cannot bind a pid to a process because
``tasklist`` reports no start time.
"""

from __future__ import annotations

import subprocess
import time
from typing import Dict, Optional

from ._process_verdicts import ALIVE, GONE, UNKNOWN
from .model import ProcessInstance

_WINDOWS_TABLE: Optional[Dict[int, str]] = None
_WINDOWS_TABLE_AT = 0.0
_WINDOWS_TABLE_TTL = 2.0


def scan_windows() -> Dict[int, ProcessInstance]:
    """Windows process snapshot via ``tasklist``.

    Windows has no ``ps``; ``tasklist`` gives the image name but no start
    time, so there is no start identity to record. The field says
    ``unknown`` rather than carrying a collection marker that would look
    like an identity and change on every read. A Windows handle cannot be
    re-verified against a recycled pid without the Win32 ``CreateTime``,
    which this module does not reach for.

    A snapshot entry with no start identity cannot be bound to a process,
    and a pid alone is not an identity. Rather than emit
    ``start_identity="unknown"`` handles that look like identities, this
    returns an empty snapshot: the platform can enumerate pids (see
    :func:`windows_liveness`), it just cannot bind them.
    """
    return {}


def windows_table(cached: bool = True) -> Optional[Dict[int, str]]:
    """The Windows process table as pid -> image name.

    ``None`` means the table could not be read at all, which is different
    from a table that was read and did not contain a given pid.
    """
    global _WINDOWS_TABLE, _WINDOWS_TABLE_AT
    now = time.monotonic()
    if (
        cached
        and _WINDOWS_TABLE is not None
        and (now - _WINDOWS_TABLE_AT) < _WINDOWS_TABLE_TTL
    ):
        return _WINDOWS_TABLE
    table = read_windows_table()
    _WINDOWS_TABLE = table
    _WINDOWS_TABLE_AT = now
    return table


def read_windows_table() -> Optional[Dict[int, str]]:
    """One ``tasklist`` read, as pid -> image name, or None on failure."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    table: Dict[int, str] = {}
    for line in (out.stdout or "").splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        table[pid] = parts[0]
    return table


def windows_liveness(pid: int) -> str:
    """Liveness for one pid on Windows, without ``kill -0``.

    The filtered ``tasklist`` form answers about one pid: it either prints
    the matching row or says that no task matches. Anything else - a
    missing ``tasklist``, a non-zero exit, an unparsable line - is an
    unknown answer, not a dead process.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if out.returncode != 0:
        return UNKNOWN
    text = out.stdout or ""
    if "no tasks" in text.lower():
        return GONE
    for line in text.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            found = int(parts[1])
        except ValueError:
            continue
        if found == pid:
            return ALIVE
    return UNKNOWN
