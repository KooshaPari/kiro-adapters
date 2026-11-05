"""The ShareCLI session reader: bounded, fail-closed, and it never guesses.

ShareCLI answers ``session list`` with a JSON array of descriptors. Every test
here injects a runner, so the suite needs no ShareCLI install, starts no child
process, and never reads a real ledger. Each case covers one failure mode the
reader must survive without raising and without fabricating a session:

* valid payload -- one envelope per descriptor, with the fields projected and
  the rest (``resume.argv``) dropped;
* empty and malformed payloads -- zero sessions, not an exception;
* a missing binary -- zero sessions and a refused history, no traceback;
* a timeout and an oversized stdout -- zero sessions;
* redaction -- a descriptor carrying a credential-shaped field or credential
  in ``resume.argv`` never reaches the projected envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import sharecli_paths as paths  # noqa: E402
from kiro_adapters.inventory.sharecli_fs import ShareCLIStore  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    CAP_DISCOVER_SESSION,
    CAP_READ_HISTORY,
    SOURCE_SHARECLI_SESSION,
    STATE_RUNNING,
    STATE_UNKNOWN,
)

SECRET = "sk-live-DO-NOT-PROJECT-8f3a1c"


def _record(
    session_id: str = "codex:1111-2222",
    harness: str = "codex",
    state: str = "Active",
    **extra: object,
) -> dict:
    record = {
        "confidence": "Exact",
        "cwd": "/work/repo",
        "harness": harness,
        "id": session_id,
        "resume": {
            "argv": ["codex", "resume", "1111-2222", "--token", SECRET],
            "cwd": "/work/repo",
            "harness": harness,
            "session_id": "1111-2222",
        },
        "session_id": "1111-2222",
        "state": state,
    }
    record.update(extra)
    return record


def _store(payload: object, binary: str = "/fake/sharecli", **kwargs: object) -> ShareCLIStore:
    """A store whose one read returns ``payload`` rendered as JSON."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ShareCLIStore(
        binary=binary,
        runner=lambda argv: text,
        environ={},
        **kwargs,
    )


