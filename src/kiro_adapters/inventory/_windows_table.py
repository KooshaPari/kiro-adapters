"""The Windows half of the process table: ``tasklist``, and its liveness probe.

Windows cannot use ``kill -0``: CPython accepts only ``CTRL_C_EVENT``,
``CTRL_BREAK_EVENT`` and ``SIGTERM`` there, so signal 0 raises ``OSError``, and
``os.kill(pid, 0)`` is not a probe at all. This module therefore owns its own
liveness answer, and it is deliberately conservative: anything other than a
definitive "no tasks match" or a definitive pid match is ``UNKNOWN``, never
``GONE``.

It also owns the Windows snapshot, which cannot bind a pid to a process because
``tasklist`` reports no start time.

Every child process here is read through :func:`_run_capture`, which caps both
streams while they are being read. ``subprocess.run(capture_output=True)``
would buffer an unbounded amount of output before any size check could run, so
a wedged ``tasklist`` or ``powershell`` could exhaust memory. The bounded
reader kills the child as soon as either stream passes its cap, and every
caller fails closed on a truncated, timed-out, or failed read.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, NamedTuple, Optional, Sequence

from ._process_verdicts import ALIVE, GONE, UNKNOWN
from .model import ProcessInstance

_MAX_OUTPUT_BYTES = 1_000_000
_MAX_STDERR_BYTES = 64_000

# One pipe read. Small enough that a cap is reached promptly, large enough that
# a normal ``tasklist`` read costs a handful of syscalls.
_READ_CHUNK = 65_536
# How often the main thread re-checks the overflow flag and the deadline.
_POLL_SECONDS = 0.05
# How long to wait for the reader threads after the child has been killed.
_JOIN_SECONDS = 5.0

_WINDOWS_TABLE: Optional[Dict[int, str]] = None
_WINDOWS_TABLE_AT = 0.0
_WINDOWS_TABLE_TTL = 2.0

# Windows PowerShell 5.1 first (present on every supported Windows), then the
# PowerShell 7 executable. Both accept the same read-only CIM script below.
_POWERSHELL_CANDIDATES = ("powershell.exe", "powershell", "pwsh")


class CaptureResult(NamedTuple):
    """Outcome of one bounded child-process read.

    ``returncode`` is ``None`` when the child could not be started at all.
    ``truncated`` means a stream passed its cap and the child was killed;
    ``timed_out`` means the deadline expired. A truncated or timed-out read is
    never partial evidence: callers must fail closed.
    """

    returncode: Optional[int]
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.truncated
            and not self.timed_out
        )


def _drain_capped(stream, cap: int):
    """Read ``stream`` up to ``cap`` bytes without ever buffering past it.

    Returns ``(data, overflow)``. Once the cap is crossed the reader stops and
    reports overflow; the caller kills the child, so the pipe cannot stay full
    for long. Memory is bounded by ``cap`` plus one chunk.
    """
    parts = []
    total = 0
    overflow = False
    try:
        while True:
            block = stream.read(_READ_CHUNK)
            if not block:
                break
            room = cap - total
            if room > 0:
                take = block[:room]
                parts.append(take)
                total += len(take)
            if len(block) > max(room, 0):
                overflow = True
                break
    except (OSError, ValueError):
        # The main thread can close the pipe while we are blocked on it after a
        # kill. That is not a read of the child's output.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    return b"".join(parts), overflow


def _terminate(proc: "subprocess.Popen") -> None:
    """Best-effort kill of ``proc`` and, on POSIX, of its process group."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, 9)
            return
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass


