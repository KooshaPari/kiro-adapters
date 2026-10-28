"""Tests for the subprocess runner."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.runner import first_line, resolve_binary, run  # noqa: E402


class BasicRunTests(unittest.TestCase):
    def test_captures_stdout(self):
        result = run(["/bin/echo", "hello"], timeout=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_captures_nonzero_exit(self):
        result = run(["/bin/sh", "-c", "exit 7"], timeout=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 7)

    def test_captures_stderr(self):
        result = run(["/bin/sh", "-c", "echo boom >&2"], timeout=10)
        self.assertIn("boom", result.stderr)

    def test_strips_ansi(self):
        result = run(["/bin/sh", "-c", "printf '\\033[31mred\\033[0m\\n'"], timeout=10)
        self.assertEqual(result.stdout.strip(), "red")

    def test_timeout_is_reported(self):
        result = run(["/bin/sleep", "5"], timeout=0.4)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_missing_binary_is_reported_not_raised(self):
        result = run(["/nonexistent/definitely-not-a-binary"], timeout=5)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.spawn_error)
        self.assertIsNone(result.code)

    def test_argv_is_not_shell_expanded(self):
        with S.temp_dir() as tmp:
            script = S.fake_bin(
                tmp,
                "echo-args",
                'printf "%s\\n" "$#"\nprintf "%s\\n" "$1"\n',
            )
            result = run([script, "a; echo pwned"], timeout=10)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines, ["1", "a; echo pwned"])

    def test_python_env_is_scrubbed(self):
        with S.env_vars(PYTHONPATH="/tmp/kiro-evil-path"):
            scrubbed = run(["/usr/bin/env"], timeout=10)
            kept = run(["/usr/bin/env"], timeout=10, scrub_python_env=False)
        self.assertNotIn("/tmp/kiro-evil-path", scrubbed.stdout)
        self.assertIn("/tmp/kiro-evil-path", kept.stdout)


class ResolveBinaryTests(unittest.TestCase):
    def test_env_override_wins(self):
        with S.temp_dir() as tmp:
            path = S.fake_bin(tmp, "fake-kiro-x", "exit 0\n")
            with S.env_vars(KIRO_TEST_BIN=path):
                found, tried = resolve_binary(("fake-kiro-x",), "KIRO_TEST_BIN")
        self.assertEqual(found, path)
        self.assertIn("KIRO_TEST_BIN={}".format(path), tried)

    def test_missing_binary_returns_none_with_diagnostics(self):
        with S.env_vars(KIRO_TEST_BIN=None, PATH="/usr/bin:/bin"):
            found, tried = resolve_binary(
                ("kirocode-does-not-exist",), "KIRO_TEST_BIN"
            )
        self.assertIsNone(found)
        self.assertTrue(any("which" in entry for entry in tried))


class HelperTests(unittest.TestCase):
    def test_first_line(self):
        self.assertEqual(first_line("\n\n  hello  \nworld"), "hello")
        self.assertEqual(first_line(""), "")


if __name__ == "__main__":
    unittest.main()
