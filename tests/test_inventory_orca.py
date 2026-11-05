"""The Orca agent-hook adapter reads files, and only files.

Each test builds a throwaway ``.../orca/agent-hooks`` tree; the real Orca
profile is never touched and no hook endpoint is contacted.

The defects covered:

* **shape** -- a valid ``last-status.json`` becomes one envelope per entry,
  with Orca's own state mapped onto the shared vocabulary;
* **failure** -- empty, malformed, missing, oversized and symlink-escaping
  documents all come back as "no sessions", not as an exception or a guess;
* **redaction** -- a token in ``endpoint.env``, and token-like keys or values
  in ``last-status.json``, never reach a projection.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory.model import (  # noqa: E402
    SOURCE_ORCA_FS,
    STATE_UNKNOWN,
    STATE_WORKING,
)
from kiro_adapters.inventory.orca_fs import OrcaStore  # noqa: E402
from kiro_adapters.inventory.registry import FILE_SOURCES, KNOWN_SOURCES  # noqa: E402

TOKEN_VALUE = "orca-token-do-not-leak-0123456789abc"
SECRET_TITLE = "token=sk-abcdefghijklmnopqrstuvwx"


def _status(entries, version: int = 2) -> str:
    return json.dumps(
        {"version": version, "entries": entries, "authorityCommitments": {}}
    )


def _entry(**overrides):
    base = {
        "state": "working",
        "title": "review the parser",
        "cwd": "/tmp/workspace",
        "updatedAt": 1700000000000,
    }
    base.update(overrides)
    return base


class OrcaCase(unittest.TestCase):
    """A throwaway Orca profile."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orca = Path(self.tmp.name) / "orca"
        self.hooks = self.orca / "agent-hooks"
        self.hooks.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_env(self, extra: str = "") -> None:
        (self.hooks / "endpoint.env").write_text(
            "\n".join(
                [
                    "ORCA_AGENT_HOOK_PORT=62042",
                    "ORCA_AGENT_HOOK_TOKEN={}".format(TOKEN_VALUE),
                    "ORCA_AGENT_HOOK_ENV=production",
                    "ORCA_AGENT_HOOK_VERSION=1",
                    "ORCA_AGENT_HOOK_TRANSPORT=raw-json-v1",
                    "ORCA_AGENT_HOOK_OPENCODE_TUI=0",
                    extra,
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def write_status(self, body: str) -> None:
        (self.hooks / "last-status.json").write_text(body, encoding="utf-8")

    def store(self) -> OrcaStore:
        return OrcaStore(self.orca, host_id="host", namespace_id="ns")


class EnvelopeTests(OrcaCase):
    def test_one_entry_becomes_one_envelope(self):
        self.write_env()
        self.write_status(_status({"sess-a": _entry()}))
        found = self.store().enumerate()
        self.assertEqual(len(found), 1)
        envelope = found[0]
        self.assertEqual(envelope.session_uid, SOURCE_ORCA_FS + ":sess-a")
        self.assertEqual(envelope.source, SOURCE_ORCA_FS)
        self.assertEqual(envelope.harness_kind, "orca")
        self.assertEqual(envelope.state, STATE_WORKING)
        self.assertEqual(envelope.title, "review the parser")
        self.assertEqual(envelope.workspace, "/tmp/workspace")
        self.assertEqual(envelope.source_updated_at, "2023-11-14T22:13:20Z")
        # No pid in the artifact, so no process claim is made.
        self.assertIsNone(envelope.process_instance)
        self.assertTrue(envelope.supports("discover_session"))
        self.assertFalse(envelope.supports("discover_process"))

    def test_entry_with_explicit_id_uses_it(self):
        self.write_status(_status({"map-key": _entry(id="explicit-id")}))
        self.assertEqual(
            self.store().enumerate()[0].native_session_id, "explicit-id"
        )

    def test_unknown_state_stays_unknown(self):
        self.write_status(_status({"s1": _entry(state="launching")}))
        self.assertEqual(self.store().enumerate()[0].state, STATE_UNKNOWN)

    def test_limit_is_respected(self):
        entries = {"s{}".format(i): _entry() for i in range(5)}
        self.write_status(_status(entries))
        self.assertEqual(len(self.store().enumerate(limit=2)), 2)

    def test_history_is_a_declared_refusal(self):
        result = self.store().history("sess-a", limit=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["messages"], [])


class FailureTests(OrcaCase):
    def test_empty_entries_is_empty_list(self):
        self.write_status(_status({}))
        self.assertEqual(self.store().enumerate(), [])

    def test_malformed_json_is_empty_list(self):
        self.write_status("{not json")
        self.assertEqual(self.store().enumerate(), [])

    def test_json_array_is_refused(self):
        self.write_status("[1, 2, 3]")
        self.assertEqual(self.store().enumerate(), [])

    def test_missing_directory_is_absent(self):
        store = OrcaStore(self.orca / "nope")
        self.assertFalse(store.exists())
        self.assertEqual(store.enumerate(), [])

    def test_oversized_document_is_refused(self):
        self.write_status('{"version":2,"entries":{' + " " * 300000 + "}}")
        self.assertEqual(self.store().enumerate(), [])
        # A well-formed but oversized document must not be partially parsed.
        self.assertIsNone(self.store().read_last_status())

    def test_symlink_escape_is_refused(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(_status({"s1": _entry()}), encoding="utf-8")
        try:
            os.symlink(str(outside), str(self.hooks / "last-status.json"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        store = self.store()
        self.assertIsNone(store.read_last_status())
        self.assertEqual(store.enumerate(), [])

    def test_symlinked_agent_hooks_dir_escape_is_refused(self):
        outside_dir = Path(self.tmp.name) / "outside-hooks"
        outside_dir.mkdir()
        (outside_dir / "last-status.json").write_text(
            _status({"s1": _entry()}), encoding="utf-8"
        )
        empty_orca = Path(self.tmp.name) / "empty-orca"
        empty_orca.mkdir()
        try:
            os.symlink(str(outside_dir), str(empty_orca / "agent-hooks"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(OrcaStore(empty_orca).enumerate(), [])


class RedactionTests(OrcaCase):
    """A token must never reach a projection, by key or by value."""

    def test_env_token_value_never_projected(self):
        self.write_env()
        endpoint = self.store().endpoint()
        self.assertTrue(endpoint["ok"])
        self.assertIn("ORCA_AGENT_HOOK_TOKEN", endpoint["keys"])
        self.assertTrue(endpoint["token_present"])
        self.assertNotIn("ORCA_AGENT_HOOK_TOKEN", endpoint["values"])
        self.assertNotIn(TOKEN_VALUE, json.dumps(endpoint))
        self.assertEqual(endpoint["values"]["ORCA_AGENT_HOOK_TRANSPORT"], "raw-json-v1")

    def test_token_like_entry_keys_and_values_never_projected(self):
        self.write_env()
        self.write_status(
            _status(
                {
                    "s1": _entry(
                        title=SECRET_TITLE,
                        token=TOKEN_VALUE,
                        apiKey=TOKEN_VALUE,
                        Authorization="Bearer " + TOKEN_VALUE,
                    )
                }
            )
        )
        rendered = json.dumps([e.to_dict() for e in self.store().enumerate()])
        self.assertNotIn(TOKEN_VALUE, rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("apiKey", rendered)
        # The envelope redacts credential-shaped text even inside a title.
        self.assertNotIn(SECRET_TITLE, rendered)


class RegistryPlacementTests(unittest.TestCase):
    def test_orca_is_a_file_source(self):
        self.assertIn(SOURCE_ORCA_FS, KNOWN_SOURCES)
        self.assertIn(SOURCE_ORCA_FS, FILE_SOURCES)


if __name__ == "__main__":
    unittest.main()
