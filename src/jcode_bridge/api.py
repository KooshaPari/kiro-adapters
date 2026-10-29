"""Reverse bridge: drive a live ``jcode`` session over ``jcode api --stdio``.

:mod:`jcode_bridge.backend` makes jcode a one-shot task backend: it shells out
to ``jcode run --json`` for a single answer. This module reverses the
mechanism. It starts ``jcode api --stdio`` and speaks jcode's own stable,
versioned harness API, so the caller keeps a *session* rather than a process:
it streams assistant text as it arrives, sees tool calls and token usage, and
can send more than one turn before it disconnects.

The layering is deliberate:

* :mod:`jcode_bridge.api_transport` owns the process and the error taxonomy.
* :mod:`jcode_bridge.api_client` owns the NDJSON protocol state machine.
* this module owns the ``AdapterResult`` envelope every other adapter returns.

Nothing here uses a shell, and no credential is parsed, logged or echoed: the
child inherits the caller's environment minus ``PYTHONPATH`` / ``PYTHONHOME``,
exactly as the rest of this package does.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from kiro_adapters import result as R
from kiro_adapters.redact import redact, redact_obj
from kiro_adapters.runner import resolve_binary

from .api_client import (
    CLIENT_NAME,
    CONNECT_TIMEOUT,
    PING_TIMEOUT,
    TURN_TIMEOUT,
    HarnessApiClient,
)
from .api_transport import (
    ApiTimeout,
    ApiTransportError,
    SubprocessTransport,
    Transport,
)

HARNESS = "jcode-api"
ENV_VAR = "JCODE_BRIDGE_BIN"
MAX_TIMEOUT = 86400.0


def _validate_timeout(
    value: Optional[float], default: float
) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(timeout, problem)`` after checking the shared contract.

    A timeout has to be a finite number greater than 0 and at most 86400
    seconds. Passing 0 or NaN to a bounded wait would quietly make it
    unbounded, so the check happens here at the public edge.
    """
    if value is None:
        return default, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "timeout must be a number, got {}".format(type(value).__name__)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None, "timeout must be a finite number, got {}".format(number)
    if number <= 0:
        return None, "timeout must be greater than 0 seconds, got {}".format(number)
    if number > MAX_TIMEOUT:
        return None, "timeout must be at most {} seconds, got {}".format(
            int(MAX_TIMEOUT), number
        )
    return number, None


def _redact_result(result: R.AdapterResult) -> R.AdapterResult:
    """Return ``result`` with every credential-looking string redacted."""
    return replace(
        result,
        detail=redact(result.detail),
        data=cast(Dict[str, Any], redact_obj(result.data)),
        stdout=redact(result.stdout),
        stderr=redact(result.stderr),
        hints=[redact(hint) for hint in result.hints],
    )