class ShareCLIReaderCase(unittest.TestCase):
    # -- valid payload -----------------------------------------------------
    def test_valid_payload_projects_one_envelope_per_descriptor(self):
        store = _store([_record(), _record("droid:3333", harness="droid")])
        envelopes = store.enumerate()

        self.assertEqual(len(envelopes), 2)
        first = envelopes[0]
        self.assertEqual(first.source, SOURCE_SHARECLI_SESSION)
        self.assertTrue(first.session_uid.startswith(SOURCE_SHARECLI_SESSION + ":"))
        self.assertEqual(first.native_session_id, "codex:1111-2222")
        self.assertEqual(first.harness_kind, "codex")
        self.assertEqual(first.workspace, "/work/repo")
        self.assertEqual(first.state, STATE_RUNNING)
        self.assertIsNone(first.process_instance)
        self.assertTrue(first.capability(CAP_DISCOVER_SESSION).supported)
        self.assertFalse(first.capability(CAP_READ_HISTORY).supported)
        self.assertIn("sharecli session list", first.evidence_ref)
        self.assertEqual(envelopes[1].harness_kind, "droid")

    def test_unknown_state_word_is_not_reinterpreted(self):
        store = _store([_record(state="Quiescing")])
        envelope = store.enumerate()[0]
        self.assertEqual(envelope.state, STATE_UNKNOWN)
        # The raw claim survives in the evidence line, not in the state field.
        self.assertIn("Quiescing", envelope.evidence_ref)

    def test_limit_bounds_the_answer(self):
        store = _store([_record("s{}".format(i)) for i in range(10)])
        self.assertEqual(len(store.enumerate(limit=3)), 3)
        self.assertEqual(store.enumerate(limit=0), [])

    # -- degraded payloads -------------------------------------------------
    def test_empty_payload_yields_no_sessions(self):
        self.assertEqual(_store([]).enumerate(), [])
        self.assertEqual(_store("[]").enumerate(), [])

    def test_malformed_json_fails_closed(self):
        store = _store("{not json at all")
        self.assertEqual(store.enumerate(), [])
        self.assertFalse(store.history("codex:1")["ok"])

    def test_non_array_payload_fails_closed(self):
        self.assertEqual(_store({"sessions": [_record()]}).enumerate(), [])
        self.assertEqual(_store('"a string"').enumerate(), [])

    def test_descriptor_without_id_is_skipped(self):
        orphan = {"harness": "codex", "state": "Active", "cwd": "/work"}
        envelopes = _store([orphan, _record()]).enumerate()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].native_session_id, "codex:1111-2222")

    def test_non_object_entries_are_skipped(self):
        envelopes = _store([_record(), "junk", 7, None]).enumerate()
        self.assertEqual(len(envelopes), 1)

    # -- missing binary / bad runner --------------------------------------
    def test_missing_binary_degrades_to_zero_sources(self):
        store = ShareCLIStore(binary="", runner=None, environ={"PATH": "/nonexistent"})
        self.assertFalse(store.exists())
        self.assertIsNone(store.binary)
        self.assertEqual(store.enumerate(), [])
        history = store.history("codex:1")
        self.assertFalse(history["ok"])
        self.assertEqual(history["messages"], [])

    def test_env_override_is_honoured(self):
        store = ShareCLIStore(environ={paths.BINARY_ENV: "/custom/sharecli"})
        self.assertTrue(store.exists())
        self.assertEqual(store.binary, "/custom/sharecli")

    def test_timeout_and_oserror_fail_closed(self):
        def boom(argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1.0)

        def denied(argv):
            raise OSError("permission denied")

        for runner in (boom, denied):
            store = ShareCLIStore(binary="/fake/sharecli", runner=runner, environ={})
            self.assertEqual(store.enumerate(), [])

    def test_runner_returning_a_non_string_fails_closed(self):
        store = ShareCLIStore(
            binary="/fake/sharecli", runner=lambda argv: b"[]", environ={}
        )
        self.assertEqual(store.enumerate(), [])

    def test_oversized_output_is_refused_not_parsed(self):
        store = _store([_record()], max_stdout=64)
        self.assertEqual(store.enumerate(), [])

    # -- bounds ------------------------------------------------------------
    def test_timeout_is_clamped_and_defaulted(self):
        self.assertEqual(paths.resolve_timeout(None, {}), paths.DEFAULT_TIMEOUT_SECS)
        self.assertEqual(paths.resolve_timeout(-4, {}), paths.DEFAULT_TIMEOUT_SECS)
        self.assertEqual(paths.resolve_timeout("abc", {}), paths.DEFAULT_TIMEOUT_SECS)
        self.assertEqual(
            paths.resolve_timeout(10_000, {}), paths.MAX_TIMEOUT_SECS
        )

    def test_limit_clamp_is_bounded(self):
        self.assertEqual(paths.clamp_limit(None), paths.DEFAULT_RECORD_LIMIT)
        self.assertEqual(paths.clamp_limit(10_000), paths.MAX_RECORDS)
        self.assertEqual(paths.clamp_limit(-1), 0)

    # -- redaction ---------------------------------------------------------
    def test_credentials_never_reach_the_envelope(self):
        leaky = _record(api_key=SECRET, token=SECRET, env={"API_KEY": SECRET})
        envelope = _store([leaky]).enumerate()[0]
        projected = json.dumps(envelope.to_dict())

        self.assertNotIn(SECRET, projected)
        self.assertNotIn('"resume"', projected)
        self.assertNotIn("argv", projected)
        self.assertNotIn("api_key", projected)
        self.assertNotIn("token", projected)

    def test_control_characters_in_cwd_cannot_forge_a_line(self):
        forged = _record(cwd="/work\nERROR: forged detail line")
        envelope = _store([forged]).enumerate()[0]
        self.assertNotIn("\n", envelope.workspace or "")
        self.assertNotIn("\n", envelope.evidence_ref or "")

    def test_history_is_refused_but_well_shaped(self):
        history = _store([]).history("codex:1111-2222", limit=5)
        self.assertEqual(history["source"], SOURCE_SHARECLI_SESSION)
        self.assertEqual(history["native_session_id"], "codex:1111-2222")
        self.assertEqual(history["returned"], 0)
        self.assertIn("descriptor", history["detail"])


if __name__ == "__main__":
    unittest.main()
