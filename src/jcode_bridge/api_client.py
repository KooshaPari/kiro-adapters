"""Client for jcode's stable, versioned harness API (protocol major version 1).

Wire contract, from the shipped implementation in ``crates/jcode-harness-api``
and verified against the installed ``jcode`` binary:

* Every frame is one JSON object on one line (NDJSON), maximum 16 MiB.
* Every frame carries ``v``, the protocol major version.
* A client frame is ``{"v": 1, "id": <u64>, "req": "<name>", ...}``.
* A server frame is ``{"v": 1, "reply_to": <u64>?, "ev": "<name>", ...}``.
  ``reply_to`` is present only on direct replies; streaming events are not
  tied to a request id.
* ``hello`` must be the first frame. It is answered with ``hello_ok``, which
  carries the server identity and a capability list.
* Clients must ignore unknown fields and skip unknown event kinds, so an
  additive server change cannot break an existing client.

The transport is injected, so the protocol state machine can be exercised
without a real ``jcode`` process. :class:`SubprocessTransport` is the
production one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .api_transport import (
    MAX_FRAME_BYTES,
    ApiError,
    ApiProtocolError,
    ApiRemoteError,
    ApiTimeout,
    ApiTransportError,
    Transport,
)

#: Protocol major version this client implements.
API_MAJOR = 1
#: Identity sent in the ``hello`` handshake.
CLIENT_NAME = "kiro-adapters-jcode-api/{}".format(__version__)

CONNECT_TIMEOUT = 60.0
PING_TIMEOUT = 60.0
SESSION_TIMEOUT = 120.0
TURN_TIMEOUT = 900.0
DETACH_TIMEOUT = 30.0


def _deadline(timeout: Optional[float], deadline: Optional[float]) -> Optional[float]:
    if deadline is not None:
        return deadline
    if timeout is None:
        return None
    return time.monotonic() + timeout


def _remaining(deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise ApiTimeout("timed out waiting for jcode api")
    return left


def _frame_text(frame: Dict[str, Any]) -> str:
    event = frame.get("ev")
    return event if isinstance(event, str) and event else "<unnamed>"


def _most_recent(sessions: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Return at most ``limit`` sessions, newest first.

    The daemon's own ``limit`` handling landed after the released binary this
    bridge is tested against (v0.99.1 ignores the field and returns every
    session). Enforcing the bound here keeps the bridge's contract the same
    on every daemon, instead of silently returning more than the caller asked
    for. Sessions without a usable timestamp keep their relative order and are
    treated as oldest.
    """
    if limit <= 0:
        return []
    if len(sessions) <= limit:
        return sessions

    def sort_key(item: Dict[str, Any]) -> int:
        for key in ("updated_at_ms", "last_active_at_ms"):
            value = item.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
        return -1

    ordered = sorted(
        enumerate(sessions), key=lambda pair: (-sort_key(pair[1]), pair[0])
    )
    return [item for _, item in ordered[:limit]]


@dataclass
class TurnOutcome:
    """Everything one ``send_message`` produced, in arrival order."""

    session_id: str
    text: str = ""
    done: bool = False
    accepted: bool = False
    events: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    token_usage: List[Dict[str, Any]] = field(default_factory=list)
    permission_requests: List[Dict[str, Any]] = field(default_factory=list)
    statuses: List[str] = field(default_factory=list)
    ignored_events: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "text": self.text,
            "done": self.done,
            "accepted": self.accepted,
            "events": list(self.events),
            "tool_calls": list(self.tool_calls),
            "token_usage": list(self.token_usage),
            "permission_requests": list(self.permission_requests),
            "statuses": list(self.statuses),
            "ignored_events": self.ignored_events,
        }


