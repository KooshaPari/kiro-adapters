"""Inventory-only MCP server mode: only the four read-only tools are exposed
and every dispatch/ask/spawn tool is rejected, not merely hidden."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters import mcp_server as M  # noqa: E402

DISPATCH_TOOLS = [
    "kirocrew_status", "kirocrew_agents", "kirocrew_ask", "kirocrew_spawn",
    "kirocrew_spawn_list", "kirocrew_run_spec", "kirocrew_dashboard_url",
    "kirocode_status", "kirocode_ask", "kiro_dispatch",
]
INVENTORY_TOOLS = [
    "agent_sessions_list", "agent_session_get",
    "agent_session_history", "agent_sources",
]


def _call(mode, name, arguments=None):
    response = M.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        mode=mode,
    )
    if response is None:
        return None, None
    if "error" in response:
        return True, {"ok": False, "error": response["error"]["message"]}
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return result["isError"], payload


class InventoryModeTests(unittest.TestCase):
    def test_tools_list_only_inventory_tools(self):
        for mode in ("inventory", "inventory-only", "read-only", "readonly"):
            tools = M.build_tools(mode=mode)
            names = [t["name"] for t in tools]
            self.assertEqual(names, INVENTORY_TOOLS, msg=mode)
            for forbidden in DISPATCH_TOOLS:
                self.assertNotIn(forbidden, names, msg=mode)

    def test_tools_list_full_mode_unchanged(self):
        tools = M.build_tools(mode="full")
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), 14)
        for name in DISPATCH_TOOLS + INVENTORY_TOOLS:
            self.assertIn(name, names)

    def test_inventory_tools_callable_in_inventory_mode(self):
        is_error, payload = _call("inventory", "agent_sources", {})
        self.assertFalse(is_error)
        self.assertTrue(payload["ok"])

    def test_dispatch_tool_rejected_at_tools_call(self):
        for forbidden in DISPATCH_TOOLS:
            is_error, payload = _call(
                "inventory", forbidden, {"prompt": "x", "message": "x", "harness": "kirocrew"}
            )
            self.assertTrue(is_error, msg=forbidden)
            # Either a protocol-level rejection or an envelope failure counts.
            self.assertFalse(payload.get("ok", False) if isinstance(payload, dict) else True)

    def test_dispatch_tool_rejected_by_call_tool(self):
        for forbidden in DISPATCH_TOOLS:
            result = M.call_tool(forbidden, {"prompt": "x"}, mode="inventory")
            self.assertFalse(result.ok, msg=forbidden)

    def test_unknown_tool_still_unknown(self):
        is_error, _ = _call("inventory", "does_not_exist", {})
        self.assertTrue(is_error)

    def test_full_mode_still_dispatches(self):
        result = M.call_tool("kiro_dispatch", {"harness": "bogus", "prompt": "hi"}, mode="full")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "UNSUPPORTED")

    def test_coerce_mode_spelling(self):
        self.assertEqual(M._coerce_mode("inventory_only"), "inventory")
        self.assertEqual(M._coerce_mode("read only"), "inventory")
        self.assertEqual(M._coerce_mode(True), "inventory")
        self.assertEqual(M._coerce_mode("full"), "full")
        self.assertEqual(M._coerce_mode(False), "full")


if __name__ == "__main__":
    unittest.main()
