"""Tests for the Kiro Code adapter (binary resolution + capability probing)."""

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
from kiro_adapters.kirocode import KiroCodeAdapter  # noqa: E402

SCRIPT = """case "$1" in
  --help) printf '%s\\n' {help_lines} ;;
  --version) printf 'kirocode 1.2.3\\n' ;;
  *) printf 'ARGS:%s\\n' "$*" ;;
esac
"""


def help_lines(*lines: str) -> str:
    return " ".join("'{}'".format(line) for line in lines)


class CodeTestBase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="kirocode-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.config_path = self.tmp / "adapter.json"

    def adapter(self, *help_text: str, body: str = None) -> "tuple[KiroCodeAdapter, str]":
        script = body if body is not None else SCRIPT.format(help_lines=help_lines(*help_text))
        binary = S.fake_bin(self.tmp, "kirocode", script)
        return (
            KiroCodeAdapter(binary=binary, config_path=str(self.config_path)),
            binary,
        )


class ProbeTests(CodeTestBase):
    def test_no_interactive_flag_is_preferred(self):
        adapter, binary = self.adapter("usage: kirocode chat", "  chat --no-interactive PROMPT")
        result = adapter.ask("hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["argv"], [binary, "chat", "--no-interactive", "hello"])
        self.assertIn("ARGS:chat --no-interactive hello", result.data["answer"])

    def test_print_flag_is_used_when_no_interactive_absent(self):
        adapter, binary = self.adapter("usage: kirocode chat", "  chat --print PROMPT")
        result = adapter.ask("hello")
        self.assertEqual(result.data["argv"], [binary, "chat", "--print", "hello"])

    def test_positional_chat_is_last_resort(self):
        adapter, binary = self.adapter("usage: kirocode", "  chat PROMPT")
        result = adapter.ask("hello")
        self.assertEqual(result.data["argv"], [binary, "chat", "hello"])

    def test_exec_is_used_when_chat_absent(self):
        adapter, binary = self.adapter("usage: kirocode", "  exec PROMPT")
        result = adapter.ask("hello")
        self.assertEqual(result.data["argv"], [binary, "exec", "hello"])

    def test_unrecognised_cli_is_unsupported(self):
        adapter, _ = self.adapter("usage: kirocode --help")
        result = adapter.ask("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.UNSUPPORTED)
        self.assertTrue(result.hints)

    def test_status_reports_version_and_capabilities(self):
        adapter, _ = self.adapter("usage: kirocode chat", "  chat --no-interactive PROMPT")
        result = adapter.status()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["version"], "kirocode 1.2.3")
        self.assertTrue(result.data["capabilities"]["supports_no_interactive"])
        self.assertEqual(result.data["argv_source"], "help-probe")
        self.assertFalse(result.data["verified_live"])


class ConfigTests(CodeTestBase):
    def test_config_argv_template_wins(self):
        self.config_path.write_text(
            json.dumps({"argv": ["chat", "--no-interactive", "{prompt}"]}),
            encoding="utf-8",
        )
        adapter, binary = self.adapter("usage: kirocode --help")
        result = adapter.ask("hey")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["argv"], [binary, "chat", "--no-interactive", "hey"])
        self.assertEqual(adapter.status().data["argv_source"], "config")

    def test_config_bin_is_used_when_env_absent(self):
        binary = S.fake_bin(self.tmp, "kiro-cli", SCRIPT.format(help_lines=help_lines("usage: x")))
        self.config_path.write_text(json.dumps({"bin": binary}), encoding="utf-8")
        with S.env_vars(KIROCODE_BIN=None):
            adapter = KiroCodeAdapter(config_path=str(self.config_path))
            self.assertEqual(adapter.binary, binary)


class MissingBinaryTests(unittest.TestCase):
    def test_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with S.env_vars(KIROCODE_BIN=None, PATH="/usr/bin:/bin"):
                adapter = KiroCodeAdapter(config_path=os.path.join(tmp, "missing.json"))
                result = adapter.status()
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.NOT_INSTALLED)
        self.assertFalse(result.data["found"])
        self.assertTrue(result.hints)

    def test_ask_without_binary_is_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with S.env_vars(KIROCODE_BIN=None, PATH="/usr/bin:/bin"):
                adapter = KiroCodeAdapter(config_path=os.path.join(tmp, "missing.json"))
                result = adapter.ask("hi")
        self.assertEqual(result.code, S.NOT_INSTALLED)


class FailureTests(CodeTestBase):
    def test_nonzero_exit_is_error(self):
        body = (
            'case "$1" in\n'
            "  --help) printf '%s\\n' 'usage: kirocode chat' '  chat PROMPT' ;;\n"
            "  *) exit 3 ;;\n"
            "esac\n"
        )
        adapter, _ = self.adapter(body=body)
        result = adapter.ask("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.ERROR)

    def test_timeout_is_reported(self):
        body = (
            'case "$1" in\n'
            "  --help) printf '%s\\n' 'usage: kirocode chat' '  chat PROMPT' ;;\n"
            "  chat) sleep 5 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        adapter, _ = self.adapter(body=body)
        result = adapter.ask("hello", timeout=0.4)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, S.TIMEOUT)

    def test_empty_prompt_is_rejected(self):
        adapter, _ = self.adapter("usage: kirocode", "  chat PROMPT")
        result = adapter.ask("  ")
        self.assertEqual(result.code, S.ERROR)


if __name__ == "__main__":
    unittest.main()
