"""The normalized session envelope every agent-ops adapter emits.

One shape, one meaning, one set of rules. The point of this module is that a
consumer app never has to know whether a session came from jcode's on-disk
store, Codex's rollout jsonl, Forge's conversations table or a Kiro Crew
ledger — it only ever sees a :class:`SessionEnvelope`.

Three rules are enforced here rather than in prose, because prose does not
fail a test:

1. A **pid alone never identifies a session**. :class:`ProcessInstance`
   requires a start identity, and a session with no provable native id is
   never emitted at all.
2. ``observed_at`` (when *we* looked) and ``source_updated_at`` (when the
   *source* says it changed) are separate fields and are never conflated.
3. A capability is only ``supported`` when the adapter that probed it recorded
   the probe. An exit code of 0 is not a probe.

The vocabulary this envelope speaks (:mod:`._model_constants`) and the
projection-sanitising rules it obeys (:mod:`._model_sanitize`) live in their
own modules; both are re-exported from here so that this module remains the
single public import surface it has always been.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ._model_constants import (  # noqa: F401  (re-exported public surface)
    ALL_CAPABILITIES,
    CAP_DETACH,
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_INPUT,
    CAP_INTERRUPT,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_OBSERVE_STREAM,
    CAP_PERMISSIONS,
    CAP_READ_HISTORY,
    CAP_RESUME,
    CAP_TERMINAL_RESIZE,
    LAUNCH_HEADLESS,
    LAUNCH_INTERACTIVE,
    LAUNCH_UNKNOWN,
    MAX_HISTORY_LIMIT,
    SOURCE_CODEX_APP_SERVER,
    SOURCE_CODEX_FS,
    SOURCE_FORGE_FS,
    SOURCE_HERDR_PANE,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    SOURCE_KIROCREW_LEDGER,
    SOURCE_SHARECLI_PROC,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WORKING,
    clamp_history_limit,
    now_iso,
)
from ._model_sanitize import (  # noqa: F401  (re-exported public surface)
    MAX_ENV_FIELD_CHARS,
    MAX_MACHINE_STATE_CHARS,
    MAX_NATIVE_ID_CHARS,
    MAX_TITLE_CHARS,
    MAX_WORKSPACE_CHARS,
    sanitize_machine_value,
    sanitize_native_id,
    sanitize_user_text,
    strip_control_chars,
)


@dataclass(frozen=True)
class ProcessInstance:
    """A process, bound tightly enough that pid reuse cannot fool us.

    ``start_identity`` is the OS's own record of when the process began
    (``ps`` start time on POSIX, creation time on Windows). Two processes
    with the same pid at different times are different processes.
    """

    pid: int
    start_identity: str
    boot_id: str
    executable_path: Optional[str] = None

    def __post_init__(self) -> None:
        # An executable path is environment-derived text and reaches the same
        # projection as a workspace, so it gets the same treatment.
        object.__setattr__(
            self,
            "executable_path",
            sanitize_user_text(self.executable_path, MAX_ENV_FIELD_CHARS),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "start_identity": self.start_identity,
            "boot_id": self.boot_id,
            "executable_path": self.executable_path,
        }


@dataclass(frozen=True)
class Capability:
    """One declared capability with the evidence that backs it.

    ``supported`` is only true when ``probe`` and ``observed_at`` are both
    present. That is checked in :meth:`__post_init__` so an adapter cannot
    accidentally claim a capability it did not probe.
    """

    supported: bool
    probe: Optional[str] = None
    observed_at: Optional[str] = None
    reason: Optional[str] = None
    replay: Optional[str] = None
    gap_policy: Optional[str] = None

    def __post_init__(self) -> None:
        if self.supported and not (self.probe and self.observed_at):
            raise ValueError(
                "a supported capability needs a probe and a probe timestamp; "
                "got probe={!r} observed_at={!r}".format(self.probe, self.observed_at)
            )
        if not self.supported and not self.reason:
            raise ValueError(
                "an unsupported capability needs a reason; got none for {!r}".format(
                    self.probe
                )
            )

    @classmethod
    def proved(cls, probe: str, observed_at: Optional[str] = None) -> "Capability":
        return cls(supported=True, probe=probe, observed_at=observed_at or now_iso())

    @classmethod
    def unsupported(cls, reason: str) -> "Capability":
        return cls(supported=False, reason=reason)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"supported": self.supported}
        if self.probe is not None:
            out["probe"] = self.probe
        if self.observed_at is not None:
            out["observed_at"] = self.observed_at
        if self.reason is not None:
            out["reason"] = self.reason
        if self.replay is not None:
            out["replay"] = self.replay
        if self.gap_policy is not None:
            out["gap_policy"] = self.gap_policy
        return out


@dataclass(frozen=True)
class SessionEnvelope:
    """One agent session, as seen by one source at one moment."""

    session_uid: str
    native_session_id: str
    source: str
    host_id: str
    namespace_id: str
    harness_kind: str
    harness_version: Optional[str] = None
    harness_lineage: Optional[str] = None
    process_instance: Optional[ProcessInstance] = None
    runtime_incarnation: Optional[str] = None
    launch_mode: str = LAUNCH_UNKNOWN
    workspace: Optional[str] = None
    title: Optional[str] = None
    state: str = STATE_UNKNOWN
    observed_at: str = field(default_factory=now_iso)
    source_updated_at: Optional[str] = None
    evidence_ref: Optional[str] = None
    capabilities: Dict[str, Capability] = field(default_factory=dict)
    # Non-authoritative artifacts bearing on ``launch_mode`` (for example a
    # ``client_sessions/<pid>`` file). These are evidence for a consumer to
    # weigh, never a verdict: an artifact's presence does not prove a mode and
    # its absence does not disprove one.
    launch_hints: Tuple[str, ...] = ()
    externally_writable: bool = False
    parent_session_id: Optional[str] = None

    def __post_init__(self) -> None:
        raw_native_id = self.native_session_id
        native_id = sanitize_native_id(raw_native_id)
        if not native_id:
            raise ValueError(
                "a session envelope needs a native session id; pid alone is "
                "never an identity (source={!r})".format(self.source)
            )
        if native_id != raw_native_id:
            # The id was sanitised, so the derived uid and parent link must be
            # rebuilt rather than left carrying the raw bytes.
            object.__setattr__(self, "native_session_id", native_id)
            object.__setattr__(
                self, "session_uid", make_session_uid(self.source, native_id)
            )
        object.__setattr__(
            self, "title", sanitize_user_text(self.title, MAX_TITLE_CHARS)
        )
        object.__setattr__(
            self, "workspace", sanitize_user_text(self.workspace, MAX_WORKSPACE_CHARS)
        )
        object.__setattr__(
            self,
            "evidence_ref",
            sanitize_user_text(self.evidence_ref, MAX_WORKSPACE_CHARS),
        )
        # Environment-derived text: redacted and bounded like a title.
        object.__setattr__(
            self, "host_id", sanitize_user_text(self.host_id, MAX_ENV_FIELD_CHARS) or ""
        )
        object.__setattr__(
            self,
            "namespace_id",
            sanitize_user_text(self.namespace_id, MAX_ENV_FIELD_CHARS) or "",
        )
        object.__setattr__(
            self,
            "harness_version",
            sanitize_user_text(self.harness_version, MAX_ENV_FIELD_CHARS),
        )
        # Machine-state values: control characters only, no re-interpretation.
        object.__setattr__(
            self,
            "state",
            sanitize_machine_value(self.state, MAX_MACHINE_STATE_CHARS) or "",
        )
        object.__setattr__(
            self,
            "launch_mode",
            sanitize_machine_value(self.launch_mode, MAX_MACHINE_STATE_CHARS) or "",
        )
        object.__setattr__(
            self,
            "source_updated_at",
            sanitize_machine_value(self.source_updated_at, MAX_MACHINE_STATE_CHARS),
        )
        object.__setattr__(
            self, "parent_session_id", sanitize_native_id(self.parent_session_id) or None
        )
        if not self.session_uid:
            raise ValueError("a session envelope needs a session_uid")

    def capability(self, name: str) -> Capability:
        """Return the declared capability, or an explicit unsupported marker.

        A capability nobody declared is *not* silently absent: it comes back
        as unsupported with a reason, so a consumer can tell "not declared"
        from "declared as false".
        """
        found = self.capabilities.get(name)
        if found is None:
            return Capability.unsupported("not declared by source {!r}".format(self.source))
        return found

    def supports(self, name: str) -> bool:
        return self.capability(name).supported

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_uid": self.session_uid,
            "native_session_id": self.native_session_id,
            "source": self.source,
            "host_id": self.host_id,
            "namespace_id": self.namespace_id,
            "harness_kind": self.harness_kind,
            "harness_version": self.harness_version,
            "harness_lineage": self.harness_lineage,
            "process_instance": (
                self.process_instance.to_dict() if self.process_instance else None
            ),
            "runtime_incarnation": self.runtime_incarnation,
            "launch_mode": self.launch_mode,
            "workspace": self.workspace,
            "title": self.title,
            "state": self.state,
            "observed_at": self.observed_at,
            "source_updated_at": self.source_updated_at,
            "evidence_ref": self.evidence_ref,
            "capabilities": {
                key: value.to_dict() for key, value in self.capabilities.items()
            },
            "launch_hints": list(self.launch_hints),
            "externally_writable": self.externally_writable,
            "parent_session_id": self.parent_session_id,
        }


def make_session_uid(source: str, native_session_id: str) -> str:
    """Globally unique, namespaced by the source that produced it.

    Two different sources observing the same native id must not collide, and
    the same source must be stable across runs.
    """
    return "{}:{}".format(source, native_session_id)


# Collection-level helpers live in their own module because they describe many
# sessions rather than one, but they are part of this module's public surface:
# the import is placed after the definitions above so the aggregate module can
# import the envelope types without a circular import.
from ._model_aggregate import (  # noqa: E402,F401  (re-exported public surface)
    envelope_sort_key,
    merge_capabilities,
    summarize,
)
