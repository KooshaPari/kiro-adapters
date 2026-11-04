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
    SHIM_PROMPT_FORWARDING_DISABLED,
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
        self.assertTrue(meta["readOnly"])
        self.assertFalse(meta["promptForwarding"])
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


class TestPromptForwarding(ShimCase):
    """The shim must not offer a capability it cannot enforce.

    Adoption is read-only. The daemon's tool authority is not bridged, so a
    forwarded prompt would run with permissions this shim never checked.
    """

    def _adopted(self, **overrides: Any):
        self.write_session()
        self.listen()

        def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            if method == "session/load":
                return {"sessionId": params["sessionId"]}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            return {}

        shim, upstream = self.shim(handler=handler, **overrides)
        adopted = self.call(shim, "session/load", {"sessionId": "sess-001"})
        self.assertNotIn("error", adopted)
        return shim, upstream

    def test_prompt_refused_by_default_with_structured_error(self) -> None:
        shim, upstream = self._adopted()
        payload = self.call(
            shim, "session/prompt", {"sessionId": "sess-001", "prompt": []}
        )
        error = payload["error"]
        self.assertEqual(error["code"], SHIM_PROMPT_FORWARDING_DISABLED)
        self.assertEqual(error["data"]["precondition"], "prompt_forwarding_disabled")
        self.assertEqual(error["data"]["adoptionMode"], MODE_READ_ONLY)
        self.assertFalse(error["data"]["promptForwarding"])
        self.assertFalse(error["data"]["willSpawnDaemon"])
        self.assertEqual(error["data"]["optIn"], "--allow-prompt-forwarding")
        meta = error["data"]["_meta"]["jcode"]["acpShim"]
        self.assertTrue(meta["readOnly"])
        self.assertFalse(meta["promptForwarding"])
        self.assertFalse(meta["upstreamToolAuthorityEnforced"])
        self.assertNotIn("session/prompt", upstream.calls)

    def test_a_write_requesting_prompt_is_never_forwarded(self) -> None:
        shim, upstream = self._adopted()
        payload = self.call(
            shim,
            "session/prompt",
            {
                "sessionId": "sess-001",
                "prompt": [{"type": "text", "text": "write /etc/passwd"}],
            },
        )
        self.assertEqual(payload["error"]["code"], SHIM_PROMPT_FORWARDING_DISABLED)
        self.assertEqual(upstream.calls, ["session/load"])

    def test_prompt_forwarded_only_with_the_explicit_flag(self) -> None:
        shim, upstream = self._adopted(allow_prompt_forwarding=True)
        payload = self.call(
            shim, "session/prompt", {"sessionId": "sess-001", "prompt": []}
        )
        self.assertEqual(payload["result"]["stopReason"], "end_turn")
        self.assertIn("session/prompt", upstream.calls)
        self.assertNotIn("session/new", upstream.calls)
        self.assertNotIn("session/close", upstream.calls)

    def test_initialize_advertises_the_opt_in_honestly(self) -> None:
        shim, _ = self.shim()
        meta = self.call(shim, "initialize", {})["result"]["agentCapabilities"][
            "_meta"
        ]["jcode.acpShim"]
        self.assertTrue(meta["readOnly"])
        self.assertFalse(meta["promptForwarding"])

        opting_in, _ = self.shim(allow_prompt_forwarding=True)
        opt_meta = self.call(opting_in, "initialize", {})["result"][
            "agentCapabilities"
        ]["_meta"]["jcode.acpShim"]
        self.assertTrue(opt_meta["promptForwarding"])
        self.assertFalse(opt_meta["readOnly"])

    def test_cancel_stays_local_while_prompt_forwarding_is_disabled(self) -> None:
        shim, upstream = self._adopted()
        payload = self.call(shim, "session/cancel", {"sessionId": "sess-001"})
        self.assertFalse(payload["result"]["cancelled"])
        self.assertNotIn("session/cancel", upstream.calls)


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


