"""Tests for the ACP adoption shim: fail-closed gate, capabilities, no close.

Every test uses an in-process upstream double and throwaway session and socket
directories. Nothing here starts a daemon or touches a real install.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from jcode_bridge.acp_shim import (  # noqa: E402
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MODE_READ_ONLY,
    PARSE_ERROR,
    SHIM_PRECONDITION_FAILED,
    AdoptionShim,
    BlockedUpstreamCall,
    InProcessUpstream,
    RpcError,
    ShimConfig,
)

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcGVyYXRvciJ9.c2lnbmF0dXJlc2ln"


def _short_root() -> str:
    """A short temp root: a unix socket path is capped at ~104 bytes."""
    if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK):
        return "/tmp"
    return tempfile.gettempdir()


class ShimCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="acpshim-", dir=_short_root()))
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.socket_dir = self.root / "sock"
        self.socket_dir.mkdir()
        self.socket_path = self.socket_dir / "jcode.sock"
        self._listeners: List[socket.socket] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for listener in self._listeners:
            listener.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def write_session(self, session_id: str = "sess-001") -> None:
        payload = {
            "id": session_id,
            "working_dir": "/tmp/project",
            "title": "Demo session",
            "updated_at": "2026-10-29T00:00:00Z",
        }
        path = self.sessions / (session_id + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")

    def listen(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(4)
        self._listeners.append(listener)
        return listener

    def shim(
        self,
        handler=None,
        upstream=None,
        **overrides: Any,
    ):
        transport = upstream if upstream is not None else InProcessUpstream(handler=handler)
        config = ShimConfig(
            session_dirs=(str(self.sessions),),
            socket_paths=(str(self.socket_path),),
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return AdoptionShim(config=config, upstream_factory=lambda _cfg: transport), transport

    def call(self, shim: AdoptionShim, method: str, params=None, request_id: int = 1):
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        payload = shim.handle_message(message)
        self.assertIsNotNone(payload)
        return payload


class TestCapabilities(ShimCase):
    def test_initialize_advertises_load_session_and_read_only_mode(self) -> None:
        shim, _ = self.shim()
        result = self.call(shim, "initialize", {"protocolVersion": 1})["result"]
        caps = result["agentCapabilities"]
        self.assertTrue(caps["loadSession"])
        self.assertEqual(caps["currentModeId"], MODE_READ_ONLY)
        self.assertEqual([mode["id"] for mode in caps["modes"]], [MODE_READ_ONLY])
        self.assertIn("list", caps["sessionCapabilities"])
        self.assertNotIn("close", caps["sessionCapabilities"])
        meta = caps["_meta"]["jcode.acpShim"]
        self.assertFalse(meta["spawnsDaemon"])
        self.assertFalse(meta["forwardsSessionClose"])
        self.assertEqual(result["protocolVersion"], 1)

    def test_initialize_preserves_upstream_capabilities(self) -> None:
        upstream = InProcessUpstream(
            initialize_result={
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {
                        "image": True,
                        "audio": False,
                        "embeddedContext": True,
                    },
                    "mcpCapabilities": {"http": False, "sse": False},
                    "sessionCapabilities": {"list": {}, "close": {}, "resume": {}},
                },
                "authMethods": [],
            }
        )
        shim, _ = self.shim(upstream=upstream)
        caps = self.call(shim, "initialize", {})["result"]["agentCapabilities"]
        self.assertEqual(
            caps["promptCapabilities"],
            {"image": True, "audio": False, "embeddedContext": True},
        )
        self.assertIn("resume", caps["sessionCapabilities"])
        self.assertNotIn("close", caps["sessionCapabilities"])
        self.assertTrue(caps["loadSession"])


class TestFailClosedAdoption(ShimCase):
    def test_unknown_session_fails_closed_and_never_calls_upstream(self) -> None:
        shim, upstream = self.shim()
        payload = self.call(shim, "session/load", {"sessionId": "nope", "cwd": "/tmp"})
        error = payload["error"]
        self.assertEqual(error["code"], SHIM_PRECONDITION_FAILED)
        self.assertEqual(error["data"]["precondition"], "unknown_session")
        self.assertFalse(error["data"]["willSpawnDaemon"])
        self.assertEqual(upstream.calls, [])

    def test_known_session_without_live_daemon_fails_closed(self) -> None:
        self.write_session()
        shim, upstream = self.shim()
        payload = self.call(shim, "session/load", {"sessionId": "sess-001"})
        error = payload["error"]
        self.assertEqual(error["code"], SHIM_PRECONDITION_FAILED)
        self.assertEqual(error["data"]["precondition"], "daemon_not_running")
        self.assertFalse(error["data"]["willSpawnDaemon"])
        self.assertEqual(upstream.calls, [])

    def test_unsafe_session_id_never_touches_the_filesystem(self) -> None:
        shim, upstream = self.shim()
        payload = self.call(shim, "session/load", {"sessionId": "../escape"})
        self.assertEqual(payload["error"]["data"]["precondition"], "invalid_session_id")
        self.assertEqual(upstream.calls, [])

    def test_adoptable_session_is_forwarded_as_session_load(self) -> None:
        self.write_session()
        self.listen()

        def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            if method == "session/load":
                return {"sessionId": params["sessionId"], "modes": []}
            return {}

        shim, upstream = self.shim(handler=handler)
        payload = self.call(shim, "session/load", {"sessionId": "sess-001"})
        self.assertNotIn("error", payload)
        self.assertEqual(payload["result"]["sessionId"], "sess-001")
        self.assertIn("session/load", upstream.calls)
        self.assertNotIn("session/new", upstream.calls)

    def test_prompt_before_load_is_refused_locally(self) -> None:
        shim, upstream = self.shim()
        payload = self.call(shim, "session/prompt", {"sessionId": "sess-001"})
        self.assertEqual(payload["error"]["code"], SHIM_PRECONDITION_FAILED)
        self.assertEqual(payload["error"]["data"]["precondition"], "session_not_loaded")
        self.assertEqual(upstream.calls, [])


class TestNeverClosesOrLeaks(ShimCase):
    def _adopted_shim(self):
        self.write_session()
        self.listen()

        def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            if method == "session/load":
                return {"sessionId": "sess-001"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            return {}

        return self.shim(handler=handler)

    def test_shim_never_sends_session_close_upstream(self) -> None:
        shim, upstream = self._adopted_shim()
        self.call(shim, "session/load", {"sessionId": "sess-001"})
        self.call(shim, "session/prompt", {"sessionId": "sess-001", "prompt": []})
        self.call(shim, "session/cancel", {"sessionId": "sess-001"})
        payload = self.call(shim, "session/close", {"sessionId": "sess-001"})
        self.assertFalse(payload["result"]["closed"])
        self.assertTrue(payload["result"]["detached"])
        self.assertNotIn("session/close", upstream.calls)
        self.assertNotIn("session/new", upstream.calls)

    def test_transport_blocks_forbidden_methods(self) -> None:
        upstream = InProcessUpstream()
        with self.assertRaises(BlockedUpstreamCall):
            upstream.request("session/close", {})
        with self.assertRaises(BlockedUpstreamCall):
            upstream.request("session/new", {})

    def test_errors_do_not_echo_credentials(self) -> None:
        shim, _ = self.shim()
        payload = self.call(shim, "session/load", {"sessionId": FAKE_JWT})
        rendered = json.dumps(payload)
        self.assertNotIn(FAKE_JWT, rendered)
        self.assertIn("<redacted>", rendered)


class TestModeHandling(ShimCase):
    def test_set_mode_is_local_by_default(self) -> None:
        shim, upstream = self.shim()
        payload = self.call(shim, "session/set_mode", {"modeId": MODE_READ_ONLY})
        self.assertEqual(payload["result"]["currentModeId"], MODE_READ_ONLY)
        self.assertEqual(upstream.calls, [])

    def test_set_mode_rejects_anything_but_read_only(self) -> None:
        shim, upstream = self.shim()
        payload = self.call(shim, "session/set_mode", {"modeId": "full-access"})
        self.assertEqual(payload["error"]["code"], INVALID_PARAMS)
        self.assertEqual(upstream.calls, [])

    def test_upstream_32601_for_set_mode_is_tolerated(self) -> None:
        def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            raise RpcError(METHOD_NOT_FOUND, "Unsupported ACP method: " + method)

        shim, upstream = self.shim(handler=handler, forward_mode=True)
        payload = self.call(shim, "session/set_mode", {"modeId": MODE_READ_ONLY})
        self.assertNotIn("error", payload)
        meta = payload["result"]["_meta"]["jcode"]["acpShim"]
        self.assertTrue(meta["degraded"])
        self.assertIn("session/set_mode", upstream.calls)

    def test_write_config_option_is_refused(self) -> None:
        shim, _ = self.shim()
        payload = self.call(
            shim, "session/set_config_option", {"configId": "writes", "value": True}
        )
        self.assertEqual(payload["error"]["code"], INVALID_PARAMS)


class TestStreamAndListing(ShimCase):
    def test_unknown_method_returns_method_not_found(self) -> None:
        shim, _ = self.shim()
        payload = self.call(shim, "session/teleport")
        self.assertEqual(payload["error"]["code"], METHOD_NOT_FOUND)

    def test_session_list_reads_files_only(self) -> None:
        self.write_session("sess-001")
        self.write_session("sess-002")
        shim, upstream = self.shim()
        payload = self.call(shim, "session/list", {})
        ids = [item["sessionId"] for item in payload["result"]["sessions"]]
        self.assertEqual(ids, ["sess-001", "sess-002"])
        self.assertEqual(upstream.calls, [])

    def test_serve_loop_survives_a_parse_error(self) -> None:
        shim, upstream = self.shim()
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "{not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/teleport"}),
        ]
        reader = io.StringIO("\n".join(lines) + "\n")
        writer = io.StringIO()
        served = shim.serve(reader=reader, writer=writer)
        self.assertEqual(served, 2)
        out = [json.loads(line) for line in writer.getvalue().splitlines()]
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["error"]["code"], PARSE_ERROR)
        self.assertEqual(out[2]["error"]["code"], METHOD_NOT_FOUND)
        self.assertTrue(upstream.closed)


if __name__ == "__main__":
    unittest.main()
