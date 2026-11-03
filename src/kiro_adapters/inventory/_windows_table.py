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

import json
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


# -- read-only Windows process identity via CIM ----------------------------


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def read_windows_process_identities(pids) -> Optional[Dict[int, tuple]]:
    """Read PID + creation time + executable path for the given pids.

    Uses the Windows CIM (``Win32_Process``) read-only. Only the requested
    pids are queried: no full process enumeration, no command lines, no user
    secrets. Returns ``None`` when the read could not be performed at all
    (denied, missing tool, malformed output). An empty dict means the read
    succeeded but bound no pid. Per-pid partial/ambiguous rows are dropped
    rather than trusted.
    """
    wanted = []
    for raw in pids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            wanted.append(pid)
    if not wanted:
        return {}
    wanted_set = set(wanted)
    result: Dict[int, tuple] = {}
    for chunk in _chunked(wanted, 64):
        chunk_result = _read_windows_process_identity_chunk(chunk, wanted_set)
        if chunk_result is None:
            # A failed read is not evidence. Fail closed for the whole request
            # rather than silently dropping the unread pids.
            return None
        result.update(chunk_result)
    return result


def _read_windows_process_identity_chunk(pids, wanted_set):
    """One bounded CIM read for a small set of pids."""
    if not pids:
        return {}
    filter_expr = " OR ".join("ProcessId={}".format(pid) for pid in pids)
    script = (
        "Get-CimInstance Win32_Process -Filter \"{filter}\" | "
        "ForEach-Object {{ "
        "$creation = $null; "
        "if ($_.CreationDate) {{ $creation = $_.CreationDate.ToUniversalTime().ToString('o') }}; "
        "[PSCustomObject]@{{ pid = $_.ProcessId; creation = $creation; executable = $_.ExecutablePath }} "
        "}} | ConvertTo-Json -Compress"
    ).format(filter=filter_expr)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    chunk_result: Dict[int, tuple] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid not in wanted_set:
            # Unexpected/ambiguous row: do not trust it.
            continue
        creation = item.get("creation")
        executable = item.get("executable")
        if not isinstance(creation, str) or not creation.strip():
            continue
        if not isinstance(executable, str) or not executable.strip():
            continue
        chunk_result[pid] = (creation.strip(), executable.strip())
    return chunk_result
