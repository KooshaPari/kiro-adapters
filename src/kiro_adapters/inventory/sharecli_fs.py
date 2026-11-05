"""ShareCLI's durable session ledger, read as a descriptor inventory.

ShareCLI keeps a durable agent-session ledger and answers two read-only verbs
over it: ``session list`` (a JSON array of descriptors) and ``session inspect
<ID>`` (one descriptor of the same shape). Each descriptor was observed as::

    {
      "confidence": "Exact",
      "cwd": "/path/to/worktree",
      "harness": "codex",
      "id": "codex:<uuid>",
      "resume": {"argv": ["codex", "resume", "<uuid>"],
                 "cwd": "...", "harness": "codex", "session_id": "<uuid>"},
      "session_id": "<uuid>",
      "state": "Active"
    }

Only ``id``/``session_id``/``harness``/``cwd``/``state``/``confidence`` are
projected. ``resume.argv`` is deliberately dropped: it is an executable
recipe, and an argv is one of the places a credential can hide. The reader
never runs it, and the envelope never carries it.

Two consequences follow, and both are stated in the envelope rather than left
for a consumer to infer:

* the descriptor carries no pid, so ``process_instance`` stays ``None`` and no
  capability claims process discovery;
* the verbs return a descriptor, not a transcript, so ``read_native_history``
  is declared unsupported. A consumer that needs turns reads the harness's own
  store, not this one.

Read-only, bounded, fail-closed: a missing binary, a timeout, an oversized
stdout, malformed JSON or a non-array payload all yield zero sessions, never
an exception and never a partial parse.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import sharecli_paths as paths
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
    SOURCE_SHARECLI_SESSION,
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
# ShareCLI installed and without a child process.
Runner = Callable[[Sequence[str]], str]


def _default_runner(
    binary: str, timeout: float, max_stdout: int
) -> Runner:
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
            raise OSError(
                "sharecli exited {}".format(completed.returncode)
            )
        raw = completed.stdout or b""
        if len(raw) > max_stdout:
            raise ValueError("sharecli stdout exceeded {} bytes".format(max_stdout))
        return raw.decode("utf-8", "replace")

    return run


class ShareCLIStore:
    """Read-only view of ShareCLI's session descriptors.

    The store is CLI-backed by necessity: ShareCLI is the authority on which
    ledger it is answering from (``--db``), so parsing a guessed sqlite path
    would be a second, silently divergent source.
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
        self.max_stdout = max_stdout if isinstance(max_stdout, int) and max_stdout > 0 else paths.MAX_STDOUT_BYTES
        self.host_id = host_id
        self.namespace_id = namespace_id
        self.harness_version = harness_version
        self.harness_lineage = harness_lineage
        self._runner = runner
        self._last_probe: Optional[str] = None

    # -- presence ----------------------------------------------------------
    def exists(self) -> bool:
        """True when a ShareCLI binary was resolved. No process is started."""
        return self.binary is not None

    def store_path(self) -> Optional[str]:
        """The conventional state directory, for reporting only."""
        directory = paths.app_support_dir(self._environ)
        return str(directory) if directory is not None else None

    # -- the one bounded read ---------------------------------------------
    def _runner_for(self) -> Optional[Runner]:
        if self._runner is not None:
            return self._runner
        if not self.binary:
            return None
        return _default_runner(self.binary, self.timeout, self.max_stdout)

    def _invoke(self, verb: Sequence[str]) -> Optional[str]:
        """Run one read verb and return stdout, or ``None`` on any failure.

        Every failure mode is collapsed to ``None`` on purpose: the caller
        cannot distinguish a timeout from a missing binary, and does not need
        to, because both mean "this host contributes no ShareCLI sessions".
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

    def _records(self, verb: Sequence[str]) -> List[Dict[str, Any]]:
        """Parse a JSON array of descriptors, dropping anything else."""
        output = self._invoke(verb)
        if output is None:
            return []
        text = output.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    # -- the inventory -----------------------------------------------------
    def enumerate(self, limit: int = paths.DEFAULT_RECORD_LIMIT) -> List[SessionEnvelope]:
        """Every descriptor ShareCLI names, as envelopes, bounded by ``limit``.

        A descriptor with no usable id is skipped: without an id the session
        the source asserts cannot be named, and an index would be a guess.
        """
        bound = paths.clamp_limit(limit)
        if bound <= 0:
            return []
        observed = now_iso()
        envelopes: List[SessionEnvelope] = []
        for record in self._records(paths.SESSION_VERB):
            if len(envelopes) >= bound:
                break
            envelope = self._envelope(record, observed)
            if envelope is not None:
                envelopes.append(envelope)
        return envelopes

    def _envelope(
        self, record: Dict[str, Any], observed: str
    ) -> Optional[SessionEnvelope]:
        native_id = sanitize_native_id(_first_text(record, "id", "session_id"))
        if not native_id:
            return None
        state_word = _first_text(record, "state")
        state = paths.STATE_WORDS.get(state_word.strip().lower(), STATE_UNKNOWN)
        harness = sanitize_user_text(
            _first_text(record, "harness") or "sharecli", 64
        ) or "sharecli"
        return SessionEnvelope(
            session_uid=make_session_uid(SOURCE_SHARECLI_SESSION, native_id),
            native_session_id=native_id,
            source=SOURCE_SHARECLI_SESSION,
            host_id=self.host_id,
            namespace_id=self.namespace_id,
            harness_kind=harness,
            harness_version=self.harness_version,
            harness_lineage=self.harness_lineage,
            process_instance=None,
            runtime_incarnation=None,
            launch_mode=LAUNCH_UNKNOWN,
            workspace=_first_text(record, "cwd") or None,
            title=None,
            state=state,
            observed_at=observed,
            source_updated_at=None,
            evidence_ref=self._evidence(native_id, state_word),
            capabilities=self._capabilities(),
            externally_writable=False,
            parent_session_id=None,
        )

    def _evidence(self, native_id: str, state_word: str) -> str:
        """One line naming the verb, the id and the source's own state word.

        The state word rides here rather than in the envelope's ``state``
        field when it is not one this adapter recognises, so the raw claim is
        preserved without being reinterpreted.
        """
        verb = self._last_probe or "session list"
        claim = state_word.strip() if state_word.strip() else "state-unreported"
        return "sharecli {}#{}:{}".format(verb, native_id, claim)

    def _capabilities(self) -> Dict[str, Capability]:
        """What a descriptor read can honestly support.

        The descriptor is a point-in-time claim by ShareCLI. It carries no
        pid, no transcript, no connection and no control channel, so exactly
        two capabilities are proved and the rest are refused with a reason.
        """
        caps: Dict[str, Capability] = {}
        caps[CAP_DISCOVER_SESSION] = Capability.proved(
            "sharecli session list (JSON descriptor array)"
        )
        caps[CAP_OBSERVE_SNAPSHOT] = Capability.proved(
            "sharecli session list returns one descriptor per ledger entry"
        )
        caps[CAP_READ_HISTORY] = Capability.unsupported(
            "session list and session inspect return a descriptor only; no "
            "transcript surface was observed"
        )
        caps[CAP_DISCOVER_PROCESS] = Capability.unsupported(
            "the descriptor records no pid; liveness is the source's claim, "
            "not a probe (process inventory is the separate proc lane)"
        )
        caps[CAP_OBSERVE_STREAM] = Capability.unsupported(
            "the descriptor is a snapshot; the CLI has no streaming verb"
        )
        caps[CAP_JOIN_LIVE] = Capability.unsupported(
            "joining a session goes through its harness, not through a "
            "descriptor read"
        )
        caps[CAP_RESUME] = Capability.unsupported(
            "the descriptor carries a resume command recipe, but this "
            "adapter never executes it and holds no lease to resume with"
        )
        caps[CAP_INPUT] = Capability.unsupported(
            "no input channel exists on a descriptor read"
        )
        caps[CAP_INTERRUPT] = Capability.unsupported(
            "interrupting a session belongs to its harness"
        )
        caps[CAP_PERMISSIONS] = Capability.unsupported(
            "no permission surface is exposed on the session verbs"
        )
        caps[CAP_TERMINAL_RESIZE] = Capability.unsupported(
            "no terminal is attached to a descriptor read"
        )
        caps[CAP_DETACH] = Capability.unsupported(
            "the descriptor holds no connection; there is nothing to detach"
        )
        caps[CAP_ENROLL] = Capability.unsupported(
            "enrollment writes to ShareCLI's ledger; this adapter is read-only"
        )
        return caps

    # -- history -----------------------------------------------------------
    def history(self, native_id: str, limit: int = 20) -> Dict[str, Any]:
        """Refused, in the shape the registry expects.

        ``session inspect`` answers with the same descriptor as ``session
        list``, so there is no turn to return. Reporting an honest empty slice
        is better than re-labelling the descriptor as content.
        """
        del limit  # a refused read has no window
        sanitized = sanitize_native_id(native_id)
        return {
            "ok": False,
            "source": SOURCE_SHARECLI_SESSION,
            "native_session_id": sanitized or None,
            "messages": [],
            "returned": 0,
            "detail": (
                "ShareCLI exposes a session descriptor, not a transcript; "
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


def default_store(**kwargs: Any) -> ShareCLIStore:
    """A store for the current user's ShareCLI install, if any."""
    return ShareCLIStore(**kwargs)


__all__ = ["Runner", "ShareCLIStore", "default_store"]
