"""Upstream link to a running KCode/jcode ACP process.

Two invariants are enforced here, not trusted to callers:

* ``session/close`` is never sent. Upstream it cancels a running prompt and
  stops the event pump (src/cli/acp.rs:653-677), so it is not a detach. Detach
  is dropping the stream.
* ``session/new`` is never sent. The shim adopts, it does not create.

``InProcessUpstream`` is the test double: it never spawns anything and records
only method names, never parameters.
"""

from __future__ import annotations

import copy
import os
import queue
import subprocess
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence

from .protocol import (
    METHOD_NOT_FOUND,
    SHIM_UPSTREAM_UNAVAILABLE,
    BlockedUpstreamCall,
    RpcError,
)

FORBIDDEN_METHODS = ("session/close", "session/new")

_SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME")


class Upstream(ABC):
    """Common guard rails and call ledger for every upstream transport."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def guard(self, method: str) -> None:
        if method in FORBIDDEN_METHODS:
            raise BlockedUpstreamCall(method)
        self.calls.append(method)

    def initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("initialize", params)

    @abstractmethod
    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class InProcessUpstream(Upstream):
    """Fake upstream used by tests and by callers that inject a handler."""

    def __init__(
        self,
        handler: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        initialize_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._handler = handler
        self._initialize_result = initialize_result
        self.closed = False
        self.notifications: List[str] = []

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.guard(method)
        if method == "initialize" and self._initialize_result is not None:
            return copy.deepcopy(self._initialize_result)
        if self._handler is None:
            return {}
        value = self._handler(method, dict(params or {}))
        return copy.deepcopy(value or {})

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.guard(method)
        self.notifications.append(method)

    def close(self) -> None:
        self.closed = True


def _child_env(extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
    env["JCODE_ACP_SHIM"] = "1"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


class StdioUpstream(Upstream):
    """Newline-JSON ACP client over a child process's stdin/stdout.

    ``argv`` must be an ACP server invocation (for example ``kcode acp``). The
    child is not a daemon: it is the ACP front end the operator's own build
    already provides. The shim still refuses ``session/new`` and
    ``session/close`` at :meth:`Upstream.guard`.
    """

    def __init__(
        self,
        argv: Sequence[str],
        on_notification: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: float = 60.0,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__()
        if not argv:
            raise ValueError("argv must not be empty")
        self._timeout = timeout
        self._on_notification = on_notification
        self._proc = subprocess.Popen(
            [str(item) for item in argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=_child_env(env),
        )
        self._lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._next_id = 0
        self._alive = True
        self._closed = False
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def _pump(self) -> None:
        import json

        stdout = self._proc.stdout
        try:
            if stdout is None:
                return
            for line in stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if "method" not in message and isinstance(message_id, int):
                    with self._lock:
                        waiter = self._pending.pop(message_id, None)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                if self._on_notification is not None:
                    try:
                        self._on_notification(message)
                    except Exception:
                        pass
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        with self._lock:
            self._alive = False
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter.put({"error": {"code": SHIM_UPSTREAM_UNAVAILABLE,
                                  "message": "upstream stream ended"}})

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.guard(method)
        if self._proc.stdin is None or not self._alive:
            raise RpcError(SHIM_UPSTREAM_UNAVAILABLE, "upstream is not running")
        with self._lock:
            self._next_id += 1
            wire_id = request_id if request_id is not None else self._next_id
            waiter: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
            self._pending[wire_id] = waiter
        payload = {"jsonrpc": "2.0", "id": wire_id, "method": method,
                   "params": dict(params or {})}
        try:
            self._proc.stdin.write(_dumps(payload) + "\n")
            self._proc.stdin.flush()
        except OSError:
            with self._lock:
                self._pending.pop(wire_id, None)
            raise RpcError(SHIM_UPSTREAM_UNAVAILABLE, "upstream write failed")
        try:
            message = waiter.get(timeout=self._timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(wire_id, None)
            raise RpcError(SHIM_UPSTREAM_UNAVAILABLE, "upstream request timed out")
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise RpcError(
                code if isinstance(code, int) else SHIM_UPSTREAM_UNAVAILABLE,
                str(error.get("message") or "upstream error"),
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.guard(method)
        if self._proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        try:
            self._proc.stdin.write(_dumps(payload) + "\n")
            self._proc.stdin.flush()
        except OSError:
            self._fail_pending()

    def close(self) -> None:
        """Drop the link. No ``session/close`` is ever sent."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._fail_pending()


def _dumps(payload: Dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"))


def method_not_found(method: str) -> RpcError:
    return RpcError(METHOD_NOT_FOUND, "Unsupported ACP method: {0}".format(method))