class HarnessApiClient:
    """A synchronous client for one harness-API connection."""

    def __init__(
        self,
        transport: Transport,
        client_name: str = CLIENT_NAME,
        max_version: int = API_MAJOR,
    ) -> None:
        self.transport = transport
        self.client_name = client_name
        self.max_version = max_version
        self.version: Optional[int] = None
        self.server: Optional[str] = None
        self.capabilities: List[str] = []
        self.server_frames: int = 0
        self._next_id = 0
        self._pending: List[Dict[str, Any]] = []

    # -- framing ----------------------------------------------------------
    def _send(self, request: Dict[str, Any]) -> int:
        self._next_id += 1
        frame: Dict[str, Any] = {"v": API_MAJOR, "id": self._next_id}
        frame.update(request)
        self.transport.send_line(json.dumps(frame, separators=(",", ":")))
        return self._next_id

    def _read(self, timeout: Optional[float]) -> Dict[str, Any]:
        try:
            line = self.transport.read_line(timeout)
        except TimeoutError as exc:
            raise ApiTimeout(str(exc) or "timed out waiting for jcode api")
        if line is None:
            raise ApiTransportError("jcode api stream closed before the reply arrived")
        if not line.strip():
            raise ApiProtocolError("jcode api sent an empty frame")
        if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ApiProtocolError(
                "jcode api frame exceeds {} bytes".format(MAX_FRAME_BYTES)
            )
        try:
            frame = json.loads(line)
        except ValueError as exc:
            raise ApiProtocolError("jcode api sent invalid JSON: {}".format(exc))
        if not isinstance(frame, dict):
            raise ApiProtocolError("jcode api frame is not a JSON object")
        version = frame.get("v")
        if version != API_MAJOR:
            raise ApiProtocolError(
                "unsupported harness API version {!r}, this client speaks {}".format(
                    version, API_MAJOR
                )
            )
        self.server_frames += 1
        return frame

    @staticmethod
    def _raise_on_error(frame: Dict[str, Any]) -> None:
        if frame.get("ev") == "error":
            message = frame.get("message")
            code = frame.get("code")
            raise ApiRemoteError(
                code if isinstance(code, str) else None,
                message if isinstance(message, str) else "unspecified error",
            )

    def request(
        self,
        request: Dict[str, Any],
        timeout: Optional[float] = 30.0,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Send ``request`` and return its reply, keeping streamed frames.

        Streaming events that arrive while the reply is outstanding are not
        lost: they are queued and handed to the next :meth:`collect_turn`.
        """
        request_id = self._send(request)
        wait_deadline = _deadline(timeout, deadline)
        while True:
            frame = self._read(_remaining(wait_deadline))
            if frame.get("reply_to") == request_id:
                self._raise_on_error(frame)
                return frame
            self._pending.append(frame)

    # -- handshake --------------------------------------------------------
    def handshake(self, timeout: Optional[float] = 30.0) -> Dict[str, Any]:
        """Negotiate the protocol version. Must be the first call."""
        frame = self.request(
            {
                "req": "hello",
                "min_version": API_MAJOR,
                "max_version": max(API_MAJOR, int(self.max_version)),
                "client": self.client_name,
            },
            timeout=timeout,
        )
        if frame.get("ev") != "hello_ok":
            raise ApiProtocolError("expected hello_ok, got {}".format(_frame_text(frame)))
        version = frame.get("version")
        if version != API_MAJOR:
            raise ApiProtocolError(
                "server negotiated version {!r}, this client speaks {}".format(
                    version, API_MAJOR
                )
            )
        self.version = version
        server = frame.get("server")
        self.server = server if isinstance(server, str) else None
        caps = frame.get("capabilities")
        self.capabilities = (
            [item for item in caps if isinstance(item, str)]
            if isinstance(caps, list)
            else []
        )
        return {
            "version": version,
            "server": self.server,
            "capabilities": list(self.capabilities),
        }

    def ping(self, timeout: Optional[float] = PING_TIMEOUT) -> None:
        frame = self.request({"req": "ping"}, timeout=timeout)
        if frame.get("ev") != "pong":
            raise ApiProtocolError("expected pong, got {}".format(_frame_text(frame)))

    # -- sessions ---------------------------------------------------------
    @staticmethod
    def _attached_session(frame: Dict[str, Any], action: str) -> Dict[str, Any]:
        if frame.get("ev") not in ("attached", "session_forked"):
            raise ApiProtocolError(
                "{} expected an attached session, got {}".format(
                    action, _frame_text(frame)
                )
            )
        session = frame.get("session")
        if not isinstance(session, dict):
            raise ApiProtocolError("{} reply has no session object".format(action))
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ApiProtocolError("{} reply has no session_id".format(action))
        return session

    def create_session(
        self,
        working_dir: Optional[str] = None,
        timeout: Optional[float] = SESSION_TIMEOUT,
    ) -> Dict[str, Any]:
        request: Dict[str, Any] = {"req": "create_session"}
        if working_dir:
            request["working_dir"] = working_dir
        return self._attached_session(
            self.request(request, timeout=timeout), "create_session"
        )

    def attach_session(
        self, session_id: str, timeout: Optional[float] = SESSION_TIMEOUT
    ) -> Dict[str, Any]:
        frame = self.request(
            {"req": "attach_session", "session_id": session_id}, timeout=timeout
        )
        return self._attached_session(frame, "attach_session")

    def detach_session(
        self, session_id: str, timeout: Optional[float] = DETACH_TIMEOUT
    ) -> bool:
        frame = self.request(
            {"req": "detach_session", "session_id": session_id}, timeout=timeout
        )
        return frame.get("ev") == "ok"

    def list_sessions(
        self,
        limit: Optional[int] = None,
        include_archived: bool = False,
        timeout: Optional[float] = CONNECT_TIMEOUT,
    ) -> List[Dict[str, Any]]:
        request: Dict[str, Any] = {"req": "list_sessions"}
        if include_archived:
            request["include_archived"] = True
        if limit is not None:
            request["limit"] = int(limit)
        frame = self.request(request, timeout=timeout)
        if frame.get("ev") != "sessions":
            raise ApiProtocolError("expected sessions, got {}".format(_frame_text(frame)))
        sessions = frame.get("sessions")
        if not isinstance(sessions, list):
            raise ApiProtocolError("sessions reply is not a list")
        items = [item for item in sessions if isinstance(item, dict)]
        if limit is not None:
            items = _most_recent(items, limit)
        return items

    def send_message(
        self, session_id: str, content: str, timeout: Optional[float] = PING_TIMEOUT
    ) -> Dict[str, Any]:
        """Send a user message to an attached session.

        ``send_message`` is a *notify*: the daemon does not answer it with a
        reply frame, so waiting for one would block until the turn timed out.
        The daemon instead emits a ``message_accepted`` event once the agent
        has the message queued. This method sends and returns immediately; the
        caller collects the turn (including ``message_accepted``) with
        :meth:`collect_turn`.
        """
        self._send(
            {
                "req": "send_message",
                "session_id": session_id,
                "content": content,
            }
        )
        return {}

    def permission_response(
        self, session_id: str, request_id: str, decision: str = "deny"
    ) -> None:
        """Answer a ``permission_request``. Sends only; the ack is ignored."""
        if decision not in ("allow", "allow_always", "deny"):
            raise ValueError("decision must be allow, allow_always or deny")
        self._send(
            {
                "req": "permission_response",
                "session_id": session_id,
                "request_id": request_id,
                "decision": decision,
            }
        )

    # -- streaming --------------------------------------------------------
    def collect_turn(
        self,
        session_id: str,
        timeout: Optional[float] = TURN_TIMEOUT,
        on_permission: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> TurnOutcome:
        """Read events until ``turn_done`` for ``session_id``.

        Unknown event kinds are counted and skipped, never raised: the protocol
        promises additive compatibility. A ``permission_request`` is recorded
        and, when ``on_permission`` is given, answered with its return value
        (``allow`` / ``allow_always`` / ``deny``); otherwise it is left for the
        caller to answer with :meth:`permission_response`, and the turn will
        normally stall until it times out. That is deliberate: silently
        granting a tool permission from a bridge would be a privilege decision
        the caller did not make.
        """
        outcome = TurnOutcome(session_id=session_id)
        deadline = _deadline(timeout, None)
        queued, self._pending = self._pending, []
        calls: Dict[str, Dict[str, Any]] = {}
        while True:
            frame = queued.pop(0) if queued else self._read(_remaining(deadline))
            self._raise_on_error(frame)
            event = frame.get("ev")
            if not isinstance(event, str) or not event:
                raise ApiProtocolError("jcode api frame has no ev")
            owner = frame.get("session_id")
            if owner is not None and owner != session_id:
                outcome.ignored_events += 1
                continue
            outcome.events.append(event)
            if event == "message_accepted":
                outcome.accepted = True
            elif event == "text_delta":
                text = frame.get("text")
                if isinstance(text, str):
                    outcome.text += text
            elif event == "tool_start":
                call_id = str(frame.get("call_id") or "")
                record: Dict[str, Any] = {
                    "call_id": call_id,
                    "name": frame.get("name"),
                    "input": "",
                    "output": "",
                }
                calls[call_id] = record
                outcome.tool_calls.append(record)
            elif event == "tool_input_delta":
                record = calls.get(str(frame.get("call_id") or ""))
                if record is not None:
                    delta = frame.get("delta")
                    if isinstance(delta, str):
                        record["input"] += delta
            elif event == "tool_done":
                record = calls.get(str(frame.get("call_id") or ""))
                if record is not None:
                    record["output"] = frame.get("output") or ""
                    if frame.get("error"):
                        record["error"] = frame.get("error")
            elif event == "token_usage":
                outcome.token_usage.append(
                    {
                        "input": frame.get("input"),
                        "output": frame.get("output"),
                        "cache_read_input": frame.get("cache_read_input"),
                        "cache_creation_input": frame.get("cache_creation_input"),
                    }
                )
            elif event == "permission_request":
                permission = {
                    "request_id": frame.get("request_id"),
                    "tool_name": frame.get("tool_name"),
                    "description": frame.get("description"),
                }
                outcome.permission_requests.append(permission)
                if on_permission is not None:
                    decision = on_permission(dict(permission))
                    if isinstance(decision, str) and decision:
                        self.permission_response(
                            session_id, str(permission["request_id"]), decision
                        )
            elif event == "session_status":
                status = frame.get("status")
                if isinstance(status, str):
                    outcome.statuses.append(status)
            elif event == "turn_done":
                outcome.done = True
                return outcome
            # Every other kind is additive: skip it.

    def run_turn(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        timeout: Optional[float] = TURN_TIMEOUT,
        keep_session: bool = False,
        on_permission: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> Tuple[TurnOutcome, Dict[str, Any]]:
        """Create a session, send one message, read the answer, detach."""
        deadline = _deadline(timeout, None)
        session = self.create_session(
            working_dir=working_dir, timeout=_remaining(deadline)
        )
        session_id = str(session["session_id"])
        self.send_message(session_id, prompt, timeout=_remaining(deadline))
        outcome = self.collect_turn(
            session_id, timeout=_remaining(deadline), on_permission=on_permission
        )
        if not keep_session:
            try:
                self.detach_session(session_id, timeout=DETACH_TIMEOUT)
            except ApiError:
                # A failed detach must not hide a completed turn: closing the
                # connection releases the session anyway.
                pass
        return outcome, session
