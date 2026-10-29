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
        # tasklist has no start time, so no pid can be bound to a process on
        # this platform. Return nothing rather than a pid dressed up as an
        # identity; callers use :func:`liveness_many` for the verdict.
        return {}
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


def same_process(recorded: Optional[ProcessInstance], current_pid: int) -> bool:
    """True when ``current_pid`` is provably the process ``recorded`` saw.

    Two ways to fail, both refusals:

    * the pid is gone, or could not be checked -> not the same process
    * the pid is alive but its start time differs -> a *different* process
      recycled onto the same number
    """
    if recorded is None:
        return False
    if recorded.boot_id != boot_id():
        # The host rebooted. Nothing from before the reboot is the same.
        return False
    if liveness(current_pid) != ALIVE:
        return False
    now_start = process_table.process_start(current_pid)
    if now_start == process_table.UNKNOWN_START:
        # We cannot prove identity; do not claim it.
        return False
    if recorded.start_identity == process_table.UNKNOWN_START:
        # We cannot prove identity; do not claim it.
        return False
    return now_start == recorded.start_identity


def pid_start_identity(pid: int) -> Optional[str]:
    """The start identity for a pid, without building a full instance."""
    if liveness(pid) != ALIVE:
        return None
    return process_table.process_start(pid)
