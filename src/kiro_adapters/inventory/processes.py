"""Process liveness that survives pid reuse.

``kill -0``-style checks only answer "does this pid exist right now". A pid
that was recycled onto an unrelated process still answers yes. The only
defense is to compare the OS's own record of *when* the process started.

The platform probes live in :mod:`.process_table`; this module is the policy
layer, and its one rule is that a failed probe is never reported as a fact.
A caller gets one of three answers - alive, gone, unknown - and only "gone"
is allowed to become the word "exited".

On POSIX the start time comes from ``ps -o lstart=`` and the boot id from
``/proc/sys/kernel/random/boot_id`` on Linux or ``kern.boottime`` on macOS.
On Windows there is no ``ps`` and no cheap start time, so a process can be
seen but not identified, and the identity says so.

This module is deliberately dependency-free and never writes.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from . import process_table
from .model import ProcessInstance
from .process_table import ALIVE, GONE, UNKNOWN  # noqa: F401  (public verdicts)

__all__ = [
    "ALIVE",
    "GONE",
    "UNKNOWN",
    "boot_id",
    "describe",
    "describe_many",
    "is_alive",
    "is_windows",
    "liveness",
    "liveness_many",
    "pid_start_identity",
    "same_process",
    "scan",
]


def is_windows() -> bool:
    """True on Windows, where ``os.kill(pid, 0)`` is a terminate, not a probe."""
    return process_table.is_windows()


def boot_id() -> str:
    """A value that changes on every reboot."""
    return process_table.boot_id()


def scan() -> Dict[int, ProcessInstance]:
    """Snapshot of the host's processes, keyed by pid."""
    return process_table.scan()


def liveness(pid: int) -> str:
    """One of ``alive``, ``gone`` or ``unknown`` for ``pid``.

    ``unknown`` means the probe could not answer. It is not a synonym for
    "gone": reporting "the process exited" from a failed probe invents a
    fact that was never observed.
    """
    return process_table.liveness(pid)


def is_alive(pid: int) -> bool:
    """True when the pid names a live process right now.

    This is the *weak* check. Use :func:`describe` when you need to know that
    it is the *same* process you saw before.

    An ``unknown`` verdict is not proof of life, so it reads as False here.
    Callers that must distinguish "gone" from "could not tell" use
    :func:`liveness` instead.
    """
    return liveness(pid) == ALIVE


def describe(pid: int) -> Optional[ProcessInstance]:
    """A :class:`ProcessInstance` for a live pid, or None when it is gone.

    The returned instance carries the start identity and boot id, so a later
    call can prove it is the same process rather than a recycled pid.

    A process whose start identity the platform will not report is *omitted*
    (``None``), not described with ``start_identity="unknown"``: a pid alone
    is not an identity, and an instance that cannot be re-verified would be
    a claim the OS never made.

    ``None`` means "no instance", which covers both a gone process and one
    the platform could not confirm. Callers that must tell the two apart ask
    :func:`liveness` first.
    """
    if liveness(pid) != ALIVE:
        return None
    start_identity = process_table.process_start(pid)
    if start_identity == process_table.UNKNOWN_START:
        # Alive, but unidentifiable. Omitting the instance is the honest
        # answer; emitting one would treat a pid as an identity.
        return None
    return ProcessInstance(
        pid=int(pid),
        start_identity=start_identity,
        boot_id=boot_id(),
        executable_path=process_table.executable_path(pid),
    )


def describe_many(pids: Iterable[int]) -> Dict[int, ProcessInstance]:
    """Describe many pids with **one** process-table read.

    Enumerating a jcode store means asking about thousands of sessions, and
    one ``ps`` invocation per session is unusably slow.

    The read is a *full* process listing filtered client-side rather than a
    ``ps -p <list>`` query, because ``ps -p`` refuses the ENTIRE query when
    any single pid in the list is out of the OS's pid range or otherwise
    invalid - it prints nothing at all rather than skipping the bad entry.
    A stale session record naming a long-gone pid would then blank the
    whole result, and the inventory would silently report zero live
    sessions. A full listing has no such failure mode.

    A pid that is not in the listing is simply absent. Absence is the
    honest answer; a caller must not fill it in with a guess. When the
    listing could not be read at all, nothing is returned and
    :func:`liveness_many` reports the pids as unknown.

    Every returned entry is re-checked for liveness, because the cached
    table may be a couple of seconds old and a process can exit inside that
    window. An entry whose start identity is unknown is dropped as well: it
    could not be bound, so it is not a process identity.
    """
    wanted = process_table.wanted_pids(pids)
    if not wanted:
        return {}
    if process_table.is_windows():
        # Windows can bind a pid via CIM read-only creation time + executable
        # path. Query only the requested pids (bounded), never enumerate all.
        identities = process_table.read_windows_process_identities(list(wanted))
        if identities is None:
            return {}
        bound: Dict[int, ProcessInstance] = {}
        shared_boot = boot_id()
        for pid in wanted:
            identity = identities.get(pid) if isinstance(identities, dict) else None
            if not identity or not isinstance(identity, tuple) or len(identity) < 2:
                continue
            creation, executable = identity[0], identity[1]
            if not isinstance(creation, str) or not creation.strip():
                continue
            if not isinstance(executable, str) or not executable.strip():
                continue
            if liveness(pid) != ALIVE:
                continue
            bound[pid] = ProcessInstance(
                pid=int(pid),
                start_identity=creation.strip(),
                boot_id=shared_boot,
                executable_path=executable.strip(),
            )
        return bound
    listed = process_table.cached_scan() or {}
    bound: Dict[int, ProcessInstance] = {}
    for pid in wanted:
        instance = listed.get(pid)
        if instance is None:
            continue
        if instance.start_identity == process_table.UNKNOWN_START:
            continue
        if liveness(pid) != ALIVE:
            # The table entry is stale: the process exited after the read.
            continue
        bound[pid] = instance
    return bound


