"""Capability declarations for the jcode file store.

Every declaration here is read-only and is backed by what the store actually
did for that session: a capability is proved only when this enumeration
observed the evidence it cites.
"""

from __future__ import annotations

from typing import Dict, Optional

from .model import (
    CAP_DETACH,
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_READ_HISTORY,
    CAP_RESUME,
    Capability,
)


class JcodeStoreCapabilityMixin:
    """Per-session capability declarations for the jcode file store."""

    def _capabilities(
        self,
        pid: Optional[int],
        process_instance,
        record_read: bool = False,
    ) -> Dict[str, Capability]:
        """What this source can actually do for one session.

        Everything here is read-only. Control capabilities are declared
        unsupported with a reason, because the file store cannot control a
        session -- only the ACP adapter can, and only on a host where the
        harness supports it.

        ``record_read`` is the *observed* outcome of reading this session's
        record. The capabilities that depend on that read (snapshot, history,
        enroll) are only proved when the read actually succeeded: a refused or
        capped read must not leave a probe behind claiming it saw the record.
        """
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved("jcode sessions/*.json")
        # Process discovery is a probe, not an inference. It is only proved
        # when this enumeration actually bound a pid to a process start
        # identity (``describe_many``/``liveness_many``). A pid file that the
        # process table could not bind proves nothing, so it stays
        # unsupported with the reason that was observed.
        if process_instance is not None:
            caps[CAP_DISCOVER_PROCESS] = Capability.proved(
                "process table bound pid {} to a start identity".format(
                    getattr(process_instance, "pid", "?")
                )
            )
        elif pid is None:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "no pid is recorded for this session, so no process was "
                "probed"
            )
        elif not self.probe_processes:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "process probing is disabled for this read; the pid file is "
                "a claimed pointer, not proof of a process"
            )
        else:
            caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
                "pid {} could not be bound to a process start identity; the "
                "pid file is a claimed pointer, not proof of a process".format(
                    pid
                )
            )
        if record_read:
            caps[CAP_READ_HISTORY] = Capability.proved(
                "jcode sessions/<native_id>.json messages[]",
            )
            caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
                "jcode sessions/<native_id>.json projected fields"
            )
        else:
            refused = (
                "the session record read was refused or capped, so the record "
                "was never observed"
            )
            caps[CAP_READ_HISTORY] = Capability.unsupported(refused)
            caps[CAP_OBSERVE_SNAPSHOT] = Capability.unsupported(refused)
        # Enrollment is a write into the harness's own session registry: it
        # needs the harness session path, not a read of the file store. A
        # read-only observer can never enroll an existing session, so this
        # stays unsupported regardless of whether the record was observed.
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment requires the harness's own session path; the "
            "read-only file store can observe a session but cannot enroll an "
            "existing one"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "the file store can see a session but cannot resume it; resume "
            "must go through the harness's own resume path with a pinned "
            "binary path"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "joining a live session requires the harness protocol (ACP); the "
            "file store is observation only"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "the file store holds no connection; there is nothing to detach"
        )
        return caps
