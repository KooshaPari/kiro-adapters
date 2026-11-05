"""CLI-backed inventory sources are an explicit opt-in, never a default.

The ShareCLI session reader is CLI-backed by necessity, so it must not ride in
``KNOWN_SOURCES``. These tests pin that boundary at both the registry level and
the MCP level:

* default discovery returns no ``sharecli-session`` envelopes even when a
  runner is injected that *would* return records;
* ``include_cli=True`` turns them on;
* the opt-in is environment-only at the MCP edge -- no tool argument can flip
  it -- and
* a malformed or failing CLI degrades to zero sessions, never a raise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import registry  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    SOURCE_HERDR_PANE,
    SOURCE_SHARECLI_SESSION,
)
from kiro_adapters.inventory.sharecli_fs import ShareCLIStore  # noqa: E402
from kiro_adapters import mcp_server  # noqa: E402


def _record(session_id: str = "codex:1111-2222", state: str = "Active") -> dict:
    return {
        "confidence": "Exact",
        "cwd": "/work/repo",
        "harness": "codex",
        "id": session_id,
        "session_id": "1111-2222",
        "state": state,
    }


def _yielding_store() -> ShareCLIStore:
    """A ShareCLI store whose one read would return two descriptors."""
    return ShareCLIStore(
        binary="/fake/sharecli",
        runner=lambda argv: json.dumps([_record(), _record("droid:3333")]),
        environ={},
    )


class RegistryCliOptInCase(unittest.TestCase):
    def test_default_discovery_excludes_sharecli_despite_yielding_runner(self):
        # A runner that would return records is injected; default discovery
        # must still never ask the CLI, so no sharecli envelope appears.
        with mock.patch(
            "kiro_adapters.inventory.registry.sharecli_fs.default_store",
            return_value=_yielding_store(),
        ):
            envelopes = registry.enumerate_sessions()
        sources = {e.source for e in envelopes}
        self.assertNotIn(SOURCE_SHARECLI_SESSION, sources)

    def test_opt_in_surfaces_sharecli_envelopes(self):
        with mock.patch(
            "kiro_adapters.inventory.registry.sharecli_fs.default_store",
            return_value=_yielding_store(),
        ):
            envelopes = registry.enumerate_sessions(include_cli=True)
        sources = {e.source for e in envelopes}
        self.assertIn(SOURCE_SHARECLI_SESSION, sources)
        sharecli = [e for e in envelopes if e.source == SOURCE_SHARECLI_SESSION]
        self.assertEqual(len(sharecli), 2)

    def test_available_sources_omits_sharecli_by_default_and_includes_on_opt_in(self):
        with mock.patch(
            "kiro_adapters.inventory.registry.sharecli_fs.default_store",
            return_value=_yielding_store(),
        ):
            default = registry.available_sources()
            opt_in = registry.available_sources(include_cli=True)
        self.assertNotIn(
            SOURCE_SHARECLI_SESSION, {s["source"] for s in default}
        )
        self.assertIn(SOURCE_SHARECLI_SESSION, {s["source"] for s in opt_in})

    def test_parse_session_uid_rejects_sharecli_unless_opted_in(self):
        uid = SOURCE_SHARECLI_SESSION + ":codex:1111-2222"
        self.assertIsNone(registry.parse_session_uid(uid))
        self.assertEqual(
            registry.parse_session_uid(uid, include_cli=True),
            (SOURCE_SHARECLI_SESSION, "codex:1111-2222"),
        )

    def test_known_sources_semantics_unchanged(self):
        # KNOWN_SOURCES stays the file-backed set; FILE_SOURCES is the same
        # tuple object, and CLI_SOURCES is separate. The CLI set is asserted by
        # membership rather than by an exact tuple, because more than one
        # adapter is CLI-backed by necessity (sharecli, herdr-pane) and the
        # invariant that matters is "file-backed stays file-backed".
        self.assertIs(registry.FILE_SOURCES, registry.KNOWN_SOURCES)
        self.assertNotIn(SOURCE_SHARECLI_SESSION, registry.KNOWN_SOURCES)
        self.assertNotIn(SOURCE_HERDR_PANE, registry.KNOWN_SOURCES)
        self.assertIn(SOURCE_SHARECLI_SESSION, registry.CLI_SOURCES)
        self.assertIn(SOURCE_HERDR_PANE, registry.CLI_SOURCES)

    def test_malformed_cli_degrades_to_zero_not_raise(self):
        def malformed(argv):
            return "{not json at all"

        store = ShareCLIStore(binary="/fake/sharecli", runner=malformed, environ={})
        with mock.patch(
            "kiro_adapters.inventory.registry.sharecli_fs.default_store",
            return_value=store,
        ):
            envelopes = registry.enumerate_sessions(include_cli=True)
        # No sharecli envelope, and no exception: the malformed CLI contributes
        # nothing even when the opt-in is on.
        self.assertNotIn(
            SOURCE_SHARECLI_SESSION, {e.source for e in envelopes}
        )

    def test_failing_cli_degrades_to_zero_not_raise(self):
        def boom(argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1.0)

        store = ShareCLIStore(binary="/fake/sharecli", runner=boom, environ={})
        with mock.patch(
            "kiro_adapters.inventory.registry.sharecli_fs.default_store",
            return_value=store,
        ):
            envelopes = registry.enumerate_sessions(include_cli=True)
        self.assertNotIn(
            SOURCE_SHARECLI_SESSION, {e.source for e in envelopes}
        )


class McpOptInCase(unittest.TestCase):
    def test_no_tool_argument_can_enable_cli_sources(self):
        # The four inventory tools expose no include_cli property, so the model
        # cannot flip the opt-in through an argument. It is env-only.
        for tool in mcp_server.build_tools(mode="inventory"):
            props = tool["inputSchema"]["properties"]
            self.assertNotIn("include_cli", props)
            self.assertNotIn("allow_cli", props)

    def test_cli_sources_disabled_by_default_and_enabled_by_env_only(self):
        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: None}):
            self.assertFalse(mcp_server._cli_sources_enabled())
        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: "1"}):
            self.assertTrue(mcp_server._cli_sources_enabled())
        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: "true"}):
            self.assertTrue(mcp_server._cli_sources_enabled())
        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: "0"}):
            self.assertFalse(mcp_server._cli_sources_enabled())

    def test_agent_sources_reflects_the_env_opt_in(self):
        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: None}):
            with mock.patch(
                "kiro_adapters.inventory.registry.sharecli_fs.default_store",
                return_value=_yielding_store(),
            ):
                result = mcp_server._agent_sources({})
            sources = {s["source"] for s in result.data["sources"]}
            self.assertNotIn(SOURCE_SHARECLI_SESSION, sources)

        with S.env_vars(**{mcp_server.CLI_SOURCES_ENV: "1"}):
            with mock.patch(
                "kiro_adapters.inventory.registry.sharecli_fs.default_store",
                return_value=_yielding_store(),
            ):
                result = mcp_server._agent_sources({})
            sources = {s["source"] for s in result.data["sources"]}
            self.assertIn(SOURCE_SHARECLI_SESSION, sources)


if __name__ == "__main__":
    unittest.main()