def liveness_many(pids: Iterable[int]) -> Dict[int, str]:
    """The liveness verdict for many pids, from one process-table read.

    This is the batched form of :func:`liveness`, and it keeps "gone" and
    "unknown" apart: a pid missing from a table that was read is gone, while
    a table that could not be read leaves every pid unknown.
    """
    return process_table.liveness_many(pids)


def _exact_pid(value) -> Optional[int]:
    """``value`` as the positive integer pid it exactly is, else None.

    Nothing is coerced. ``bool`` is refused even though it subclasses ``int``
    (``True`` would otherwise pass for pid 1), a float is refused rather than
    truncated (``12.9`` must not answer for pid 12), and a string counts only
    when it is nothing but digits.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            pid = int(text)
            return pid if pid > 0 else None
    return None


def same_process(recorded: Optional[ProcessInstance], current_pid: int) -> bool:
    """True when ``current_pid`` is provably the process ``recorded`` saw.

    Every failure is a refusal, never a guess:

    * the recorded pid is not ``current_pid`` -> a different process
    * the pid is gone, or could not be checked -> not the same process
    * the pid is alive but its start time differs -> a *different* process
      recycled onto the same number
    * either the recorded or the current boot identity is unknown -> not
      proven, so not the same process
    * either start identity is coarse (a one-second ``ps`` timestamp) or
      unknown -> not proven, so not the same process
    * the executable path cannot be read on either side, or the two paths
      differ -> not proven, so not the same process

    Two witnesses are required because neither alone is enough. A
    one-second ``ps`` start time cannot separate a pid recycled inside the
    same second, so a coarse identity is refused outright; only a subsecond
    marker (``/proc/<pid>/stat`` starttime, ``proc_pidinfo`` start time) is
    compared. :mod:`.process_table` prefixes every identity with its
    resolution, and :func:`process_table.is_precise_start` is the single
    arbiter. The executable path is the second witness, and an unreadable
    path on either side fails closed rather than degrading to "the pid and
    the second happened to agree".

    Windows is always refused today: its boot identity is
    :data:`process_table.UNKNOWN_START`, so nothing can be placed on a boot
    timeline even when the CIM creation time and executable match.
    """
    if recorded is None:
        return False
    wanted_pid = _exact_pid(current_pid)
    if wanted_pid is None:
        return False
    recorded_pid = _exact_pid(getattr(recorded, "pid", None))
    if recorded_pid is None or recorded_pid != wanted_pid:
        # A handle describes one process. Asked about another pid, the only
        # honest answer is "not the same process". A value that is not a pid
        # at all is not evidence either.
        return False
    recorded_boot = recorded.boot_id
    current_boot = boot_id()
    if recorded_boot == process_table.UNKNOWN_START:
        # The record cannot be placed on a boot timeline at all.
        return False
    if current_boot == process_table.UNKNOWN_START:
        # This host cannot say which boot it is on, so no handle can be
        # proven to belong to it.
        return False
    if recorded_boot != current_boot:
        # The host rebooted. Nothing from before the reboot is the same.
        return False
    if not process_table.is_precise_start(recorded.start_identity):
        # Coarse (one-second) or unknown: not proof of identity.
        return False
    if liveness(wanted_pid) != ALIVE:
        return False
    now_start = process_table.process_start(wanted_pid)
    if now_start == process_table.UNKNOWN_START:
        # We cannot prove identity; do not claim it.
        return False
    if not process_table.is_precise_start(now_start):
        # The current probe degraded to coarse text; that is not proof.
        return False
    if now_start != recorded.start_identity:
        return False
    # A second-resolution start time plus a pid is not on its own enough to
    # prove identity (a pid recycled inside the same second would match). The
    # executable path is required on both sides: absent on the record, or
    # unreadable now, means the identity is unproven and this returns False.
    recorded_exe = getattr(recorded, "executable_path", None)
    if not recorded_exe:
        return False
    now_exe = process_table.executable_path(wanted_pid)
    if not now_exe:
        return False
    if now_exe != recorded_exe:
        return False
    return True


def pid_start_identity(pid: int) -> Optional[str]:
    """The start identity for a pid, without building a full instance."""
    if liveness(pid) != ALIVE:
        return None
    return process_table.process_start(pid)
