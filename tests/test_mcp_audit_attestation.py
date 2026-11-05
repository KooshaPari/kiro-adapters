"""Tests for the opt-in, privacy-preserving tool-invocation audit log.

The contract under test: when ``KIRO_INVENTORY_AUDIT_LOG`` names a file, every
tool call appends exactly one JSON line with ``ts``/``tool``/``ok``/
``result_bytes`` and nothing else. Unset means no file at all, and a write
failure must not disturb the response.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters import mcp_server as M  # noqa: E402
from kiro_adapters import result as R  # noqa: E402

SECRET_ARG = "SECRET-ARGUMENT-VALUE"


class AuditAttestationTests(unittest.TestCase):
    def setUp(self):
        self._saved_handler = M._HANDLERS["agent_sources"]
        M._HANDLERS["agent_sources"] = lambda args: R.AdapterResult.success(
            "inventory", "agent_sources", detail="test", data={"count": 0, "sources": []}
        )
        self._saved_env = os.environ.pop(M.AUDIT_LOG_ENV, None)

    def tearDown(self):
        M._HANDLERS["agent_sources"] = self._saved_handler
        os.environ.pop(M.AUDIT_LOG_ENV, None)
        if self._saved_env is not None:
            os.environ[M.AUDIT_LOG_ENV] = self._saved_env

    def test_unset_env_writes_nothing(self):
        with S.temp_dir() as tmp:
            path = tmp / "audit.jsonl"
            os.environ.pop(M.AUDIT_LOG_ENV, None)
            result = M.call_tool("agent_sources", {}, mode="inventory")
            self.assertTrue(result.ok)
            self.assertFalse(path.exists())

    def test_empty_env_writes_nothing(self):
        with S.temp_dir() as tmp:
            path = tmp / "audit.jsonl"
            os.environ[M.AUDIT_LOG_ENV] = "   "
            M.call_tool("agent_sources", {}, mode="inventory")
            self.assertFalse(path.exists())

    def test_one_line_per_call_with_four_redacted_keys(self):
        with S.temp_dir() as tmp:
            path = tmp / "audit.jsonl"
            os.environ[M.AUDIT_LOG_ENV] = str(path)
            first = M.call_tool("agent_sources", {"session_uid": SECRET_ARG},
                                mode="inventory")
            second = M.call_tool("agent_sources", {}, mode="inventory")
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                record = json.loads(line)
                self.assertEqual(
                    set(record),
                    {"ts", "event", "tool", "ok", "result_bytes"},
                )
                self.assertEqual(record["event"], "tool")
                self.assertEqual(record["tool"], "agent_sources")
                self.assertIs(record["ok"], True)
                self.assertIsInstance(record["result_bytes"], int)
                self.assertGreater(record["result_bytes"], 0)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(SECRET_ARG, raw)
            self.assertNotIn('"sources"', raw)

    def test_tools_call_over_json_rpc_is_attested(self):
        with S.temp_dir() as tmp:
            path = tmp / "audit.jsonl"
            os.environ[M.AUDIT_LOG_ENV] = str(path)
            response = M.handle_message({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "agent_sources", "arguments": {}},
            }, mode="inventory")
            self.assertIn("result", response)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["tool"] for r in records], ["agent_sources"])

    def test_unwritable_path_does_not_break_the_response(self):
        with S.temp_dir() as tmp:
            os.environ[M.AUDIT_LOG_ENV] = str(tmp / "missing-dir" / "audit.jsonl")
            result = M.call_tool("agent_sources", {}, mode="inventory")
            self.assertTrue(result.ok)
            self.assertFalse((tmp / "missing-dir").exists())


if __name__ == "__main__":
    unittest.main()


class StartAttestationTests(unittest.TestCase):
    """The server records that it was SPAWNED, not only that a tool was called."""

    def test_start_is_recorded_without_any_tool_call(self):
        """A mounted-but-uncalled server must still leave evidence.

        This is the discriminator the MCP integration needed: a ``tool`` record
        proves a call, which requires the model to choose the tool, so it cannot
        tell "mounted but never called" from "never mounted". A ``start`` record
        can, because it is written before any protocol handling.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "audit.jsonl"
            os.environ[M.AUDIT_LOG_ENV] = str(path)
            self.assertEqual(M.main(["--inventory-only", "--selftest"]), 0)

            records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1, "exactly one start record expected")
            record = records[0]
            self.assertEqual(set(record), {"ts", "event", "pid", "mode", "argv"})
            self.assertEqual(record["event"], "start")
            self.assertEqual(record["mode"], "inventory")
            self.assertIsInstance(record["pid"], int)
            self.assertEqual(record["argv"], ["--inventory-only", "--selftest"])

    def test_start_is_not_written_when_audit_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "audit.jsonl"
            os.environ.pop(M.AUDIT_LOG_ENV, None)
            self.assertEqual(M.main(["--inventory-only", "--selftest"]), 0)
            self.assertFalse(path.exists())