def _run_capture(
    argv: Sequence[str],
    *,
    timeout: float,
    max_stdout_bytes: int = _MAX_OUTPUT_BYTES,
    max_stderr_bytes: int = _MAX_STDERR_BYTES,
) -> CaptureResult:
    """Run ``argv`` with bounded stdout/stderr capture and a hard deadline.

    Both pipes are drained concurrently by reader threads, so a child that
    writes a lot to one stream cannot deadlock the other. Either stream
    crossing its cap kills the child and sets ``truncated``. The deadline
    kills the child and sets ``timed_out``. Bytes are decoded as UTF-8 with
    replacement, matching the explicit UTF-8 encoding the PowerShell probe
    requests.
    """
    # ``start_new_session`` is POSIX-only; on Windows a new session would only
    # add console noise, so suppress the window instead.
    spawn_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        spawn_kwargs["start_new_session"] = True
    else:  # pragma: no cover - exercised only on Windows
        spawn_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    try:
        proc = subprocess.Popen(list(argv), **spawn_kwargs)
    except (OSError, ValueError):
        return CaptureResult(None, "", "", False, False)

    collected: Dict[str, bytes] = {}
    overflow = threading.Event()

    def reader(name: str, stream, cap: int) -> None:
        data, over = _drain_capped(stream, cap)
        collected[name] = data
        if over:
            overflow.set()

    threads = [
        threading.Thread(
            target=reader,
            args=("stdout", proc.stdout, max_stdout_bytes),
            name="kiro-windows-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=reader,
            args=("stderr", proc.stderr, max_stderr_bytes),
            name="kiro-windows-stderr",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if overflow.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            proc.wait(timeout=min(_POLL_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    if proc.poll() is None:
        _terminate(proc)
        # Unblock readers still parked on a pipe held open by a grandchild.
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    try:
        proc.wait(timeout=_JOIN_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass
    for thread in threads:
        thread.join(_JOIN_SECONDS)

    stdout_bytes = collected.get("stdout", b"")
    stderr_bytes = collected.get("stderr", b"")
    return CaptureResult(
        proc.returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        truncated=overflow.is_set(),
        timed_out=timed_out,
    )


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
    out = _run_capture(["tasklist", "/FO", "CSV", "/NH"], timeout=20)
    if not out.ok:
        return None
    if out.stderr.strip():
        return None
    table: Dict[int, str] = {}
    for line in out.stdout.splitlines():
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
    missing ``tasklist``, a non-zero exit, an unparsable line, excessive
    output, a timeout - is an unknown answer, not a dead process.
    """
    out = _run_capture(
        ["tasklist", "/FI", "PID eq {}".format(pid), "/NH"], timeout=20
    )
    if not out.ok:
        return UNKNOWN
    if out.stderr.strip():
        return UNKNOWN
    text = out.stdout
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


@functools.lru_cache(maxsize=1)
def _powershell_path() -> Optional[str]:
    """First available PowerShell executable, or ``None``.

    Windows PowerShell 5.1 (``powershell.exe``) exists on every supported
    Windows; ``pwsh`` is used when 5.1 is absent. ``None`` means no shell is
    available and the probe must fail closed rather than guess.
    """
    for name in _POWERSHELL_CANDIDATES:
        try:
            found = shutil.which(name)
        except (OSError, ValueError):
            found = None
        if found:
            return found
    return None


# Make the CIM output UTF-8 regardless of the host's console code page, so the
# JSON does not arrive mojibake or fail to decode. ``$OutputEncoding`` covers
# native-pipeline consumers, ``[Console]::OutputEncoding`` covers redirected
# stdout. The UTF8Encoding instance has no BOM, and the parser strips one
# defensively anyway.
_POWERSHELL_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'; "
    "try {{ [Console]::OutputEncoding = "
    "New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false }} catch {{ }}; "
    "try {{ $OutputEncoding = [Console]::OutputEncoding }} catch {{ }}; "
)


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def read_windows_process_identities(pids) -> Optional[Dict[int, tuple]]:
    """Read PID + creation time + executable path for the given pids.

    Uses the Windows CIM (``Win32_Process``) read-only. Only the requested
    pids are queried: no full process enumeration, no command lines, no user
    secrets. Returns ``None`` when the read could not be performed at all
    (denied, missing tool, malformed output, over-size output, timeout). An
    empty dict means the read succeeded but bound no pid. Per-pid
    partial/ambiguous rows are dropped rather than trusted.
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
    if not all(isinstance(pid, int) and pid > 0 for pid in pids):
        return None
    shell = _powershell_path()
    if shell is None:
        return None
    filter_expr = " OR ".join("ProcessId={}".format(pid) for pid in pids)
    script = (
        _POWERSHELL_PREAMBLE
        + "Get-CimInstance Win32_Process -Filter \"{filter}\" -ErrorAction Stop | "
        "ForEach-Object {{ "
        "$creation = $null; "
        "if ($_.CreationDate) {{ $creation = $_.CreationDate.ToUniversalTime().ToString('o') }}; "
        "[PSCustomObject]@{{ pid = $_.ProcessId; creation = $creation; executable = $_.ExecutablePath }} "
        "}} | ConvertTo-Json -Compress"
    ).format(filter=filter_expr)
    out = _run_capture(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=15,
    )
    if not out.ok:
        return None
    # PowerShell can report a non-terminating error on stderr while still
    # exiting 0. Treat any stderr output as a failed read.
    if out.stderr.strip():
        return None
    text = out.stdout.strip().lstrip("\ufeff").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
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
