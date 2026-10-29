"""Platform process-table reads and liveness probes.

This module only knows how to ask the operating system what it can see. The
liveness *policy* - what a caller may conclude from an answer - lives next
door in :mod:`.processes`; keeping the two apart is what lets a failed probe
say "unknown" instead of quietly meaning "gone".

Three facts shape the code here:

* ``kill -0`` is not a liveness test on Windows. CPython accepts only
  ``CTRL_C_EVENT``, ``CTRL_BREAK_EVENT`` and ``SIGTERM`` there, so signal 0
  raises ``OSError``. Treating that as "not alive" reports every live process
  as dead, so Windows gets its own probe.
* A plain ``ps`` lists the current terminal's processes, not the host's. On
  macOS it showed 115 of 1054 processes on the machine this was written on,
  and every one of the missing ones was a live process that the inventory
  would then have called "exited". The bulk read asks for *all* processes.
* A process table that was read is evidence of absence; a process table that
  could not be read is evidence of nothing. The two are kept apart all the
  way to the caller.

This module is deliberately dependency-free and never writes.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from typing import Dict, Iterable, Optional, Set, Tuple

from .model import ProcessInstance

# The verdict vocabulary and the Windows platform table each live in their own
# module so that there is exactly one definition of every string and one place
# that speaks ``tasklist``. They stay part of this module's public surface:
# callers and tests reach them as ``process_table.ALIVE`` and friends.
from ._process_verdicts import (  # noqa: F401  (re-exported public surface)
    ALIVE,
    GONE,
    UNKNOWN,
    UNKNOWN_START,
)
from ._windows_table import (  # noqa: F401  (re-exported public surface)
    read_windows_table,
    scan_windows,
    windows_liveness,
    windows_table,
)

# A process table read costs about a second on a busy host, and the inventory
# asks for it repeatedly while walking thousands of session records. The
# table cannot change a process's START identity, so a cached entry stays
# valid for the lifetime of the process - but a process can EXIT, so the
# cache is short-lived and every lookup re-checks liveness.
_SCAN_CACHE: Dict[int, ProcessInstance] = {}
_SCAN_CACHE_AT = 0.0
_SCAN_CACHE_TTL = 2.0


def is_windows() -> bool:
    """True on Windows, whatever the caller runs on.

    Three independent checks because this one answer decides whether
    ``os.kill`` may be used at all. On Windows, CPython's ``os.kill`` treats
    any signal other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` as
    ``TerminateProcess(handle, sig)``, so ``os.kill(pid, 0)`` does not probe
    liveness: it kills the process with exit code 0. A "is this pid alive"
    check that reaches ``os.kill`` on Windows therefore destroys the thing
    it was asked about and answers from a failed call, inverting every
    verdict. The platform test must never be skipped.
    """
    return (
        os.name == "nt"
        or sys.platform.startswith("win")
        or platform.system() == "Windows"
    )


def boot_id() -> str:
    """A value that changes on every reboot.

    A handle recorded before a reboot must not be trusted after it, and a
    recycled pid on the other side of a reboot is indistinguishable from the
    original without this.
    """
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as fh:
                return fh.read().strip()
        except OSError:
            pass
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                # "{ sec = 1792424285, usec = 622237 } Mon Oct 19 08:38:05 2026"
                return out.stdout.strip().split(",")[0].replace("{", "").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    # Fall back to the boot time of the current process, which is at least
    # stable within a boot.
    return "proc-start:{}".format(process_start(os.getpid()))


def process_start(pid: int) -> str:
    """The OS's own start time for ``pid``, as an opaque string.

    Only the start time is read. An earlier version also read the elapsed
    seconds on Linux, which made the "identity" change on every read and
    therefore never matched itself again; the start time alone is stable.
    """
    if is_windows():
        # Windows has no ``ps`` and no cheap start time.
        return UNKNOWN_START
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_START
    if out.returncode != 0:
        return UNKNOWN_START
    text = " ".join((out.stdout or "").split())
    return text or UNKNOWN_START


def executable_path(pid: int) -> Optional[str]:
    """The command path for ``pid``, or None when it cannot be read."""
    if is_windows():
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout or "").strip()
    return text or None


# -- liveness -------------------------------------------------------------


def liveness(pid: int) -> str:
    """One of :data:`ALIVE`, :data:`GONE` or :data:`UNKNOWN` for ``pid``.

    ``unknown`` means the probe could not answer. It is not a synonym for
    "gone": reporting "the process exited" from a failed probe invents a
    fact that was never observed.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return UNKNOWN
    if pid <= 0:
        return GONE
    # Before os.kill, always: on Windows signal 0 TERMINATES the process
    # rather than probing it, so reaching os.kill here would invert every
    # verdict. See :func:`is_windows`.
    if is_windows():
        return windows_liveness(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return GONE
    except PermissionError:
        # Alive, owned by another user.
        return ALIVE
    except OSError:
        # The probe failed for a reason that does not say the process is
        # gone. Say "unknown" rather than guessing.
        return UNKNOWN
    return ALIVE


def wanted_pids(pids: Iterable[int]) -> Set[int]:
    """The usable pids in ``pids``, as a set of positive integers."""
    wanted: Set[int] = set()
    for raw in pids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        wanted.add(pid)
    return wanted


def liveness_many(pids: Iterable[int]) -> Dict[int, str]:
    """The liveness verdict for many pids, from one process-table read.

    A pid that is absent from a table that *was* read is gone. A table that
    could not be read leaves every pid unknown, because an unread table is
    not evidence that a process is gone.
    """
    wanted = wanted_pids(pids)
    if not wanted:
        return {}
    if is_windows():
        table = windows_table()
        if table is None:
            return {pid: UNKNOWN for pid in wanted}
        return {pid: (ALIVE if pid in table else GONE) for pid in wanted}
    listed = cached_scan()
    if listed is None:
        return {pid: UNKNOWN for pid in wanted}
    return {pid: (ALIVE if pid in listed else GONE) for pid in wanted}


# -- the process table ----------------------------------------------------


def scan() -> Dict[int, ProcessInstance]:
    """Snapshot of this user's processes, keyed by pid.

    Used by the passive discovery path to find candidates. A process whose
    start identity cannot be read is skipped rather than guessed.
    """
    if is_windows():
        return scan_windows()
    return scan_posix() or {}


def cached_scan() -> Optional[Dict[int, ProcessInstance]]:
    """A process listing reused for a couple of seconds.

    The start identity of a live process never changes, so re-reading the
    table cannot reveal anything new about a process that is still running.
    A process that exited will be gone from the table, but the caller
    re-checks liveness with :func:`liveness` before trusting a cached entry.

    ``None`` means the table could not be read, which the caller must not
    confuse with an empty table.
    """
    global _SCAN_CACHE, _SCAN_CACHE_AT
    now = time.monotonic()
    if _SCAN_CACHE and (now - _SCAN_CACHE_AT) < _SCAN_CACHE_TTL:
        return _SCAN_CACHE
    listed = scan_posix()
    if listed is None:
        # A failed read is not cached: the next caller gets a fresh try.
        return None
    _SCAN_CACHE = listed
    _SCAN_CACHE_AT = now
    return listed


def _parse_ps_line(line: str) -> Optional[Tuple[int, str, str]]:
    """Parse one ``pid= lstart= comm=`` line.

    ``lstart`` is five whitespace-separated fields ("Mon Oct 19 08:38:05
    2026") and the command path may itself contain spaces, so the fields
    are taken by position. Splitting the remainder instead would fold the
    path into the start identity and make the bulk read disagree with the
    single-pid read.
    """
    parts = line.split(None, 6)
    if len(parts) < 6:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    start_identity = " ".join(parts[1:6])
    comm = parts[6] if len(parts) > 6 else ""
    return pid, start_identity, comm


def scan_posix() -> Optional[Dict[int, ProcessInstance]]:
    """Every process on the host, keyed by pid.

    ``-A`` asks for all processes, not just the ones attached to this
    terminal. A plain ``ps`` listed 115 of the 1054 processes on the machine
    this was written on; every missing one was a live process, and every
    missing one would have been reported as exited.

    ``None`` means the table could not be read at all.
    """
    found: Dict[int, ProcessInstance] = {}
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,lstart=,comm="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    shared_boot = boot_id()
    for line in (out.stdout or "").splitlines():
        parsed = _parse_ps_line(line)
        if parsed is None:
            continue
        pid, start_identity, comm = parsed
        found[pid] = ProcessInstance(
            pid=pid,
            start_identity=start_identity,
            boot_id=shared_boot,
            executable_path=comm or None,
        )
    # The caller is missing from the bulk listing on some platforms; add it.
    own = os.getpid()
    if own not in found:
        one = _describe_own(own)
        if one is not None:
            found[own] = one
    return found


def _describe_own(pid: int) -> Optional[ProcessInstance]:
    """The instance for the calling process, read one pid at a time."""
    from .processes import describe  # local import: processes owns the policy

    return describe(pid)

