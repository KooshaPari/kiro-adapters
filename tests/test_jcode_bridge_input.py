"""Regression tests for the jcode bridge input and error surface.

A bridge that hands a caller's prompt to a child process has to survive
whatever the caller sends: a NUL byte, a prompt that is not text at all, a home
that points at a regular file, an argument list that is too long. Each of those
has to come back as a result the caller can read, never as an exception that
escapes the API and never as a lie about the installation.

Every test in this file uses a fake POSIX executable and a throwaway home
directory. Nothing here touches a real ``jcode`` binary, a real user task
directory or a live configuration file.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from jcode_bridge.backend import JcodeBackend  # noqa: E402

from tests.test_jcode_bridge import BridgeTestCase  # noqa: E402

_OK_BODY = "printf '{\"text\": \"ok\"}\\n'"


class PromptValidationTests(BridgeTestCase):
    """A prompt that cannot reach the operating system is refused up front."""

    def test_a_prompt_with_a_nul_byte_is_rejected(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        for action in (backend.run, backend.start):
            with self.subTest(action=action.__name__):
                result = action("before\x00after")
                S.assert_result_failed(result, S.ERROR)
        self.assertFalse(
            backend.tasks_dir.exists() and list(backend.tasks_dir.iterdir()),
            "a rejected prompt left a task directory behind",
        )

    def test_a_prompt_that_is_not_text_is_rejected(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        backend = self.backend(fake)
        result = backend.run(1234)  # type: ignore[arg-type]
        S.assert_result_failed(result, S.ERROR)


class SpawnFailureTests(BridgeTestCase):
    """A launch that fails is reported as a launch failure, not as a missing binary."""

    def test_a_home_that_is_a_regular_file_is_rejected(self):
        fake = S.fake_bin(self.bin_dir, "jcode", _OK_BODY)
        plain = self.tmp / "plainfile"
        plain.write_text("not a directory", encoding="utf-8")
        backend = JcodeBackend(binary=fake, home=str(plain))
        result = backend.start("hello")
        S.assert_result_failed(result, S.ERROR)

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
