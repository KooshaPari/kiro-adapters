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

import ctypes
import functools
import os
import platform
import struct
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
    read_windows_process_identities,
    read_windows_table,
    scan_windows,
    windows_liveness,
    windows_table,
)
from ._windows_boot import windows_boot_epoch

# A process table read costs about a second on a busy host, and the inventory
# asks for it repeatedly while walking thousands of session records. The
# table cannot change a process's START identity, so a cached entry stays
# valid for the lifetime of the process - but a process can EXIT, so the
# cache is short-lived and every lookup re-checks liveness.
_SCAN_CACHE: Dict[int, ProcessInstance] = {}
_SCAN_CACHE_AT = 0.0
_SCAN_CACHE_TTL = 2.0

# Every start identity says how precise it is, because a one-second timestamp
# cannot separate a pid recycled inside the same second. Identities carrying
# one of these prefixes are subsecond and may be compared; anything else
# (including a bare ``ps -o lstart=`` string and :data:`UNKNOWN_START`) is
# coarse or unknown and must be refused by the policy layer in
# :mod:`.processes`.
# Windows start identities are the CIM process creation time, which has
# subsecond resolution. The prefix keeps them distinguishable from a coarse
# ``ps`` string and lets :func:`is_precise_start` accept them without relaxing
# the rule for any other platform.
WINDOWS_START_PREFIX = "cim-creation:"

PRECISE_START_PREFIXES = ("proc-stat:", "proc-bsdinfo:", WINDOWS_START_PREFIX)

# A coarse identity is kept only so it can be displayed; the prefix prevents
# it from ever being mistaken for a precise one.
COARSE_START_PREFIX = "lstart:"


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
    if is_windows():
        # ``LastBootUpTime`` is a real per-boot marker: constant for the whole
        # boot and different after a reboot. It is read once per process run
        # through the bounded capture path. When it cannot be read, say so;
        # never substitute the current process's creation time, which changes
        # every run and would make two handles from one boot disagree.
        epoch = windows_boot_epoch()
        if not epoch:
            return UNKNOWN_START
        return "cim-boot:" + epoch
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
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                # "{ sec = 1792424285, usec = 622237 } Mon Oct 19 08:38:05 2026"
                return out.stdout.strip().split(",")[0].replace("{", "").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    # Fall back to the boot time of the current process, which is at least
    # stable within a boot. If the platform will not even report that, do not
    # fabricate a stable boot identity from "unknown": a constant sentinel would
    # make pre-reboot and post-reboot handles indistinguishable.
    start = process_start(os.getpid())
    if start == UNKNOWN_START:
        return UNKNOWN_START
    return "proc-start:{}".format(start)


def process_start(pid: int) -> str:
    """The OS's own start time for ``pid``, as an opaque string.

    Only the start time is read. An earlier version also read the elapsed
    seconds on Linux, which made the "identity" change on every read and
    therefore never matched itself again; the start time alone is stable.

    The result is prefixed with its resolution. A subsecond marker
    (``/proc/<pid>/stat`` starttime on Linux, ``proc_pidinfo`` start time on
    macOS) is used when the platform offers one. The ``ps -o lstart=`` text is
    returned only as a coarse fallback, and :func:`is_precise_start` says so;
    callers must not treat a coarse value as a proven identity.
    """
    if is_windows():
        # Windows has a read-only CIM-backed creation time now. Resolve it
        # without os.kill or unbounded enumeration.
        identities = read_windows_process_identities([pid])
        if identities is None:
            return UNKNOWN_START
        identity = identities.get(int(pid)) if isinstance(identities, dict) else None
        if not identity:
            return UNKNOWN_START
        creation = identity[0] if isinstance(identity, tuple) else None
        if not isinstance(creation, str) or not creation.strip():
            return UNKNOWN_START
        # Mark the resolution explicitly so the policy layer can tell this
        # subsecond CIM timestamp from coarse ``ps`` text.
        return WINDOWS_START_PREFIX + creation.strip()
    precise = precise_start(pid)
    if precise is not None:
        return precise
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_START
    if out.returncode != 0:
        return UNKNOWN_START
    text = " ".join((out.stdout or "").split())
    if not text:
        return UNKNOWN_START
    return COARSE_START_PREFIX + text


def is_precise_start(identity: Optional[str]) -> bool:
    """True when ``identity`` is subsecond and may be compared as an identity.

    :data:`UNKNOWN_START`, a coarse ``lstart:`` value, a legacy bare
    ``ps`` string, a non-string, and ``None`` are all false. False means
    "not proof", never "a different process".
    """
    if not isinstance(identity, str):
        return False
    if identity == UNKNOWN_START:
        return False
    return identity.startswith(PRECISE_START_PREFIXES)


