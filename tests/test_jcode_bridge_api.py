"""Fake-transport tests for the jcode harness-API client.

``HarnessApiClient`` drives a versioned, newline-delimited JSON protocol over an
injected :class:`~jcode_bridge.api_transport.Transport`. These tests exercise the
two connection-level primitives, ``handshake`` (the ``hello`` / ``hello_ok``
version negotiation that MUST be the first frame on the wire) and ``ping`` (the
``ping`` / ``pong`` liveness check), against a scripted fake transport.

Because the transport is injected, no real ``jcode`` process is spawned: every
byte in and out is a literal the test controls. The fake records every frame the
client writes so the wire contract (``v`` / ``id`` / ``req``, monotonic request
ids, ``hello`` before anything else) can be asserted directly, and it replays a
canned server reply so each success and each protocol failure is deterministic.

Nothing here touches a subprocess, a socket, a task directory or a live config.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from jcode_bridge.api_client import (  # noqa: E402
    API_MAJOR,
    CLIENT_NAME,
    HarnessApiClient,
)
from jcode_bridge.api_transport import (  # noqa: E402
    ApiProtocolError,
    ApiRemoteError,
    ApiTimeout,
    ApiTransportError,
)


class FakeTransport:
    """A scripted :class:`Transport` that records writes and replays replies.

    ``replies`` is consumed one line per ``read_line`` call. Reading past the end
    of ``replies`` behaves like a hung peer and raises ``TimeoutError`` (the same
    signal the real transport uses for a deadline), so a test can prove the
    client maps a stall onto :class:`ApiTimeout` instead of blocking forever.
    """

    def __init__(
        self, replies: Optional[List[Optional[str]]] = None, closed: bool = False
    ) -> None:
        self.sent: List[str] = []
        self._replies: List[Optional[str]] = list(replies or [])
        self._closed = closed
        self.close_calls = 0

    # -- what the client does to us --------------------------------------
    def send_line(self, line: str) -> None:
        if self._closed:
            raise ApiTransportError("jcode api stream is closed")
        self.sent.append(line)

    def read_line(self, timeout: Optional[float]) -> Optional[str]:
        if not self._replies:
            # Exhausted script: emulate a peer that never answers in time.
            raise TimeoutError("no frame within {}s".format(timeout))
        return self._replies.pop(0)

    def close(self) -> None:
        self._closed = True
        self.close_calls += 1

    def stderr_tail(self, limit: int = 20000) -> str:
        return ""

    # -- assertion helpers ------------------------------------------------
    def sent_frames(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.sent]


def _frame(**fields: Any) -> str:
    """Serialize a server frame the way the wire would deliver it."""
    base: Dict[str, Any] = {"v": API_MAJOR}
    base.update(fields)
    return json.dumps(base, separators=(",", ":"))


class HandshakeTests(unittest.TestCase):
    """``hello`` must go first and be answered by a matching ``hello_ok``."""

    def test_hello_is_the_first_frame_and_carries_identity(self) -> None:
        transport = FakeTransport(
            replies=[
                _frame(
                    ev="hello_ok",
                    reply_to=1,
                    version=API_MAJOR,
                    server="jcode",
                    capabilities=["sessions", "streaming"],
                )
            ]
        )
        client = HarnessApiClient(transport)
        info = client.handshake()

        frames = transport.sent_frames()
        self.assertEqual(len(frames), 1, "handshake sends exactly one frame")
        hello = frames[0]
        self.assertEqual(hello["req"], "hello")
        self.assertEqual(hello["v"], API_MAJOR)
        self.assertEqual(hello["id"], 1)
        self.assertEqual(hello["min_version"], API_MAJOR)
        self.assertEqual(hello["max_version"], API_MAJOR)
        self.assertEqual(hello["client"], CLIENT_NAME)
        self.assertEqual(
            info,
            {
                "version": API_MAJOR,
                "server": "jcode",
                "capabilities": ["sessions", "streaming"],
            },
        )
        self.assertEqual(client.version, API_MAJOR)
        self.assertEqual(client.server, "jcode")
        self.assertEqual(client.capabilities, ["sessions", "streaming"])
        self.assertEqual(client.server_frames, 1)

    def test_handshake_tolerates_unknown_fields_and_non_string_capabilities(self) -> None:
        # The contract says clients ignore unknown fields; a server that adds a
        # field or mixes a non-string into capabilities must not break us.
        transport = FakeTransport(
            replies=[
                _frame(
                    ev="hello_ok",
                    reply_to=1,
                    version=API_MAJOR,
                    server="jcode",
                    future_field={"nested": True},
                    capabilities=["sessions", 42, None, "streaming"],
                )
            ]
        )
        client = HarnessApiClient(transport)
        info = client.handshake()
        # Non-string capability entries are dropped, strings kept, order kept.
        self.assertEqual(info["capabilities"], ["sessions", "streaming"])
        self.assertNotIn("future_field", info)

    def test_non_hello_ok_reply_is_a_protocol_error(self) -> None:
        transport = FakeTransport(replies=[_frame(ev="pong", reply_to=1)])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.handshake()
        self.assertIn("expected hello_ok", str(ctx.exception))

    def test_negotiated_version_mismatch_is_a_protocol_error(self) -> None:
        transport = FakeTransport(
            replies=[_frame(ev="hello_ok", reply_to=1, version=99, server="jcode")]
        )
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.handshake()
        self.assertIn("negotiated version", str(ctx.exception))

    def test_wrong_reply_to_is_not_treated_as_the_handshake_reply(self) -> None:
        # A frame addressed to a different request id must be queued aside, not
        # mistaken for the hello reply; the client then stalls waiting for the
        # real reply and raises ApiTimeout once the script is exhausted.
        transport = FakeTransport(
            replies=[
                _frame(ev="hello_ok", reply_to=77, version=API_MAJOR, server="jcode")
            ]
        )
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiTimeout):
            client.handshake()
        # The stray frame was preserved for a later collect_turn, not dropped.
        self.assertEqual(len(client._pending), 1)
        self.assertEqual(client._pending[0]["ev"], "hello_ok")

    def test_remote_error_reply_surfaces_code_and_message(self) -> None:
        transport = FakeTransport(
            replies=[_frame(ev="error", reply_to=1, code="bad_hello", message="nope")]
        )
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiRemoteError) as ctx:
            client.handshake()
        self.assertEqual(ctx.exception.code, "bad_hello")
        self.assertEqual(ctx.exception.message, "nope")

    def test_stream_closed_before_reply_is_a_transport_error(self) -> None:
        transport = FakeTransport(replies=[None])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiTransportError) as ctx:
            client.handshake()
        self.assertIn("stream closed", str(ctx.exception))


class PingTests(unittest.TestCase):
    """``ping`` is answered by ``pong``; anything else is a protocol error."""

    def test_ping_sends_ping_and_accepts_pong(self) -> None:
        transport = FakeTransport(replies=[_frame(ev="pong", reply_to=1)])
        client = HarnessApiClient(transport)
        client.ping()

        frames = transport.sent_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["req"], "ping")
        self.assertEqual(frames[0]["v"], API_MAJOR)
        self.assertEqual(frames[0]["id"], 1)

    def test_request_ids_are_monotonic_across_calls(self) -> None:
        transport = FakeTransport(
            replies=[_frame(ev="pong", reply_to=1), _frame(ev="pong", reply_to=2)]
        )
        client = HarnessApiClient(transport)
        client.ping()
        client.ping()
        self.assertEqual([f["id"] for f in transport.sent_frames()], [1, 2])

    def test_non_pong_reply_is_a_protocol_error(self) -> None:
        transport = FakeTransport(replies=[_frame(ev="hello_ok", reply_to=1)])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.ping()
        self.assertIn("expected pong", str(ctx.exception))


class FrameValidationTests(unittest.TestCase):
    """The shared read path rejects malformed frames before the state machine."""

    def test_streamed_event_is_deferred_then_ping_reply_is_returned(self) -> None:
        # A streaming event (no reply_to match) interleaves ahead of the pong;
        # it must be queued, not mis-consumed, so ping still returns cleanly.
        transport = FakeTransport(
            replies=[
                _frame(ev="text_delta", text="hi"),
                _frame(ev="pong", reply_to=1),
            ]
        )
        client = HarnessApiClient(transport)
        client.ping()
        self.assertEqual(len(client._pending), 1)
        self.assertEqual(client._pending[0]["ev"], "text_delta")
        self.assertEqual(client.server_frames, 2)

    def test_invalid_json_is_a_protocol_error(self) -> None:
        transport = FakeTransport(replies=["{not json"])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.ping()
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_wrong_protocol_version_is_a_protocol_error(self) -> None:
        transport = FakeTransport(replies=[json.dumps({"v": 2, "ev": "pong"})])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.ping()
        self.assertIn("unsupported harness API version", str(ctx.exception))

    def test_non_object_frame_is_a_protocol_error(self) -> None:
        transport = FakeTransport(replies=[json.dumps([1, 2, 3])])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiProtocolError) as ctx:
            client.ping()
        self.assertIn("not a JSON object", str(ctx.exception))

    def test_stalled_peer_raises_timeout_rather_than_blocking(self) -> None:
        # An empty reply script means the peer never answers; the fake raises
        # TimeoutError and the client must translate that to ApiTimeout.
        transport = FakeTransport(replies=[])
        client = HarnessApiClient(transport)
        with self.assertRaises(ApiTimeout):
            client.ping(timeout=0.01)


class SendMessageNotifyTests(unittest.TestCase):
    """``send_message`` is a notify: it has no reply frame.

    The daemon acknowledges the message with a ``message_accepted`` *event*,
    not a reply. A client that waits for a reply to ``send_message`` blocks
    until the turn times out, which is exactly the bug this class pins down.
    """

    def test_send_message_returns_without_waiting_for_a_reply(self) -> None:
        transport = FakeTransport(replies=[])
        client = HarnessApiClient(transport)

        result = client.send_message("s1", "hello")

        self.assertEqual(result, {})
        frames = transport.sent_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["req"], "send_message")
        self.assertEqual(frames[0]["session_id"], "s1")
        self.assertEqual(frames[0]["content"], "hello")

    def test_a_turn_collects_the_answer_and_the_acceptance(self) -> None:
        transport = FakeTransport(
            replies=[
                _frame(ev="message_accepted", session_id="s1"),
                _frame(ev="text_delta", session_id="s1", text="hello "),
                _frame(ev="text_delta", session_id="s1", text="world"),
                _frame(ev="turn_done", session_id="s1"),
            ]
        )
        client = HarnessApiClient(transport)

        outcome = client.collect_turn("s1")

        self.assertTrue(outcome.done)
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.text, "hello world")
        self.assertEqual(outcome.events[0], "message_accepted")

    def test_an_event_for_another_session_is_ignored(self) -> None:
        transport = FakeTransport(
            replies=[
                _frame(ev="text_delta", session_id="other", text="not mine"),
                _frame(ev="text_delta", session_id="s1", text="mine"),
                _frame(ev="turn_done", session_id="s1"),
            ]
        )
        client = HarnessApiClient(transport)

        outcome = client.collect_turn("s1")

        self.assertEqual(outcome.text, "mine")
        self.assertEqual(outcome.ignored_events, 1)


class ListSessionsLimitTests(unittest.TestCase):
    """The bridge enforces its own session bound.

    The released daemon (v0.99.1) ignores the ``limit`` field and returns every
    session, so a client that trusted the server would hand back more than the
    caller asked for.
    """

    def _reply(self, count: int) -> str:
        sessions = [
            {
                "session_id": "s{}".format(index),
                "updated_at_ms": 1000 + index,
            }
            for index in range(count)
        ]
        return _frame(ev="sessions", reply_to=1, sessions=sessions)

    def test_the_limit_is_applied_even_when_the_server_ignores_it(self) -> None:
        transport = FakeTransport(replies=[self._reply(10)])
        client = HarnessApiClient(transport)

        sessions = client.list_sessions(limit=3)

        self.assertEqual(len(sessions), 3)
        self.assertEqual(
            [item["session_id"] for item in sessions], ["s9", "s8", "s7"]
        )

    def test_the_limit_frame_is_still_sent_to_the_server(self) -> None:
        transport = FakeTransport(replies=[self._reply(2)])
        client = HarnessApiClient(transport)

        client.list_sessions(limit=5)

        frames = transport.sent_frames()
        self.assertEqual(frames[0]["req"], "list_sessions")
        self.assertEqual(frames[0]["limit"], 5)

    def test_a_short_list_is_returned_unchanged(self) -> None:
        transport = FakeTransport(replies=[self._reply(2)])
        client = HarnessApiClient(transport)

        sessions = client.list_sessions(limit=10)

        self.assertEqual(len(sessions), 2)

    def test_sessions_without_a_timestamp_sort_last(self) -> None:
        transport = FakeTransport(
            replies=[
                _frame(
                    ev="sessions",
                    reply_to=1,
                    sessions=[
                        {"session_id": "old"},
                        {"session_id": "new", "updated_at_ms": 5000},
                    ],
                )
            ]
        )
        client = HarnessApiClient(transport)

        sessions = client.list_sessions(limit=1)

        self.assertEqual([item["session_id"] for item in sessions], ["new"])


if __name__ == "__main__":
    unittest.main()
