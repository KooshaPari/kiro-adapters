"""Tests for credential redaction."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.redact import redact, redact_obj  # noqa: E402

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsb2NhbC1hcHAifQ.abc123xyz"


class RedactTests(unittest.TestCase):
    def test_jwt_is_redacted(self):
        out = redact("dashboard http://localhost:5476?token={}".format(JWT))
        self.assertNotIn("eyJhbGciOi", out)
        self.assertIn("<jwt:redacted>", out)
        self.assertIn("http://localhost:5476", out)

    def test_token_assignment_is_redacted(self):
        out = redact("token=abcdefgh12345678")
        self.assertNotIn("abcdefgh12345678", out)
        self.assertIn("<redacted>", out)

    def test_api_key_assignment_is_redacted(self):
        out = redact("api_key: zzzzzzzzzzzzzzzz")
        self.assertNotIn("zzzzzzzzzzzzzzzz", out)

    def test_bearer_is_redacted(self):
        out = redact("Authorization: Bearer abcdefgh12345678")
        self.assertNotIn("abcdefgh12345678", out)
        self.assertIn("Bearer", out)

    def test_provider_key_is_redacted(self):
        out = redact("key sk-abcdefghijklmnopqrst")
        self.assertNotIn("sk-abcdefghijklmnopqrst", out)
        self.assertIn("<key:redacted>", out)

    def test_aws_and_github_tokens_are_redacted(self):
        out = redact("AKIAABCDEFGHIJKLMNOP ghp_abcdefghijklmnopqrstuvwx")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", out)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwx", out)

    def test_plain_text_is_untouched(self):
        text = "the agent reported 14 definitions and gateway running"
        self.assertEqual(redact(text), text)

    def test_empty_input(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")

    def test_redact_obj_walks_nested_structures(self):
        payload = {"a": [JWT, "plain"], "b": {"c": "token=" + "x" * 12}}
        cleaned = redact_obj(payload)
        self.assertNotIn("eyJhbGciOi", str(cleaned))
        self.assertIn("plain", cleaned["a"])


if __name__ == "__main__":
    unittest.main()