class JcodeApiBackend:
    """Drive a live jcode session through ``jcode api --stdio``."""

    harness = HARNESS

    def __init__(
        self,
        binary: Optional[str] = None,
        env_var: str = ENV_VAR,
        client_name: str = CLIENT_NAME,
        transport_factory: Optional[Callable[[List[str], float], Transport]] = None,
    ) -> None:
        self._binary = binary
        self.env_var = env_var
        self.client_name = client_name
        self._transport_factory = transport_factory
        self._resolved: Optional[str] = None
        self._tried: List[str] = []

    # -- resolution -------------------------------------------------------
    @property
    def binary(self) -> Optional[str]:
        if self._binary:
            return self._binary
        if self._resolved is None:
            self._resolved, self._tried = resolve_binary(("jcode",), self.env_var)
        return self._resolved

    def resolve_info(self) -> Dict[str, Any]:
        return {
            "binary": self.binary,
            "found": self.binary is not None,
            "tried": self._tried
            if not self._binary
            else ["explicit:{}".format(self._binary)],
        }

    def build_argv(self, timeout: float = 0.0) -> List[str]:
        """Return the child argv. ``--stdio`` serves one API connection."""
        return [self.binary or "jcode", "api", "--stdio"]

    def _missing(self, action: str) -> R.AdapterResult:
        return R.AdapterResult.failure(
            HARNESS,
            action,
            R.NOT_INSTALLED,
            "jcode binary not found",
            data=self.resolve_info(),
            hints=[
                "Ensure `jcode --version` works in this shell",
                "Or pin the binary: export {}=/path/to/jcode".format(self.env_var),
            ],
        )

    def _open(self, timeout: float) -> Tuple[Transport, HarnessApiClient, Dict[str, Any]]:
        """Spawn the child and complete the handshake."""
        if self._transport_factory is not None:
            transport = self._transport_factory(self.build_argv(timeout), timeout)
        else:
            transport = SubprocessTransport(self.build_argv(timeout))
        client = HarnessApiClient(transport, client_name=self.client_name)
        try:
            hello = client.handshake(timeout=timeout)
        except BaseException:
            transport.close()
            raise
        return transport, client, hello

    @staticmethod
    def _failure(
        action: str,
        exc: Exception,
        data: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> R.AdapterResult:
        code = R.ERROR
        if isinstance(exc, ApiTimeout):
            code = R.TIMEOUT
        elif isinstance(exc, ApiTransportError) and exc.spawn_failure:
            code = R.NOT_INSTALLED
        return R.AdapterResult.failure(
            HARNESS, action, code, str(exc) or type(exc).__name__,
            data=data, duration_ms=duration_ms,
        )

    @staticmethod
    def _finish(transport: Transport, data: Dict[str, Any]) -> None:
        """Close a real child, recording its exit code; keep fake ones open."""
        stderr = transport.stderr_tail()
        if stderr:
            data["stderr"] = redact(stderr)
        if isinstance(transport, SubprocessTransport):
            transport.close()
            data["exit_code"] = transport.exit_code

    # -- probes -----------------------------------------------------------
    def health(self, timeout: float = CONNECT_TIMEOUT) -> R.AdapterResult:
        """Spawn, handshake and ping without touching a session."""
        checked, problem = _validate_timeout(timeout, CONNECT_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "health", R.ERROR, problem)
        budget: float = checked if checked is not None else CONNECT_TIMEOUT
        if self.binary is None:
            return self._missing("health")
        deadline = time.monotonic() + budget
        started = time.monotonic()
        transport: Optional[Transport] = None
        data: Dict[str, Any] = {"argv": self.build_argv(), "resolve": self.resolve_info()}
        try:
            transport, client, hello = self._open(max(0.01, deadline - time.monotonic()))
            data.update(hello)
            client.ping(timeout=max(0.01, deadline - time.monotonic()))
            data["ping"] = True
            self._finish(transport, data)
            transport = None
            return _redact_result(
                R.AdapterResult.success(
                    HARNESS,
                    "health",
                    "{} ({})".format(hello.get("server"), hello.get("version")),
                    data=data,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the result envelope
            if transport is not None:
                data.setdefault("stderr", redact(transport.stderr_tail()))
                transport.close()
            return _redact_result(
                self._failure(
                    "health", exc, data=data,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )

    # -- work -------------------------------------------------------------
    def ask(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        timeout: float = TURN_TIMEOUT,
        keep_session: bool = False,
        on_permission: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> R.AdapterResult:
        """One prompt, one answer, over a real harness-API session."""
        if not isinstance(prompt, str) or not prompt.strip():
            return R.AdapterResult.failure(
                HARNESS, "ask", R.ERROR, "prompt must be a non-empty string"
            )
        if "\x00" in prompt:
            return R.AdapterResult.failure(
                HARNESS, "ask", R.ERROR, "prompt must not contain a NUL byte"
            )
        if working_dir is not None and (
            not isinstance(working_dir, str) or "\x00" in working_dir
        ):
            return R.AdapterResult.failure(
                HARNESS, "ask", R.ERROR,
                "working_dir must be a string without NUL bytes",
            )
        checked, problem = _validate_timeout(timeout, TURN_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "ask", R.ERROR, problem)
        budget: float = checked if checked is not None else TURN_TIMEOUT
        if self.binary is None:
            return self._missing("ask")
        started = time.monotonic()
        transport: Optional[Transport] = None
        data: Dict[str, Any] = {
            "argv": self.build_argv(),
            "working_dir": working_dir,
            "keep_session": keep_session,
        }
        try:
            transport, client, hello = self._open(max(0.01, budget))
            data.update(hello)
            outcome, session = client.run_turn(
                prompt,
                working_dir=working_dir,
                timeout=max(0.01, budget - (time.monotonic() - started)),
                keep_session=keep_session,
                on_permission=on_permission,
            )
            data["session"] = session
            data["turn"] = outcome.to_dict()
            data["answer"] = outcome.text
            data["answer_chars"] = len(outcome.text)
            data["turn_done"] = outcome.done
            self._finish(transport, data)
            transport = None
            if not outcome.done:
                return _redact_result(
                    R.AdapterResult.failure(
                        HARNESS, "ask", R.TIMEOUT, "turn ended without turn_done",
                        data=data,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
            return _redact_result(
                R.AdapterResult.success(
                    HARNESS, "ask", "{} chars".format(len(outcome.text)),
                    data=data,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the result envelope
            if transport is not None:
                data.setdefault("stderr", redact(transport.stderr_tail()))
                transport.close()
            return _redact_result(
                self._failure(
                    "ask", exc, data=data,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )

    def sessions(
        self, limit: Optional[int] = 20, timeout: float = PING_TIMEOUT
    ) -> R.AdapterResult:
        """List the sessions this API connection can see."""
        checked, problem = _validate_timeout(timeout, PING_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "sessions", R.ERROR, problem)
        budget: float = checked if checked is not None else PING_TIMEOUT
        if self.binary is None:
            return self._missing("sessions")
        started = time.monotonic()
        transport: Optional[Transport] = None
        try:
            transport, client, hello = self._open(max(0.01, budget))
            items = client.list_sessions(limit=limit, timeout=budget)
            data: Dict[str, Any] = {"count": len(items), "sessions": items}
            data.update(hello)
            self._finish(transport, data)
            transport = None
            return _redact_result(
                R.AdapterResult.success(
                    HARNESS, "sessions", "{} session(s)".format(len(items)),
                    data=data,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the result envelope
            if transport is not None:
                transport.close()
            return _redact_result(
                self._failure(
                    "sessions", exc,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