class UpstreamArgvTests(unittest.TestCase):
    """The upstream argv must survive flag-like arguments.

    ``--upstream kcode --no-selfdev acp`` is how a real build is invoked:
    ``--no-selfdev`` stops the ACP front end from reloading the binary
    underneath the running session. With ``nargs="+"`` argparse tried to parse
    ``--no-selfdev`` as the shim's own flag and the process died with
    "unrecognized arguments" before it spoke a single ACP frame.
    """

    def _split(self, argv: List[str]):
        from jcode_bridge.acp_shim.__main__ import split_upstream

        return split_upstream(argv)

    def test_flag_like_arguments_reach_the_upstream_verbatim(self) -> None:
        _, upstream = self._split(["--upstream", "kcode", "--no-selfdev", "acp"])
        self.assertEqual(upstream, ["kcode", "--no-selfdev", "acp"])

    def test_the_shim_own_flags_still_parse_before_the_upstream(self) -> None:
        from jcode_bridge.acp_shim.__main__ import build_parser

        head, _ = self._split(
            ["--session-dir", "/tmp/x", "--timeout", "12",
             "--upstream", "kcode", "--no-selfdev", "acp"]
        )
        args = build_parser().parse_args(head)
        self.assertEqual(args.session_dir, ["/tmp/x"])
        self.assertEqual(args.timeout, 12.0)

    def test_an_explicit_separator_is_dropped(self) -> None:
        _, upstream = self._split(["--upstream", "--", "kcode", "acp"])
        self.assertEqual(upstream, ["kcode", "acp"])

    def test_the_default_upstream_is_used_when_none_is_given(self) -> None:
        _, upstream = self._split([])
        self.assertEqual(upstream, ["kcode", "acp"])

    def test_a_child_flag_never_becomes_a_shim_flag(self) -> None:
        # The child flag must not appear in the shim's own parsed namespace.
        from jcode_bridge.acp_shim.__main__ import build_parser

        head, _ = self._split(["--upstream", "kcode", "--no-selfdev", "acp"])
        args = build_parser().parse_args(head)
        self.assertFalse(hasattr(args, "no_selfdev"))


class TestDedicatedSessionAdoption(ShimCase):
    """Bounded, isolated adoption tests for one explicit dedicated session id.

    These use only the throwaway temp session dir, temp socket and an
    in-process upstream double from ``ShimCase``. They never touch a real
    user session store, a real daemon, or a live install.
    """

    DEDICATED = "sess-dedicated-42"

    def _capture_handler(self):
        seen: List[Tuple[str, Dict[str, Any]]] = []

        def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            seen.append((method, dict(params)))
            if method == "session/load":
                return {"sessionId": params["sessionId"], "modes": []}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            return {}

        return seen, handler

    def test_explicit_dedicated_session_id_is_forwarded_verbatim(self) -> None:
        self.write_session(self.DEDICATED)
        self.listen()
        seen, handler = self._capture_handler()
        shim, upstream = self.shim(handler=handler)

        payload = self.call(
            shim, "session/load", {"sessionId": self.DEDICATED, "cwd": "/tmp/project"}
        )

        self.assertNotIn("error", payload)
        self.assertEqual(payload["result"]["sessionId"], self.DEDICATED)
        self.assertEqual(len(seen), 1, "session/load must be forwarded exactly once")
        method, forwarded = seen[0]
        self.assertEqual(method, "session/load")
        self.assertEqual(
            forwarded.get("sessionId"), self.DEDICATED,
            "the dedicated session id must be forwarded verbatim",
        )
        self.assertNotIn("session/new", upstream.calls)

        close_payload = self.call(shim, "session/close", {"sessionId": self.DEDICATED})
        self.assertFalse(close_payload["result"]["closed"])
        self.assertTrue(close_payload["result"]["detached"])
        self.assertNotIn("session/close", upstream.calls)
        self.assertNotIn("session/new", upstream.calls)

    def test_missing_socket_fails_closed_for_dedicated_session(self) -> None:
        self.write_session(self.DEDICATED)
        shim, upstream = self.shim()

        payload = self.call(shim, "session/load", {"sessionId": self.DEDICATED})

        self.assertEqual(payload["error"]["data"]["precondition"], "daemon_not_running")
        self.assertEqual(upstream.calls, [])

    def test_unknown_dedicated_session_fails_closed(self) -> None:
        shim, upstream = self.shim()

        payload = self.call(shim, "session/load", {"sessionId": self.DEDICATED})

        self.assertEqual(payload["error"]["data"]["precondition"], "unknown_session")
        self.assertEqual(upstream.calls, [])

    def test_unsafe_dedicated_session_id_never_touches_filesystem(self) -> None:
        shim, upstream = self.shim()

        payload = self.call(shim, "session/load", {"sessionId": "../escape"})

        self.assertEqual(payload["error"]["data"]["precondition"], "invalid_session_id")
        self.assertEqual(upstream.calls, [])


if __name__ == "__main__":
    unittest.main()
