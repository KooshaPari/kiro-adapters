"""Tests for the Kiro Crew adapter, driven by fake ``kirocrew`` binaries."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.kirocrew import KiroCrewAdapter  # noqa: E402

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsb2NhbC1hcHAifQ.abc123xyz"

RUNNING_BODY = """cmd="$1"
case "$cmd" in
  status) printf 'Kiro Crew gateway is running (token auth enabled).\\n' ;;
  kas) printf 'pinned:        KAS 0.66.33; runs 0.66.33\\ninstalled:     KAS 0.66.33 is not installed  (run kirocrew kas install)\\n' ;;
  chat) printf 'ANSWER: ok\\n' ;;
  token) printf 'http://localhost:5476?token=%s\\n' '__JWT__' ;;
  spawn) printf 'spawned\\n' ;;
  run) printf 'spec done\\n' ;;
  *) printf 'unknown\\n' ;;
esac
""".replace("__JWT__", JWT)

STOPPED_BODY = """cmd="$1"
case "$cmd" in
  status) printf 'Kiro Crew gateway is not running.\\n' ;;
  kas) printf 'installed:     KAS 0.66.33 is not installed\\n' ;;
  *) printf 'unknown\\n' ;;
esac
"""

KAS_INSTALLED_BODY = """cmd="$1"
case "$cmd" in
  kas) printf 'pinned:        KAS 0.66.33; runs 0.66.33\\ninstalled:     KAS 0.66.33 is installed  (ready)\\n' ;;
  status) printf 'Kiro Crew gateway is running (token auth enabled).\\n' ;;
  *) printf 'unknown\\n' ;;
esac
"""


class CrewTestBase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="kirocrew-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.agents_dir = self.tmp / "agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)

    def adapter(self, body=RUNNING_BODY, name="kirocrew", marker=None):
        if marker is None:
            binary = S.fake_bin(self.tmp, name, body)
        else:
            binary = S.fake_bin_with_marker(self.tmp, name, marker, body)
        return KiroCrewAdapter(binary=binary, agents_dir=str(self.agents_dir))


class StatusTests(CrewTestBase):
    def test_running_gateway(self):
        result = self.adapter().status()
        self.assertTrue(result.ok)
        self.assertTrue(result.data["gateway_running"])
        self.assertTrue(result.data["token_auth"])
        self.assertEqual(result.data["agents_available"], 0)

    def test_stopped_gateway(self):
        result = self.adapter(STOPPED_BODY).status()
        self.assertTrue(result.ok)
        self.assertFalse(result.data["gateway_running"])
        self.assertTrue(any("gateway" in hint.lower() for hint in result.hints))

    def test_status_is_cached(self):
        marker = self.tmp / "calls.log"
        adapter = self.adapter(marker=marker)
        adapter.status()
        adapter.status()
        calls = [line for line in S.marker_lines(marker) if line.startswith("status")]
        self.assertEqual(len(calls), 1, "second status() should come from cache")

    def test_status_includes_kas_when_asked(self):
        result = self.adapter().status(include_kas=True)
        self.assertIn("kas", result.data)
        self.assertFalse(result.data["kas"]["installed"])
        self.assertFalse(result.data["ready"])


class KasTests(CrewTestBase):
    def test_not_installed(self):
        result = self.adapter().kas_status()
        self.assertTrue(result.ok)
        self.assertFalse(result.data["installed"])
        self.assertEqual(result.data["pinned_version"], "0.66.33")
        self.assertTrue(result.hints)

    def test_installed(self):
        result = self.adapter(KAS_INSTALLED_BODY).kas_status()
        self.assertTrue(result.data["installed"])
        self.assertEqual(result.hints, [])


class AskTests(CrewTestBase):
    def test_blocked_when_engine_missing(self):
        marker = self.tmp / "calls.log"
        adapter = self.adapter(marker=marker)
        result = adapter.ask("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.BLOCKED)
        self.assertTrue(result.hints)
        self.assertTrue(S.marker_exists(marker))
        calls = S.marker_lines(marker)
        self.assertTrue(calls, "readiness should have probed the engine")
        self.assertFalse(
            [line for line in calls if line.startswith("chat")],
            "chat must not run while the engine is missing",
        )

    def test_ask_success_when_readiness_skipped(self):
        adapter = self.adapter()
        result = adapter.ask("hello", require_ready=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["answer"], "ANSWER: ok")
        self.assertIn("chat", result.data["argv"])
        self.assertIn("-m", result.data["argv"])
        self.assertEqual(result.data["argv"][-1], "hello")

    def test_agent_and_model_flags(self):
        adapter = self.adapter()
        result = adapter.ask("hi", agent="reviewer", model="auto", require_ready=False)
        argv = result.data["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "reviewer")
        self.assertEqual(argv[argv.index("--model") + 1], "auto")

    def test_empty_message_is_rejected(self):
        result = self.adapter().ask("   ", require_ready=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.ERROR)


class DashboardTests(CrewTestBase):
    def test_token_is_redacted_by_default(self):
        result = self.adapter().dashboard_url()
        self.assertTrue(result.ok)
        self.assertNotIn("eyJhbGciOi", result.data["url"])
        self.assertIn("<jwt:redacted>", result.data["url"])
        self.assertEqual(result.data["port"], 5476)
        self.assertFalse(result.data["token_included"])

    def test_token_can_be_requested_explicitly(self):
        result = self.adapter().dashboard_url(include_token=True)
        self.assertIn("eyJhbGciOi", result.data["url"])


class SpecTests(CrewTestBase):
    def test_missing_spec_is_an_error(self):
        result = self.adapter().run_spec(str(self.tmp / "nope.md"))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.ERROR)
        self.assertIn("not found", result.detail)

    def test_existing_spec_runs(self):
        spec = self.tmp / "TASK.md"
        spec.write_text("# task\n", encoding="utf-8")
        result = self.adapter().run_spec(str(spec), require_ready=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["name"], "TASK")


class AgentListTests(CrewTestBase):
    def test_parses_agent_definitions(self):
        (self.agents_dir / "alpha.json").write_text(
            json.dumps({"name": "alpha", "description": "first", "model": "auto"}),
            encoding="utf-8",
        )
        (self.agents_dir / "beta.json").write_text(
            json.dumps({"name": "beta"}), encoding="utf-8"
        )
        result = self.adapter().list_agents()
        self.assertTrue(result.ok)
        self.assertEqual([agent["name"] for agent in result.data["agents"]], ["alpha", "beta"])
        self.assertEqual(result.data["errors"], [])

    def test_broken_json_is_reported_not_raised(self):
        (self.agents_dir / "broken.json").write_text("{not json", encoding="utf-8")
        result = self.adapter().list_agents()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["agents"], [])
        self.assertEqual(len(result.data["errors"]), 1)


class MissingBinaryTests(unittest.TestCase):
    def test_not_installed(self):
        with S.env_vars(KIROCREW_BIN=None, PATH="/usr/bin:/bin"):
            adapter = KiroCrewAdapter()
            result = adapter.status()
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.NOT_INSTALLED)
        self.assertTrue(result.hints)


if __name__ == "__main__":
    unittest.main()
