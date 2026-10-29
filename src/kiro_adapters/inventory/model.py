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
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..redact import redact

# -- source identifiers -----------------------------------------------------
# Namespaced by the thing that actually produced the observation. A consumer
# can always tell a first-hand native source from a second-hand projection.
SOURCE_JCODE_FS = "jcode-fs"
SOURCE_KCODE_FS = "kcode-fs"
SOURCE_CODEX_FS = "codex-fs"
SOURCE_CODEX_APP_SERVER = "codex-app-server"
SOURCE_KIROCREW_LEDGER = "kirocrew-ledger"
SOURCE_HERDR_PANE = "herdr-pane"
SOURCE_SHARECLI_PROC = "sharecli-proc"
SOURCE_FORGE_FS = "forge-fs"

# -- launch modes -----------------------------------------------------------
LAUNCH_INTERACTIVE = "interactive"
LAUNCH_HEADLESS = "headless"
LAUNCH_UNKNOWN = "unknown"

# -- states -----------------------------------------------------------------
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"
STATE_DONE = "done"
STATE_STALE = "stale"
STATE_EXITED = "exited"
STATE_UNKNOWN = "unknown"

# -- capability names -------------------------------------------------------
CAP_DISCOVER_PROCESS = "discover_process"
CAP_DISCOVER_SESSION = "discover_session"
CAP_OBSERVE_SNAPSHOT = "observe_snapshot"
CAP_OBSERVE_STREAM = "observe_stream"
CAP_JOIN_LIVE = "join_same_live_session"
CAP_RESUME = "resume_new_execution"
CAP_INPUT = "input"
CAP_INTERRUPT = "interrupt"
CAP_PERMISSIONS = "permissions"
CAP_TERMINAL_RESIZE = "terminal_resize"
CAP_DETACH = "detach"
CAP_READ_HISTORY = "read_native_history"
CAP_ENROLL = "enroll_existing"

ALL_CAPABILITIES = (
    CAP_DISCOVER_PROCESS,
    CAP_DISCOVER_SESSION,
    CAP_OBSERVE_SNAPSHOT,
    CAP_OBSERVE_STREAM,
    CAP_JOIN_LIVE,
    CAP_RESUME,
    CAP_INPUT,
    CAP_INTERRUPT,
    CAP_PERMISSIONS,
    CAP_TERMINAL_RESIZE,
    CAP_DETACH,
    CAP_READ_HISTORY,
    CAP_ENROLL,
)


# Ceiling on ``history(limit=...)`` for every file-backed adapter. The
# transcript slice is caller-selected, which means a caller could ask for the
# whole transcript; one shared constant keeps jcode, kcode and the crew
# adapter from disagreeing about what "bounded" means. It lives here rather
# than in one adapter so the crew adapter does not have to import jcode's
# module to share a bound.
MAX_HISTORY_LIMIT = 200


def clamp_history_limit(value: Any, ceiling: int = MAX_HISTORY_LIMIT) -> int:
    """Clamp a caller-supplied history window into ``[0, ceiling]``.

    A non-numeric, negative or absurd value is not an error: it is a request
    for a window this layer will not honour, so it becomes 0 (or the ceiling)
    rather than raising. That is what makes the bound hold even when the bound
    is applied by three adapters and one registry.
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 0
    if limit <= 0:
        return 0
    return min(limit, ceiling)


def now_iso() -> str:
    """RFC3339 UTC timestamp used for every ``observed_at``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- projection sanitising --------------------------------------------------
# A title, a working directory and a native id are all user-derived text. The
# envelope is a projection consumed by other applications (tool results,
# dashboards, log lines), so raw content never travels in it: credentials are
# redacted, control characters are removed so source data cannot forge a log
# line or a failure detail, and each field is length-bounded.

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE_RUN = re.compile(r"[ ]{2,}")

MAX_TITLE_CHARS = 200
MAX_WORKSPACE_CHARS = 512
MAX_NATIVE_ID_CHARS = 256
# Environment-derived fields (host, namespace, harness version, executable
# path) are still text that came from somewhere we do not control, so they are
# redacted and bounded like any other user-derived string.
MAX_ENV_FIELD_CHARS = 256
# Machine-state fields (state, launch_mode, source_updated_at) must stay safe
# to compare by equality, so they are only stripped of control characters and
# length-bounded. They are never word-collapsed or redacted, which would change
# a value a consumer compares against.
MAX_MACHINE_STATE_CHARS = 64


