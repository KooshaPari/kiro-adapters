"""Process transport and error taxonomy for the jcode harness-API bridge.

The stable jcode harness API is newline-delimited JSON over one connection.
:class:`Transport` is the three-operation contract the protocol client needs;
:class:`SubprocessTransport` implements it over a child process's stdio, so
``jcode api --stdio`` can be driven without a shell and without a socket.

Reads and stderr draining both happen on daemon threads. A blocking
``readline`` cannot honour a deadline, and an undrained stderr pipe deadlocks a
chatty child, so neither is left to the operating system's buffer sizes.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
from typing import Dict, List, Optional, Protocol

from kiro_adapters.runner import build_env

#: Largest single frame either side may send. Mirrors the server's own cap;
#: a client that trusted an unbounded line could buffer 16 MiB per frame too.
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: stderr lines retained for diagnostics before the oldest are dropped.
STDERR_LINES = 2000


class ApiError(Exception):
    """Base class for every failure raised by this bridge."""


class ApiProtocolError(ApiError):
    """The peer broke the frame contract: bad JSON, bad version, bad reply."""


class ApiRemoteError(ApiError):
    """The server answered a request with an ``error`` frame."""

    def __init__(self, code: Optional[str], message: str) -> None:
        self.code = code or "unknown"
        self.message = message
        super().__init__("jcode api error {}: {}".format(self.code, self.message))


class ApiTransportError(ApiError):
    """The child process could not be started, or the stream died."""

    def __init__(self, message: str, spawn_failure: bool = False) -> None:
        self.spawn_failure = spawn_failure
        super().__init__(message)


class ApiTimeout(ApiError):
    """The peer did not produce the expected frame in time."""


class Transport(Protocol):
    """The operations the protocol client needs from one connection."""

    def send_line(self, line: str) -> None:
        """Write one complete frame, including its newline."""

    def read_line(self, timeout: Optional[float]) -> Optional[str]:
        """Return one frame without its newline, ``None`` at end of stream.

        Raises :class:`TimeoutError` when ``timeout`` elapses first.
        """

    def close(self) -> None:
        """End the connection. Safe to call more than once."""

    def stderr_tail(self, limit: int = 20000) -> str:
        """Bounded diagnostic text from the peer's stderr."""


class SubprocessTransport:
    """``Transport`` over a child process's stdin/stdout."""

    def __init__(
        self,
        argv: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        close_grace: float = 2.0,
    ) -> None:
        self.argv = [str(part) for part in argv]
        self.close_grace = close_grace
        self._stderr: List[str] = []
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._exit_code: Optional[int] = None
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=build_env(env),
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            raise ApiTransportError(
                "failed to launch {}: {}: {}".format(
                    self.argv[0], type(exc).__name__, exc
                ),
                spawn_failure=isinstance(exc, (FileNotFoundError, PermissionError)),
            )
        self._frames: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # -- pumps ------------------------------------------------------------
    def _pump_stdout(self) -> None:
        stream = self._proc.stdout
        if stream is None:  # pragma: no cover - Popen always gives us a pipe
            self._frames.put(None)
            return
        try:
            for line in stream:
                self._frames.put(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._frames.put(None)

    def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:  # pragma: no cover
            return
        try:
            for line in stream:
                with self._stderr_lock:
                    self._stderr.append(line.rstrip("\n"))
                    if len(self._stderr) > STDERR_LINES:
                        del self._stderr[: STDERR_LINES // 2]
        except (OSError, ValueError):
            pass

    # -- transport --------------------------------------------------------
    def send_line(self, line: str) -> None:
        if self._closed:
            raise ApiTransportError("jcode api stream is closed")
        size = len(line.encode("utf-8"))
        if size > MAX_FRAME_BYTES:
            raise ApiProtocolError(
                "refusing to send a {} byte frame (limit {})".format(size, MAX_FRAME_BYTES)
            )
        stream = self._proc.stdin
        if stream is None:  # pragma: no cover
            raise ApiTransportError("jcode api stream has no stdin")
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError) as exc:
            raise ApiTransportError("failed to write to jcode api: {}".format(exc))

    def read_line(self, timeout: Optional[float]) -> Optional[str]:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no frame within {}s".format(timeout))

    def stderr_tail(self, limit: int = 20000) -> str:
        with self._stderr_lock:
            text = "\n".join(self._stderr)
        if len(text) <= limit:
            return text
        return "...[truncated {} chars]\n".format(len(text) - limit) + text[-limit:]

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._proc, "pid", None)

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stream = self._proc.stdin
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._exit_code = self._proc.wait(timeout=self.close_grace)
        except subprocess.TimeoutExpired:
            self._terminate_group()
            try:
                self._exit_code = self._proc.wait(timeout=self.close_grace)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
                self._proc.kill()
                self._exit_code = self._proc.wait(timeout=self.close_grace)

    def _terminate_group(self) -> None:
        """Signal the child's whole process group, then the child itself.

        The child was started with ``start_new_session=True``, so its process
        group id equals its pid. Signalling only the direct child would leave
        any grandchild it spawned alive and still holding the stdout pipe, so
        the reader thread would never see end-of-stream.
        """
        pid = getattr(self._proc, "pid", None)
        if not isinstance(pid, int) or pid <= 1:
            self._proc.terminate()
            return
        try:
            own_group = os.getpgrp()
        except OSError:  # pragma: no cover - getpgrp rarely fails
            own_group = -1
        if own_group != pid:
            try:
                os.killpg(pid, signal.SIGTERM)
                return
            except (OSError, ProcessLookupError):
                pass
        self._proc.terminate()