def precise_start(pid: int) -> Optional[str]:
    """A subsecond start identity for ``pid``, or None when none is available.

    None is the only failure answer: the caller then falls back to coarse text
    or to :data:`UNKNOWN_START`. Nothing here invents a value.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    system = platform.system()
    if system == "Linux":
        return _linux_precise_start(pid)
    if system == "Darwin":
        return _darwin_precise_start(pid)
    return None


def _linux_precise_start(pid: int) -> Optional[str]:
    """Field 22 (starttime, clock ticks since boot) of ``/proc/<pid>/stat``."""
    try:
        with open(
            "/proc/{}/stat".format(pid), "r", encoding="ascii", errors="replace"
        ) as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    return _parse_proc_stat_start(raw)


def _parse_proc_stat_start(raw: str) -> Optional[str]:
    """The ``proc-stat:`` identity from the text of ``/proc/<pid>/stat``."""
    # The command name is wrapped in parentheses and may itself contain
    # spaces and parentheses, so fields resume only after the LAST ')'.
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1 :].split()
    # rest[0] is field 3 (state); starttime is field 22.
    index = 22 - 3
    if len(rest) <= index:
        return None
    ticks = rest[index]
    if not ticks.isdigit():
        return None
    return "proc-stat:" + ticks


# ``struct proc_bsdinfo`` from <libproc.h>, a stable libproc ABI. Offsets are
# spelled out rather than mirrored in a ctypes Structure so that a layout
# change shows up as a failed self-check (pid and plausible time) instead of a
# silently wrong value.
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_BSDINFO_SIZE = 136
_DARWIN_PID_OFFSET = 12
_DARWIN_START_TVSEC_OFFSET = 120
_DARWIN_START_TVUSEC_OFFSET = 128


@functools.lru_cache(maxsize=1)
def _darwin_libproc():
    """``libproc`` with a typed ``proc_pidinfo``, or None when unavailable."""
    try:
        lib = ctypes.CDLL("libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError, ValueError):
        return None
    return lib


def _darwin_precise_start(pid: int) -> Optional[str]:
    """Microsecond start time from ``proc_pidinfo(PROC_PIDTBSDINFO)``.

    Read-only, no subprocess, no signal. The call is refused for processes the
    kernel will not describe (it returns a short read or zero); those fall back
    to coarse text, which the policy layer will refuse.
    """
    lib = _darwin_libproc()
    if lib is None:
        return None
    buffer = ctypes.create_string_buffer(_DARWIN_BSDINFO_SIZE)
    try:
        read = lib.proc_pidinfo(pid, _DARWIN_PROC_PIDTBSDINFO, 0, buffer, _DARWIN_BSDINFO_SIZE)
    except (OSError, ValueError):
        return None
    if read != _DARWIN_BSDINFO_SIZE:
        return None
    raw = buffer.raw
    try:
        seen_pid = struct.unpack_from("<I", raw, _DARWIN_PID_OFFSET)[0]
        seconds = struct.unpack_from("<Q", raw, _DARWIN_START_TVSEC_OFFSET)[0]
        micros = struct.unpack_from("<Q", raw, _DARWIN_START_TVUSEC_OFFSET)[0]
    except struct.error:
        return None
    if seen_pid != pid:
        # The struct did not describe the pid we asked about: fail closed.
        return None
    now = time.time()
    if seconds < 1_000_000_000 or seconds > now + 1:
        return None
    if micros >= 1_000_000:
        return None
    return "proc-bsdinfo:{}.{:06d}".format(seconds, micros)


def executable_path(pid: int) -> Optional[str]:
    """The command path for ``pid``, or None when it cannot be read."""
    if is_windows():
        identities = read_windows_process_identities([pid])
        if identities is None:
            return None
        identity = identities.get(int(pid)) if isinstance(identities, dict) else None
        if not identity or not isinstance(identity, tuple) or len(identity) < 2:
            return None
        executable = identity[1]
        if not isinstance(executable, str) or not executable.strip():
            return None
        return executable.strip()
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
        # Replace the second-resolution ps text with a subsecond marker where
        # the platform offers one, so the batched read agrees with the
        # single-pid read instead of handing back a coarse identity.
        precise = precise_start(pid)
        if precise is None:
            precise = COARSE_START_PREFIX + start_identity
        found[pid] = ProcessInstance(
            pid=pid,
            start_identity=precise,
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
