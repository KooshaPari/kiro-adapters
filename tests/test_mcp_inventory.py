"""The four read-only inventory MCP tools.

The tools are exercised through ``tools/call`` (the real protocol path), not by
calling the handlers directly, so the advertised schema, argument plumbing and
result rendering are all covered. Each test runs against a throwaway ``$HOME``
holding one jcode and one crew session.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters import mcp_server as M  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    SOURCE_JCODE_FS,
    SOURCE_KIROCREW_LEDGER,
)

JCODE_ID = "session_mcp_1700000000000_aaaaaaaaaaaaaaaa"
CREW_ID = "sess_mcp_1700000000000_bbbbbbbbbbbbbbbb"
FAKE_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"

INVENTORY_TOOLS = [
    "agent_sessions_list",
    "agent_session_get",
    "agent_session_history",
    "agent_sources",
]


def _write_stores(home: Path) -> None:
    jcode = home / ".jcode" / "sessions"
    jcode.mkdir(parents=True, exist_ok=True)
    (home / ".jcode" / "active_pids").mkdir(parents=True, exist_ok=True)
    (jcode / (JCODE_ID + ".json")).write_text(
        json.dumps(
            {
                "id": JCODE_ID,
                "title": "mcp fixture",
                "working_dir": "/tmp/mcp/repo",
                "status": "Active",
                "updated_at": "2026-10-20T00:05:00Z",
                "messages": [
                    {"id": "m1", "role": "user", "content": FAKE_SECRET},
                    {"id": "m2", "role": "assistant", "content": "ok"},
                ],
            }
        ),
        encoding="utf-8",
    )
    kiro = home / ".kiro"
    (kiro / "session-index").mkdir(parents=True, exist_ok=True)
    session_dir = kiro / "sessions" / "hash_m" / "sess_m"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sess_session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "id": CREW_ID}),
                json.dumps({"_type": "turn", "type": "assistant", "index": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (kiro / "session-index" / "workspace.jsonl").write_text(
        json.dumps({"op": "add", "sessionPath": "hash_m/sess_m", "at": 1700000000000})
        + "\n",
        encoding="utf-8",
    )


def _call(name, arguments):
    response = M.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return result["isError"], payload


class InventoryToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.temp_dir()
        self.home = self.tmp.__enter__()
        _write_stores(self.home)
        self._home = S.isolated_home(self.home)
        self._home.__enter__()

    def tearDown(self):
        self._home.__exit__(None, None, None)
        self.tmp.__exit__(None, None, None)


class SchemaTests(unittest.TestCase):
    def test_four_inventory_tools_are_advertised(self):
        names = [tool["name"] for tool in M.build_tools()]
        for name in INVENTORY_TOOLS:
            self.assertIn(name, names)

    def test_required_properties(self):
        by_name = {tool["name"]: tool for tool in M.build_tools()}
        self.assertEqual(
            by_name["agent_session_get"]["inputSchema"]["required"], ["session_uid"]
        )
        self.assertEqual(
            by_name["agent_session_history"]["inputSchema"]["required"],
            ["session_uid"],
        )
        self.assertEqual(by_name["agent_sessions_list"]["inputSchema"]["required"], [])
        self.assertEqual(by_name["agent_sources"]["inputSchema"]["required"], [])

    def test_list_arguments_are_typed(self):
        props = {
            tool["name"]: tool["inputSchema"]["properties"] for tool in M.build_tools()
        }
        self.assertEqual(props["agent_sessions_list"]["limit"]["type"], "integer")
        self.assertEqual(
            props["agent_sessions_list"]["include_stale"]["type"], "boolean"
        )


class ListAndSourcesTests(InventoryToolCase):
    def test_sessions_list_reports_both_stores(self):
        is_error, payload = _call("agent_sessions_list", {})
        self.assertFalse(is_error)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["harness"], "inventory")
        self.assertGreaterEqual(payload["data"]["count"], 2)
        sources = {s["source"] for s in payload["data"]["sessions"]}
        self.assertEqual(sources, {SOURCE_JCODE_FS, SOURCE_KIROCREW_LEDGER})

    def test_limit_is_honoured(self):
        is_error, payload = _call("agent_sessions_list", {"limit": 1})
        self.assertFalse(is_error)
        self.assertEqual(payload["data"]["count"], 1)

    def test_include_stale_false_is_accepted(self):
        is_error, payload = _call("agent_sessions_list", {"include_stale": False})
        self.assertFalse(is_error)
        self.assertFalse(payload["data"]["include_stale"])
        self.assertTrue(
            all(s["state"] != "stale" for s in payload["data"]["sessions"])
        )

    def test_sources_lists_every_store(self):
        is_error, payload = _call("agent_sources", {})
        self.assertFalse(is_error)
        self.assertEqual(payload["data"]["count"], 3)
        present = {s["source"]: s["present"] for s in payload["data"]["sources"]}
        self.assertTrue(present[SOURCE_JCODE_FS])
        self.assertTrue(present[SOURCE_KIROCREW_LEDGER])


class GetTests(InventoryToolCase):
    def test_get_returns_one_session(self):
        uid = "{}:{}".format(SOURCE_JCODE_FS, JCODE_ID)
        is_error, payload = _call("agent_session_get", {"session_uid": uid})
        self.assertFalse(is_error)
        self.assertEqual(payload["data"]["session"]["session_uid"], uid)

    def test_get_without_uid_is_an_error_result(self):
        is_error, payload = _call("agent_session_get", {})
        self.assertTrue(is_error)
        self.assertFalse(payload["ok"])

    def test_get_unknown_uid_is_an_error_result(self):
        is_error, payload = _call(
            "agent_session_get", {"session_uid": "{}:missing".format(SOURCE_JCODE_FS)}
        )
        self.assertTrue(is_error)
        self.assertEqual(payload["harness"], "inventory")


class HistoryTests(InventoryToolCase):
    def test_history_is_bounded_and_redacted(self):
        uid = "{}:{}".format(SOURCE_JCODE_FS, JCODE_ID)
        is_error, payload = _call(
            "agent_session_history", {"session_uid": uid, "limit": 2}
        )
        self.assertFalse(is_error)
        self.assertEqual(payload["data"]["source"], SOURCE_JCODE_FS)
        self.assertEqual(payload["data"]["limit"], 2)
        self.assertNotIn(FAKE_SECRET, json.dumps(payload))

    def test_history_limit_is_clamped(self):
        uid = "{}:{}".format(SOURCE_JCODE_FS, JCODE_ID)
        is_error, payload = _call(
            "agent_session_history", {"session_uid": uid, "limit": 10 ** 6}
        )
        self.assertFalse(is_error)
        self.assertEqual(payload["data"]["limit"], 200)

    def test_history_for_crew_uid(self):
        uid = "{}:{}".format(SOURCE_KIROCREW_LEDGER, CREW_ID)
        is_error, payload = _call("agent_session_history", {"session_uid": uid})
        self.assertFalse(is_error)
        self.assertIn("events", payload["data"])

    def test_history_without_uid_is_an_error_result(self):
        is_error, payload = _call("agent_session_history", {})
        self.assertTrue(is_error)


if __name__ == "__main__":
    unittest.main()
