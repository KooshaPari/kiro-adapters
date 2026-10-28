"""Tests for the AdapterResult envelope."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import BLOCKED, OK, assert_result_failed, assert_result_ok  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters import result as R  # noqa: E402

EXPECTED_KEYS = {
    "ok",
    "harness",
    "action",
    "code",
    "detail",
    "data",
    "stdout",
    "stderr",
    "duration_ms",
    "hints",
}


class SuccessTests(unittest.TestCase):
    def test_success_defaults(self):
        value = R.AdapterResult.success("kirocrew", "status", "gateway running", {"a": 1})
        assert_result_ok(value, OK)
        self.assertEqual(value.detail, "gateway running")
        self.assertEqual(value.data, {"a": 1})
        self.assertEqual(value.hints, [])
        self.assertEqual(set(value.to_dict()), EXPECTED_KEYS)

    def test_success_carries_hints(self):
        value = R.AdapterResult.success(
            "kirocrew", "status", hints=["Start the gateway: kirocrew gateway"]
        )
        self.assertEqual(value.hints, ["Start the gateway: kirocrew gateway"])

    def test_to_json_round_trip(self):
        value = R.AdapterResult.success("kirocrew", "agents", data={"n": 2})
        parsed = json.loads(value.to_json())
        self.assertEqual(parsed["data"], {"n": 2})
        self.assertEqual(parsed["code"], OK)
        self.assertTrue(parsed["ok"])


class FailureTests(unittest.TestCase):
    def test_failure_fields(self):
        value = R.AdapterResult.failure(
            "kirocode",
            "ask",
            R.NOT_INSTALLED,
            "no binary",
            hints=["install it"],
        )
        assert_result_failed(value, R.NOT_INSTALLED)
        self.assertEqual(value.hints, ["install it"])
        self.assertEqual(set(value.to_dict()), EXPECTED_KEYS)

    def test_blocked_code_is_preserved(self):
        value = R.AdapterResult.failure("kirocrew", "ask", R.BLOCKED, "not ready")
        self.assertEqual(value.code, BLOCKED)
        self.assertFalse(value.ok)


class TruncationTests(unittest.TestCase):
    def test_long_stdout_is_truncated(self):
        payload = "x" * (R.MAX_STREAM_CHARS + 500)
        value = R.AdapterResult.success("kirocrew", "ask", stdout=payload)
        self.assertLess(len(value.stdout), len(payload))
        self.assertIn("[truncated 500 chars]", value.stdout)

    def test_short_stdout_is_untouched(self):
        value = R.AdapterResult.success("kirocrew", "ask", stdout="hello")
        self.assertEqual(value.stdout, "hello")

    def test_truncate_helper_handles_none(self):
        self.assertEqual(R.truncate(None), "")


class SummaryTests(unittest.TestCase):
    def test_summary_shape(self):
        ok = R.AdapterResult.success("kirocrew", "status", "gateway running")
        self.assertEqual(ok.summary(), "kirocrew:status ok - gateway running")
        bad = R.AdapterResult.failure("kirocrew", "ask", R.BLOCKED, "not ready")
        self.assertIn("BLOCKED", bad.summary())
        self.assertIn("not ready", bad.summary())


if __name__ == "__main__":
    unittest.main()
