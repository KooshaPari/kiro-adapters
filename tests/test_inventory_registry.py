"""The read-only registry: one entry point over every session store.

Every test builds a throwaway ``$HOME`` and points the process at it, so no
real ``~/.jcode``, ``~/.kcode`` or ``~/.kiro`` tree is read. The registry's two
promises are checked directly: it never raises, and it never spawns a process
(the jcode stores are built with process probing off).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import registry  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    MAX_HISTORY_LIMIT,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    SOURCE_KIROCREW_LEDGER,
    STATE_STALE,
)

JCODE_ID = "session_registry_1700000000000_aaaaaaaaaaaaaaaa"
CREW_ID = "sess_registry_1700000000000_bbbbbbbbbbbbbbbb"
FAKE_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def _write_jcode(home: Path, native_id: str, with_pid: bool = False) -> None:
    store = home / ".jcode"
    (store / "sessions").mkdir(parents=True, exist_ok=True)
    (store / "active_pids").mkdir(parents=True, exist_ok=True)
    record = {
        "id": native_id,
        "title": "registry fixture",
        "created_at": "2026-10-20T00:00:00Z",
        "updated_at": "2026-10-20T00:05:00Z",
        "working_dir": "/tmp/registry/repo",
        "status": "Active",
        "last_pid": os.getpid() if with_pid else None,
        "messages": [
            {"id": "m1", "role": "user", "content": "hello " + FAKE_SECRET},
            {"id": "m2", "role": "assistant", "content": "ack"},
        ],
    }
    (store / "sessions" / (native_id + ".json")).write_text(
        json.dumps(record), encoding="utf-8"
    )
    if with_pid:
        (store / "active_pids" / native_id).write_text(str(os.getpid()), encoding="utf-8")


def _write_crew(home: Path, native_id: str) -> None:
    kiro = home / ".kiro"
    (kiro / "session-index").mkdir(parents=True, exist_ok=True)
    session_dir = kiro / "sessions" / "hash_a" / "sess_a"
    session_dir.mkdir(parents=True, exist_ok=True)
    record = session_dir / "sess_session.jsonl"
    lines = [
        json.dumps({"_type": "metadata", "id": native_id}),
        json.dumps({"_type": "turn", "type": "assistant", "index": 1, "text": "t1"}),
        json.dumps(
            {"_type": "turn", "type": "user", "index": 2, "text": "t2 " + FAKE_SECRET}
        ),
    ]
    record.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (kiro / "session-index" / "workspace.jsonl").write_text(
        json.dumps(
            {"op": "add", "sessionPath": "hash_a/sess_a", "at": 1700000000000}
        )
        + "\n",
        encoding="utf-8",
    )


class RegistryCase(unittest.TestCase):
    """A throwaway home holding one jcode and one crew session."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.home = self.tmp.__enter__()
        self._home = S.isolated_home(self.home)
        self._home.__enter__()
        _write_jcode(self.home, JCODE_ID)
        _write_crew(self.home, CREW_ID)

    def tearDown(self):
        self._home.__exit__(None, None, None)
        self.tmp.__exit__(None, None, None)


class SourceTests(RegistryCase):
    def test_every_known_source_is_listed(self):
        sources = registry.available_sources()
        self.assertEqual(
            [entry["source"] for entry in sources],
            [SOURCE_JCODE_FS, SOURCE_KCODE_FS, SOURCE_KIROCREW_LEDGER],
        )
        present = {entry["source"]: entry["present"] for entry in sources}
        self.assertTrue(present[SOURCE_JCODE_FS])
        self.assertTrue(present[SOURCE_KIROCREW_LEDGER])
        self.assertFalse(present[SOURCE_KCODE_FS])

    def test_empty_home_has_no_present_sources(self):
        with S.isolated_home(self.home / "empty-home"):
            sources = registry.available_sources()
        self.assertFalse(any(entry["present"] for entry in sources))


