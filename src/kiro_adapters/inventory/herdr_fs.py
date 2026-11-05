"""HERDR's enrolled-agent snapshot, read as a session inventory.

HERDR is a terminal workspace manager for AI coding agents. Its one read-only
structured verb is ``herdr api snapshot``, which returns::

    {"id": "<request-id>",
     "result": {"type": "snapshot",
                "snapshot": {
                  "version": "0.9.3", "protocol": 22,
                  "focused_workspace_id": "wA",
                  "focused_pane_id": "wA:pH",
                  "focused_tab_id": "wA:t1",
                  "workspaces": [{"workspace_id", "label", "number", "focused",
                                  "agent_status", "active_tab_id",
                                  "pane_count", "tab_count"}],
                  "tabs": [{"tab_id", "workspace_id", "label", "number",
                            "focused", "agent_status", "pane_count"}],
                  "panes": [{"pane_id", "agent", "agent_status", "cwd",
                             "label", "focused", "workspace_id", "tab_id",
                             "terminal_id", "terminal_title",
                             "terminal_title_stripped", "revision",
                             "foreground_cwd", "scroll", "tokens"}],
                  "agents": [<the panes entry minus "label", plus
                              "state_change_seq">],
                  "layouts": [...]}}}

Observed 2026-11-05 against ``herdr 0.9.3`` / protocol ``22``.

**The limitation this reader exists to be honest about.** ``agents`` is not a
host-wide process inventory. An agent appears there only if HERDR enrolled
it -- which happens when the agent self-reports into HERDR's socket, gated on
the ``HERDR_ENV``/``HERDR_PANE_ID`` variables HERDR injects into panes it
manages. On the same host, external processes can be running while this list
is empty. So every envelope from here carries
:data:`~.herdr_paths.ENROLLMENT_SCOPE_NOTE` in its capability detail and in
its ``evidence_ref``, and the docstring of :class:`HerdrStore` repeats it.
A consumer that wants "every agent process" must join this with the process
lane; a consumer that treats this list as universal inventory is wrong.

Projection is an allowlist (``herdr_paths.PROJECTED_KEYS``). The per-agent
``tokens`` and per-pane ``scroll`` mappings are dropped: their inner value
types are not something this adapter has established, and an unclassified
mapping is exactly where a credential or a terminal dump would hide.
``terminal_title_stripped`` is kept as the title because it is already the
control-character-free variant; the unstripped variant is not read.

Read-only, bounded, fail-closed: a missing binary, a stopped server, a
timeout, an oversized stdout, malformed JSON, a payload with no ``snapshot``
object or a non-list ``agents`` all yield zero sessions -- never an
exception and never a partial parse. No socket is opened directly and no
``agent``/``pane`` control verb is ever invoked.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import herdr_paths as paths
from .model import (
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
    LAUNCH_UNKNOWN,
    SOURCE_HERDR_PANE,
    STATE_UNKNOWN,
    Capability,
    SessionEnvelope,
    make_session_uid,
    now_iso,
    sanitize_native_id,
    sanitize_user_text,
)

# A runner takes the full argv (binary first) and returns captured stdout as
# text. Injecting it is how the tests exercise every failure mode without
# HERDR installed, without a socket, and without a child process.
Runner = Callable[[Sequence[str]], str]

# How deep a ``result`` wrapper this reader will unwrap. The observed payload
# is one level (``result.snapshot``); a bare snapshot dict is also accepted so
# a future ``api snapshot`` that prints the snapshot directly keeps working.
_MAX_UNWRAP = 3


def _default_runner(binary: str, timeout: float, max_stdout: int) -> Runner:
    """A subprocess runner with a timeout, no shell and a stdout ceiling."""

    def run(argv: Sequence[str]) -> str:
        completed = subprocess.run(  # noqa: S603 - argv only, no shell
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            # A stopped server is the common case here: ``herdr api snapshot``
            # fails when no server is listening on the socket.
            raise OSError("herdr exited {}".format(completed.returncode))
        raw = completed.stdout or b""
        if len(raw) > max_stdout:
            raise ValueError("herdr stdout exceeded {} bytes".format(max_stdout))
        return raw.decode("utf-8", "replace")

    return run


class HerdrStore:
    """Read-only view of HERDR's enrolled-agent snapshot.

    CLI-backed by necessity: the snapshot is served over HERDR's socket by
    HERDR's own server, which owns the wire framing. Reading the socket
    directly, or parsing ``~/.config/herdr`` state, would be a second source
    that silently drifts from what HERDR actually reports.

    **Scope:** results are herdr-*enrolled* sessions only. HERDR learns about
    an agent solely from that agent's self-report into HERDR's socket, so an
    agent started outside HERDR is invisible here even while its process
    runs. This store answers "what has HERDR enrolled", not "what is running
    on this host".
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout: object = None,
        max_stdout: int = paths.MAX_STDOUT_BYTES,
        host_id: str = "unknown",
        namespace_id: str = "unknown",
        harness_version: Optional[str] = None,
        harness_lineage: Optional[str] = None,
        runner: Optional[Runner] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._environ = dict(environ) if environ is not None else None
        self.binary = paths.resolve_binary(binary, self._environ)
        self.timeout = paths.resolve_timeout(timeout, self._environ)
        self.max_stdout = (
            max_stdout
            if isinstance(max_stdout, int) and max_stdout > 0
            else paths.MAX_STDOUT_BYTES
        )
        self.host_id = host_id
        self.namespace_id = namespace_id
        self.harness_version = harness_version
        self.harness_lineage = harness_lineage
        self._runner = runner
        self._last_probe: Optional[str] = None

    # -- presence ----------------------------------------------------------
    def exists(self) -> bool:
        """True when a HERDR binary was resolved. No process is started.

        A resolved binary is not proof of a running server: the server may be
        stopped, in which case the snapshot read fails and enumerates zero
        sessions. This method deliberately does not probe the socket, because
        presence reporting must stay free of side effects.
        """
        return self.binary is not None

    def store_path(self) -> Optional[str]:
        """The conventional config directory, for reporting only."""
        directory = paths.config_dir(self._environ)
        return str(directory) if directory is not None else None

    def socket_path(self) -> Optional[str]:
        """The documented socket path, for reporting only. Never opened."""
        value = paths.socket_path(self._environ)
        return str(value) if value is not None else None

    # -- the one bounded read ---------------------------------------------
    def _runner_for(self) -> Optional[Runner]:
        if self._runner is not None:
            return self._runner
        if not self.binary:
            return None
        return _default_runner(self.binary, self.timeout, self.max_stdout)

    def _invoke(self, verb: Sequence[str]) -> Optional[str]:
        """Run one read verb and return stdout, or ``None`` on any failure.

        Every failure mode collapses to ``None`` on purpose: the caller
        cannot distinguish a timeout from a missing binary from a stopped
        server, and does not need to, because all three mean "this host
        contributes no HERDR sessions".
        """
        runner = self._runner_for()
        if runner is None or not self.binary:
            return None
        argv = [self.binary, *verb]
        try:
            output = runner(argv)
        except (subprocess.TimeoutExpired, OSError, ValueError, UnicodeError):
            return None
        except Exception:  # noqa: BLE001 - a probe must never raise into a list
            return None
        if not isinstance(output, str):
            return None
        if len(output.encode("utf-8", "replace")) > self.max_stdout:
            return None
        self._last_probe = " ".join(verb)
        return output

    def _snapshot(self) -> Optional[Dict[str, Any]]:
        """The ``snapshot`` object from one read, or ``None``.

        Tolerates both the observed ``{"result": {"snapshot": {...}}}``
        envelope and a bare snapshot dict; refuses anything else.
        """
        output = self._invoke(paths.SNAPSHOT_VERB)
        if output is None:
            return None
        text = output.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        current: Any = payload
        for _ in range(_MAX_UNWRAP):
            if not isinstance(current, dict):
                return None
            if isinstance(current.get("agents"), list):
                return current
            nested = current.get("snapshot")
            if isinstance(nested, dict):
                current = nested
                continue
            result = current.get("result")
            if isinstance(result, dict):
                current = result
                continue
            return None
        return None

    # -- the inventory -----------------------------------------------------
    def enumerate(self, limit: int = paths.DEFAULT_RECORD_LIMIT) -> List[SessionEnvelope]:
        """Every enrolled agent HERDR names, as envelopes, bounded by ``limit``.

        An entry with no usable ``pane_id`` is skipped: ``pane_id`` is the
        only stable handle the snapshot offers, so without it the session the
        source asserts cannot be named, and an index would be a guess.
        """
        bound = paths.clamp_limit(limit)
        if bound <= 0:
            return []
        snapshot = self._snapshot()
        if snapshot is None:
            return []
        agents = snapshot.get("agents")
        if not isinstance(agents, list):
            return []

        version = snapshot.get("version")
        observed = now_iso()
        envelopes: List[SessionEnvelope] = []
        for record in agents:
            if len(envelopes) >= bound:
                break
            if not isinstance(record, dict):
                continue
            envelope = self._envelope(record, observed, version)
            if envelope is not None:
                envelopes.append(envelope)
        return envelopes

    def _envelope(
        self,
        record: Mapping[str, Any],
        observed: str,
        version: Any,
    ) -> Optional[SessionEnvelope]:
        native_id = sanitize_native_id(_first_text(record, "pane_id"))
        if not native_id:
            return None
        state_word = _first_text(record, "agent_status")
        state = paths.STATE_WORDS.get(state_word.strip().lower(), STATE_UNKNOWN)
        harness = sanitize_user_text(
            _first_text(record, "agent") or "herdr", 64
        ) or "herdr"
        reported_version = (
            sanitize_user_text(version, 32)
            if isinstance(version, str) and version.strip()
            else self.harness_version
        )
        return SessionEnvelope(
            session_uid=make_session_uid(SOURCE_HERDR_PANE, native_id),
            native_session_id=native_id,
            source=SOURCE_HERDR_PANE,
            host_id=self.host_id,
            namespace_id=self.namespace_id,
            harness_kind=harness,
            harness_version=reported_version,
            harness_lineage=self.harness_lineage,
            process_instance=None,
            runtime_incarnation=None,
            launch_mode=LAUNCH_UNKNOWN,
            workspace=_first_text(record, "cwd") or None,
            title=_first_text(record, "terminal_title_stripped") or None,
            state=state,
            observed_at=observed,
            source_updated_at=None,
            evidence_ref=self._evidence(native_id, state_word),
            capabilities=self._capabilities(),
            externally_writable=False,
            parent_session_id=None,
        )

    def _evidence(self, native_id: str, state_word: str) -> str:
        """One line naming the verb, the pane, the raw claim and the scope.

        The source's own state word and the enrollment caveat both ride here
        rather than in structured fields, so a consumer quoting the evidence
        cannot accidentally present a herdr-enrolled snapshot as a host-wide
        inventory.
        """
        verb = self._last_probe or "api snapshot"
        claim = state_word.strip() if state_word.strip() else "state-unreported"
        return "herdr {}#{}:{} [{}]".format(
            verb, native_id, claim, paths.ENROLLMENT_SCOPE_NOTE
        )

    def _capabilities(self) -> Dict[str, Capability]:
        """What a snapshot read can honestly support.

        The snapshot is a point-in-time report by HERDR's own server about the
        agents it enrolled. It carries no pid of our own reading, no
        transcript, no input channel and no lease, so exactly two capabilities
        are proved and the rest are refused with a reason. The enrollment
        limitation is part of the *proved* discovery detail, not a footnote:
        a consumer that reads only ``supported: true`` still sees its scope.
        """
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved(
            "herdr api snapshot lists enrolled agents keyed by pane_id; {}".format(
                paths.ENROLLMENT_SCOPE_NOTE
            )
        )
        caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
            "herdr api snapshot returns one live snapshot object"
        )
        caps[CAP_READ_HISTORY] = Capability.unsupported(
            "the snapshot carries pane metadata, not turns; reading a "
            "transcript needs a pane read or the harness's own store"
        )
        caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
            "the snapshot records HERDR's enrollment claim, not a pid this "
            "adapter probed; a stopped or external process is indistinguishable"
        )
        caps[CAP_OBSERVE_STREAM] = Capability.unsupported(
            "api snapshot is a one-shot read; this adapter opens no stream"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "attaching to a pane is a terminal-management action, out of "
            "scope for a read-only inventory"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "the snapshot has no resume recipe and this adapter starts nothing"
        )
        caps[CAP_INPUT] = Capability.unsupported(
            "herdr agent prompt / pane send-text are control verbs; this "
            "adapter never invokes them"
        )
        caps[CAP_INTERRUPT] = Capability.unsupported(
            "interrupting a session is a control action, not an inventory read"
        )
        caps[CAP_PERMISSIONS] = Capability.unsupported(
            "the snapshot exposes no permission surface"
        )
        caps[CAP_TERMINAL_RESIZE] = Capability.unsupported(
            "resizing attaches to a pane; nothing here attaches"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "no connection is held, so there is nothing to detach"
        )
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment is HERDR's own agent-start path and writes host "
            "state; this adapter is read-only"
        )
        return caps

    # -- history -----------------------------------------------------------
    def history(self, native_id: str, limit: int = 20) -> Dict[str, Any]:
        """Refused, in the shape the registry expects.

        ``api snapshot`` answers with pane metadata. Turn text lives in the
        pane's terminal, which is not a transcript a discovery read may pull,
        so this reports an honest empty slice instead of re-labelling pane
        metadata as content.
        """
        del limit  # a refused read has no window
        sanitized = sanitize_native_id(native_id)
        return {
            "ok": False,
            "source": SOURCE_HERDR_PANE,
            "native_session_id": sanitized or None,
            "messages": [],
            "returned": 0,
            "detail": (
                "HERDR exposes an enrolled-pane snapshot, not a transcript; "
                "read turns from the harness's own store"
            ),
        }


def _first_text(record: Mapping[str, Any], *keys: str) -> str:
    """The first string value among ``keys``, or ``""``."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def default_store(**kwargs: Any) -> HerdrStore:
    """A store for the current user's HERDR install, if any."""
    return HerdrStore(**kwargs)


__all__ = ["HerdrStore", "Runner", "default_store"]
