"""Regression tests for the hostile surface of the jcode bridge.

The bridge is a public API, so every input it accepts has to come back as a
result the caller can read. A NUL byte, a prompt that is not text at all, a
home that points at a regular file and an argument list that is too long are
all refused with a failure result: never an exception that escapes the API,
never a task directory left behind, and never a lie about the installation.

The same file carries the two faces that have to agree with it: how a launch
failure is classified, and the fact that a version string reported by the
probed binary is redacted before it reaches the caller.

Every test in this file uses a fake POSIX executable and a throwaway home
directory. Nothing here touches a real ``jcode`` binary, a real user task
directory or a live configuration file.
"""

from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))
import _support as S  # noqa: E402

from jcode_bridge import backend as backend_module  # noqa: E402
from jcode_bridge import payload, tasks  # noqa: E402
from jcode_bridge.backend import JcodeBackend  # noqa: E402

from tests.test_jcode_bridge import BridgeTestCase  # noqa: E402

_OK_BODY = "printf '{\"text\": \"ok\"}\\n'"
_SECRET = "sk-ABCDEFGHIJKLMNOPQRST"
_REDACTED = "<key:redacted>"


class PromptRejectionTests(BridgeTestCase):
    """A prompt the operating system cannot accept is refused up front."""

    def assert_no_task_left(self, backend: JcodeBackend) -> None:
        """Assert that no task directory survived a rejected call."""
        root = backend.tasks_dir
        if root.exists():
            self.assertEqual(
                list(root.iterdir()), [], "a rejected call left a task behind"
            )

    def test_a_nul_byte_in_the_prompt_is_refused_by_run_and_start(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        for action in (backend.run, backend.start):
            with self.subTest(action=action.__name__):
                result = action("before\x00after")
                S.assert_result_failed(result, S.ERROR)
                self.assertIn("NUL", result.detail)
        self.assert_no_task_left(backend)

    def test_the_task_store_refuses_a_nul_byte_on_its_own(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        result = tasks.start(backend, "before\x00after")
        S.assert_result_failed(result, S.ERROR)
        self.assertIn("NUL", result.detail)
        self.assert_no_task_left(backend)

    def test_a_nul_byte_in_an_option_is_refused_by_run_and_start(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        for name in ("provider", "model", "tools", "disabled_tools"):
            for action in (backend.run, backend.start):
                with self.subTest(option=name, action=action.__name__):
                    result = action("hello", **{name: "a\x00b"})
                    S.assert_result_failed(result, S.ERROR)
                    self.assertIn("NUL", result.detail)
        self.assert_no_task_left(backend)

    def test_the_task_store_refuses_a_nul_byte_in_an_option(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        result = tasks.start(backend, "hello", provider="a\x00b")
        S.assert_result_failed(result, S.ERROR)
        self.assertIn("NUL", result.detail)
        self.assert_no_task_left(backend)

    def test_a_prompt_that_is_not_text_is_refused(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        for action in (backend.run, backend.start):
            with self.subTest(action=action.__name__):
                result = action(1234)  # type: ignore[arg-type]
                S.assert_result_failed(result, S.ERROR)
                self.assertIn("int", result.detail)
        result = tasks.start(backend, 1234)  # type: ignore[arg-type]
        S.assert_result_failed(result, S.ERROR)
        self.assert_no_task_left(backend)


class HomeLayoutTests(BridgeTestCase):
    """A home that cannot hold tasks is refused, not raised."""

    def test_a_home_that_is_a_regular_file_is_refused(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        plain = self.tmp / "plainfile"
        plain.write_text("not a directory", encoding="utf-8")
        backend = JcodeBackend(binary=fake, home=str(plain))
        result = backend.start("hello")
        S.assert_result_failed(result, S.ERROR)
        self.assertIn("tasks dir", result.detail)
        self.assertFalse((plain / "tasks").exists())


class SpawnFailureTests(BridgeTestCase):
    """A launch that fails is reported as a launch failure, not a missing binary."""

    def test_an_oversized_prompt_is_not_reported_as_a_missing_binary(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        result = backend.run("x" * (1024 * 1024))
        S.assert_result_failed(result)
        self.assertNotEqual(
            result.code,
            S.NOT_INSTALLED,
            "an argument list that is too long was reported as a missing binary: {!r}".format(
                result.detail
            ),
        )
        self.assertIn("OSError", result.detail)
        self.assertNotIn("binary not found", result.detail.lower())

    def test_a_spawn_error_is_classified_by_the_exception_it_names(self):
        self.assertEqual(
            backend_module._spawn_error_code(
                "FileNotFoundError: [Errno 2] No such file or directory: 'jcode'"
            ),
            S.NOT_INSTALLED,
        )
        self.assertEqual(
            backend_module._spawn_error_code(
                "PermissionError: [Errno 13] Permission denied: 'jcode'"
            ),
            S.NOT_INSTALLED,
        )
        for spawn_error in (
            "OSError: [Errno 7] Argument list too long: 'jcode'",
            "ValueError: embedded null byte",
            "SomethingElse: boom",
        ):
            with self.subTest(spawn_error=spawn_error):
                self.assertEqual(
                    backend_module._spawn_error_code(spawn_error), S.ERROR
                )


class HealthRedactionTests(BridgeTestCase):
    """health() reports a version, and a version never carries a secret."""

    def test_a_secret_version_is_redacted_from_the_health_result(self):
        body = 'printf \'{{"version": "{}"}}\\n\''.format(_SECRET)
        fake = S.fake_bin(self.bin_dir, "jcode", body)
        backend = self.backend(fake)
        result = backend.health()
        S.assert_result_ok(result)
        self.assertNotIn(_SECRET, result.detail or "")
        self.assertNotIn(_SECRET, json.dumps(result.data or {}))
        self.assertEqual((result.data or {}).get("version"), _REDACTED)

    def test_a_secret_on_the_failure_path_is_redacted_from_the_health_result(self):
        body = 'if [ "$1" = "version" ]; then printf "not-json {}\\n"; fi'.format(_SECRET)
        fake = S.fake_bin(self.bin_dir, "jcode", body)
        backend = self.backend(fake)
        result = backend.health()
        S.assert_result_failed(result, S.ERROR)
        self.assertNotIn(_SECRET, result.stdout or "")
        self.assertNotIn(_SECRET, json.dumps(result.data or {}))


class InputContractTests(BridgeTestCase):
    """The input helpers the bridge relies on keep their contract."""

    def test_the_prompt_helper_names_the_type_and_the_nul_byte(self):
        self.assertIsNone(payload.prompt_problem("hello"))
        self.assertIn("int", payload.prompt_problem(1234) or "")
        self.assertIn("empty", payload.prompt_problem("   ") or "")
        self.assertIn("NUL", payload.prompt_problem("a\x00b") or "")

    def test_the_option_helper_names_the_option_and_the_nul_byte(self):
        self.assertIsNone(payload.option_problem("provider", None))
        self.assertIsNone(payload.option_problem("provider", "openai"))
        self.assertIn("provider", payload.option_problem("provider", 7) or "")
        self.assertIn("model", payload.option_problem("model", "a\x00b") or "")
        self.assertIn("NUL", payload.option_problem("model", "a\x00b") or "")


if __name__ == "__main__":
    import unittest

    unittest.main()