def strip_control_chars(value: str) -> str:
    """Remove control characters, keeping printable text intact."""
    return _CONTROL_CHARS.sub("", value)


def sanitize_native_id(value: Optional[str]) -> str:
    """A native id safe to place in a projection or in a failure detail.

    Control characters (newlines, NUL, ESC) are removed rather than escaped:
    such an id is not a legitimate file name, and letting one through would
    let source data forge log lines and detail strings. Returns ``""`` when
    nothing printable remains, which callers must treat as "no identity".
    """
    if not value:
        return ""
    cleaned = strip_control_chars(value)
    cleaned = _SPACE_RUN.sub(" ", cleaned).strip()
    if len(cleaned) > MAX_NATIVE_ID_CHARS:
        cleaned = cleaned[:MAX_NATIVE_ID_CHARS]
    return cleaned


def sanitize_user_text(value: Optional[str], limit: int) -> Optional[str]:
    """Redact and bound user-derived text such as a title or a directory."""
    if value is None:
        return None
    cleaned = redact(strip_control_chars(value))
    cleaned = _SPACE_RUN.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 1)].rstrip() + "\u2026"
    return cleaned


def sanitize_machine_value(value: Optional[str], limit: int) -> Optional[str]:
    """Make a machine-state string safe without changing what it means.

    ``state``, ``launch_mode`` and ``source_updated_at`` are compared by exact
    equality by consumers, so they are not redacted, word-collapsed or
    ellipsised. Control characters are still removed: a newline or ESC in a
    machine field could forge a log line exactly as it could in a title.
    """
    if value is None:
        return None
    cleaned = strip_control_chars(value)
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


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


def merge_capabilities(
    primary: Dict[str, Capability], secondary: Dict[str, Capability]
) -> Dict[str, Capability]:
    """Merge two capability maps without ever inventing support.

    A capability is supported in the result if *either* side proved it, and
    unsupported otherwise. A capability neither side declared stays absent
    from the map entirely - the envelope's :meth:`SessionEnvelope.capability`
    turns an absent capability into an explicit unsupported-with-reason, so
    "not declared" never reads as "declared as false".

    When both sides declare the same capability and disagree, the side that
    proved it wins. When both are unsupported, the primary's reason is kept
    and the secondary's is appended, so the disagreement stays visible
    instead of being smoothed over.
    """
    merged: Dict[str, Capability] = dict(secondary)
    for name, cap in primary.items():
        other = merged.get(name)
        if other is None:
            merged[name] = cap
            continue
        if cap.supported and not other.supported:
            merged[name] = cap
        elif cap.supported and other.supported:
            merged[name] = cap
        elif not cap.supported and not other.supported:
            if other.reason and other.reason not in (cap.reason or ""):
                merged[name] = Capability.unsupported(
                    "{}; also: {}".format(cap.reason or "", other.reason)
                )
            else:
                merged[name] = cap
        else:
            # The secondary proved it and the primary did not. Keep the proof.
            merged[name] = other
    return merged


def envelope_sort_key(envelope: SessionEnvelope) -> tuple:
    """Deterministic ordering so two readers see the same list order."""
    return (
        envelope.source_updated_at or "",
        envelope.native_session_id,
        envelope.source,
    )


def summarize(envelopes: List[SessionEnvelope]) -> Dict[str, Any]:
    """Counts by source, launch mode and state.

    A consumer dashboard wants this shape; it deliberately does not collapse
    "process exists" into "controllable".
    """
    by_source: Dict[str, int] = {}
    by_launch_mode: Dict[str, int] = {}
    by_state: Dict[str, int] = {}
    for env in envelopes:
        by_source[env.source] = by_source.get(env.source, 0) + 1
        by_launch_mode[env.launch_mode] = by_launch_mode.get(env.launch_mode, 0) + 1
        by_state[env.state] = by_state.get(env.state, 0) + 1
    return {
        "total": len(envelopes),
        "by_source": by_source,
        "by_launch_mode": by_launch_mode,
        "by_state": by_state,
    }
