"""Tests for the daemon-API wiring in the CLI and MCP surfaces.

The harness-API client speaks a real protocol to a real ``jcode`` process, so
these tests never spawn one. Instead the backend is constructed with an injected
transport factory that replays canned frames, which keeps the tests fast and
makes the wire contract assertable.

Three things are covered:

* the MCP tool surface advertises the daemon tools and dispatches to them;
* the CLI exposes the same operations and reports the result envelope;
* closing a subprocess transport signals the child's whole process group, so a
  grandchild holding the stdout pipe cannot outlive the bridge.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from jcode_bridge import cli, mcp_server  # noqa: E402
from jcode_bridge.api import JcodeApiBackend  # noqa: E402
from jcode_bridge.api_transport import (  # noqa: E402
    ApiProtocolError,
    ApiRemoteError,
    ApiTimeout,
    ApiTransportError,
    SubprocessTransport,
)


def _frame(**fields: Any) -> str:
    """Serialize a server frame the way the wire would deliver it."""
    base: Dict[str, Any] = {"v": 1}
    base.update(fields)
    return json.dumps(base, separators=(",", ":"))


class ScriptedTransport:
    """A transport that replays canned frames and records what was written."""

    def __init__(self, replies: List[Optional[str]]) -> None:
        self.sent: List[str] = []
        self._replies = list(replies)
        self.closed = False

    def send_line(self, line: str) -> None:
        self.sent.append(line)

    def read_line(self, timeout: Optional[float]) -> Optional[str]:
        if not self._replies:
            raise TimeoutError("no frame within {}s".format(timeout))
        return self._replies.pop(0)

    def close(self) -> None:
        self.closed = True

    def stderr_tail(self, limit: int = 20000) -> str:
        return ""

    def sent_frames(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.sent]


def _hello_ok(request_id: int = 1) -> str:
    return _frame(
        ev="hello_ok",
        reply_to=request_id,
        version=1,
        server="jcode-harness-api-bridge/0.1.0",
        capabilities=["sessions", "streaming"],
    )


class ApiBackendWiringTests(unittest.TestCase):
    """The backend talks to the real protocol through an injected transport."""

    def setUp(self) -> None:
        mcp_server.reset_backend()

    def test_health_handshakes_and_pings(self) -> None:
        transport = ScriptedTransport([_hello_ok(1), _frame(ev="pong", reply_to=2)])
        backend = JcodeApiBackend(
            binary="/nonexistent/jcode",
            transport_factory=lambda argv, timeout: transport,
        )

        result = backend.health(timeout=5.0)

        self.assertTrue(result.ok, result.detail)
        self.assertTrue(result.data["ping"])
        self.assertEqual(result.data["server"], "jcode-harness-api-bridge/0.1.0")
        requests = [frame["req"] for frame in transport.sent_frames()]
        self.assertEqual(requests, ["hello", "ping"])

    def test_a_remote_error_becomes_a_failed_result(self) -> None:
        transport = ScriptedTransport(
            [_hello_ok(1), _frame(ev="error", reply_to=2, code="nope", message="denied")]
        )
        backend = JcodeApiBackend(
            binary="/nonexistent/jcode",
            transport_factory=lambda argv, timeout: transport,
        )

        result = backend.health(timeout=5.0)

        self.assertFalse(result.ok)
        self.assertIn("denied", result.detail)

    def test_a_missing_binary_is_reported_as_not_installed(self) -> None:
        # The binary is pinned to a path that cannot exist, so the resolution
        # never falls back to PATH and no real process is started.
        backend = JcodeApiBackend(binary="/nonexistent/jcode-not-here")

        result = backend.health(timeout=5.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "NOT_INSTALLED")

    def test_an_empty_prompt_is_rejected_before_any_process_is_spawned(self) -> None:
        def explode(argv: List[str], timeout: float) -> Any:
            raise AssertionError("no transport should be created for an invalid prompt")

        backend = JcodeApiBackend(binary="/nonexistent/jcode", transport_factory=explode)

        result = backend.ask("   ")

        self.assertFalse(result.ok)
        self.assertIn("prompt", result.detail)

    def test_a_nul_byte_in_the_prompt_is_rejected(self) -> None:
        backend = JcodeApiBackend(
            binary="/nonexistent/jcode",
            transport_factory=lambda argv, timeout: ScriptedTransport([]),
        )

        result = backend.ask("hello\x00world")

        self.assertFalse(result.ok)
        self.assertIn("NUL", result.detail)


class McpSurfaceTests(unittest.TestCase):
    """The MCP server advertises and dispatches the daemon tools."""

    def setUp(self) -> None:
        mcp_server.reset_backend()
        self.transport = ScriptedTransport([_hello_ok(1), _frame(ev="pong", reply_to=2)])
        mcp_server._API_BACKEND = JcodeApiBackend(
            binary="/nonexistent/jcode",
            transport_factory=lambda argv, timeout: self.transport,
        )

    def tearDown(self) -> None:
        mcp_server.reset_backend()

    def test_the_daemon_tools_are_advertised(self) -> None:
        names = {tool["name"] for tool in mcp_server.build_tools()}

        self.assertIn("jcode_api_health", names)
        self.assertIn("jcode_api_ask", names)
        self.assertIn("jcode_api_sessions", names)

    def test_the_existing_tool_surface_is_unchanged(self) -> None:
        names = {tool["name"] for tool in mcp_server.build_tools()}

        for name in (
            "jcode_health",
            "jcode_run",
            "jcode_task_start",
            "jcode_task_status",
            "jcode_task_output",
            "jcode_task_cancel",
            "jcode_task_list",
        ):
            self.assertIn(name, names)

    def test_jcode_api_health_dispatches_to_the_daemon_backend(self) -> None:
        result = mcp_server.call_tool("jcode_api_health", {})

        self.assertTrue(result.ok, result.detail)
        self.assertEqual([frame["req"] for frame in self.transport.sent_frames()],
                         ["hello", "ping"])

    def test_jcode_api_ask_requires_a_prompt(self) -> None:
        result = mcp_server.call_tool("jcode_api_ask", {"prompt": ""})

        self.assertFalse(result.ok)

    def test_an_unknown_tool_is_rejected(self) -> None:
        result = mcp_server.call_tool("jcode_api_nope", {})

        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.detail)

    def test_a_bad_timeout_is_named_rather_than_dropped(self) -> None:
        result = mcp_server.call_tool("jcode_api_ask", {"prompt": "hi", "timeout_secs": -1})

        self.assertFalse(result.ok)
        self.assertIn("timeout_secs", result.detail)


class CliSurfaceTests(unittest.TestCase):
    """The CLI exposes the daemon operations as subcommands."""

    def test_the_daemon_subcommands_exist(self) -> None:
        parser = cli._build_parser()

        args = parser.parse_args(["api-health"])
        self.assertEqual(args.command, "api-health")

        args = parser.parse_args(["api-ask", "hello"])
        self.assertEqual(args.command, "api-ask")
        self.assertEqual(args.prompt, "hello")
        self.assertFalse(args.keep_session)

        args = parser.parse_args(["api-sessions", "--limit", "5"])
        self.assertEqual(args.command, "api-sessions")
        self.assertEqual(args.limit, 5)

    def test_the_existing_subcommands_still_parse(self) -> None:
        parser = cli._build_parser()

        self.assertEqual(parser.parse_args(["health"]).command, "health")
        self.assertEqual(parser.parse_args(["run", "hi"]).command, "run")


class ProcessGroupTests(unittest.TestCase):
    """Closing a transport must not leave the child's descendants running."""

    def test_a_wedged_child_group_is_signalled_not_just_the_child(self) -> None:
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "sys.stdout.write(str(child.pid) + '\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "wedged.py")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(script)

            transport = SubprocessTransport(
                [sys.executable, script_path], close_grace=0.5
            )
            try:
                line = transport.read_line(timeout=10.0)
                self.assertIsNotNone(line)
                grandchild_pid = int(str(line).strip())
                self.assertTrue(_pid_alive(grandchild_pid))

                transport.close()

                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and _pid_alive(grandchild_pid):
                    time.sleep(0.1)
                self.assertFalse(
                    _pid_alive(grandchild_pid),
                    "the grandchild outlived the closed transport",
                )
            finally:
                _kill_pid(grandchild_pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if not _pid_alive(pid):
            return


if __name__ == "__main__":
    unittest.main()
