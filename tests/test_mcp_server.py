"""Tests for the zero-dependency MCP stdio server (protocol surface only)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters import mcp_server as M  # noqa: E402

TOOL_NAMES = [
    "kirocrew_status",
    "kirocrew_agents",
    "kirocrew_ask",
    "kirocrew_spawn",
    "kirocrew_spawn_list",
    "kirocrew_run_spec",
    "kirocrew_dashboard_url",
    "kirocode_status",
    "kirocode_ask",
    "kiro_dispatch",
    "agent_sessions_list",
    "agent_session_get",
    "agent_session_history",
    "agent_sources",
]


class InitializeTests(unittest.TestCase):
    def test_initialize(self):
        response = M.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], M.PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "kiro-adapters")
        self.assertEqual(result["capabilities"], {"tools": {}})

    def test_notification_gets_no_response(self):
        self.assertIsNone(
            M.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_ping(self):
        response = M.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response["result"], {})


class ToolListingTests(unittest.TestCase):
    def test_all_tools_are_advertised(self):
        tools = M.build_tools()
        self.assertEqual([tool["name"] for tool in tools], TOOL_NAMES)

    def test_schemas_are_closed_objects(self):
        for tool in M.build_tools():
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertIsInstance(schema["properties"], dict)
            self.assertIsInstance(schema["required"], list)
            self.assertTrue(tool["description"])

    def test_tools_list_method(self):
        response = M.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(names, TOOL_NAMES)

    def test_required_properties(self):
        by_name = {tool["name"]: tool for tool in M.build_tools()}
        self.assertEqual(by_name["kirocrew_ask"]["inputSchema"]["required"], ["message"])
        self.assertEqual(by_name["kirocrew_spawn"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(
            by_name["kirocrew_run_spec"]["inputSchema"]["required"], ["spec_path"]
        )
        self.assertEqual(by_name["kirocode_ask"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(
            by_name["kiro_dispatch"]["inputSchema"]["required"], ["harness", "prompt"]
        )
        harness = by_name["kiro_dispatch"]["inputSchema"]["properties"]["harness"]
        self.assertEqual(harness["enum"], ["kirocrew", "kirocode"])


class ErrorPathTests(unittest.TestCase):
    def test_unknown_method(self):
        response = M.handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_missing_method(self):
        response = M.handle_message({"jsonrpc": "2.0", "id": 5})
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_object_message(self):
        response = M.handle_message(["not", "a", "message"])
        self.assertEqual(response["error"]["code"], -32600)

    def test_tools_call_without_params(self):
        response = M.handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call"})
        self.assertEqual(response["error"]["code"], -32602)

    def test_tools_call_unknown_tool(self):
        response = M.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            }
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_call_tool_unknown_returns_failure_not_raise(self):
        result = M.call_tool("does_not_exist", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.ERROR)


class DispatchRoutingTests(unittest.TestCase):
    def test_unknown_harness_is_unsupported(self):
        result = M.call_tool("kiro_dispatch", {"harness": "bogus", "prompt": "hi"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