class EnumerateTests(RegistryCase):
    def test_both_stores_contribute_envelopes(self):
        envelopes = registry.enumerate_sessions()
        self.assertEqual(
            sorted(envelope.source for envelope in envelopes),
            sorted([SOURCE_JCODE_FS, SOURCE_KIROCREW_LEDGER]),
        )
        uids = {envelope.session_uid for envelope in envelopes}
        self.assertIn("{}:{}".format(SOURCE_JCODE_FS, JCODE_ID), uids)
        self.assertIn("{}:{}".format(SOURCE_KIROCREW_LEDGER, CREW_ID), uids)

    def test_order_is_deterministic(self):
        first = [e.session_uid for e in registry.enumerate_sessions()]
        second = [e.session_uid for e in registry.enumerate_sessions()]
        self.assertEqual(first, second)

    def test_include_stale_false_drops_heartbeatless_sessions(self):
        envelopes = registry.enumerate_sessions(include_stale=False)
        self.assertTrue(
            all(envelope.state != STATE_STALE for envelope in envelopes),
            [envelope.state for envelope in envelopes],
        )
        self.assertLessEqual(len(envelopes), len(registry.enumerate_sessions()))

    def test_limit_is_bounded_and_never_negative(self):
        self.assertEqual(registry.enumerate_sessions(limit=0), [])
        self.assertEqual(registry.enumerate_sessions(limit=-5), [])
        self.assertEqual(len(registry.enumerate_sessions(limit=1)), 1)
        self.assertLessEqual(
            len(registry.enumerate_sessions(limit=10 ** 9)),
            registry.MAX_SESSION_LIMIT,
        )

    def test_enumeration_spawns_no_process(self):
        # The registry builds jcode stores with process probing disabled, so
        # enumeration must complete with subprocess.run booby-trapped.
        with mock.patch(
            "subprocess.run", side_effect=AssertionError("registry spawned a process")
        ):
            envelopes = registry.enumerate_sessions()
        self.assertTrue(envelopes)


class FindTests(RegistryCase):
    def test_find_returns_the_envelope(self):
        uid = "{}:{}".format(SOURCE_JCODE_FS, JCODE_ID)
        self.assertIsNotNone(registry.find_session(uid))

    def test_unknown_or_malformed_uid_returns_none(self):
        self.assertIsNone(registry.find_session("nope"))
        self.assertIsNone(registry.find_session("{}:{}".format("bogus-source", JCODE_ID)))
        self.assertIsNone(registry.find_session(None))
        self.assertIsNone(registry.find_session("{}:{}".format(SOURCE_JCODE_FS, "missing")))


class HistoryTests(RegistryCase):
    def test_jcode_history_is_redacted_and_bounded(self):
        result = registry.session_history("{}:{}".format(SOURCE_JCODE_FS, JCODE_ID))
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], SOURCE_JCODE_FS)
        self.assertEqual(result["limit"], registry.DEFAULT_HISTORY_LIMIT)
        self.assertNotIn(FAKE_SECRET, json.dumps(result))
        self.assertEqual(result["returned"], 2)

    def test_crew_history_dispatches_by_prefix(self):
        result = registry.session_history("{}:{}".format(SOURCE_KIROCREW_LEDGER, CREW_ID))
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], SOURCE_KIROCREW_LEDGER)
        self.assertIn("events", result)
        self.assertNotIn(FAKE_SECRET, json.dumps(result))

    def test_limit_is_clamped_to_the_shared_ceiling(self):
        uid = "{}:{}".format(SOURCE_JCODE_FS, JCODE_ID)
        huge = registry.session_history(uid, limit=10 ** 6)
        self.assertEqual(huge["limit"], MAX_HISTORY_LIMIT)
        self.assertEqual(registry.session_history(uid, limit=-3)["limit"], 0)
        self.assertEqual(registry.session_history(uid, limit="not-a-number")["limit"], 0)

    def test_unknown_uid_is_a_failure_dict_not_an_exception(self):
        for uid in ("nope", "", None, "bogus-source:abc"):
            result = registry.session_history(uid)
            self.assertFalse(result["ok"])
            self.assertIn("detail", result)

    def test_summary_counts_sources(self):
        summary = registry.inventory_summary()
        self.assertGreaterEqual(summary["total"], 2)
        self.assertIn(SOURCE_JCODE_FS, summary["by_source"])
        self.assertEqual(len(summary["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
