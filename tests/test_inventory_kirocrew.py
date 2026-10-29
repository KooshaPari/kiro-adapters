"""The crew ledger adapter must not follow a path out of its store.

Three concerns, one per defect:

* containment -- a ``sessionPath`` that is absolute or walks out with ``..``
  (or leaves through a symlink) is not a crew session, and neither
  ``enumerate`` nor ``history`` may read it.
* bounds -- ``history`` reads a bounded tail, not the whole transcript.
* failure hygiene -- a path or an OS message in a failure detail is bounded
  and redacted before it reaches a tool result.

Standard library only. No network. No real ``~/.kiro`` tree is touched.
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
from kiro_adapters.inventory.kirocrew_fs import KiroCrewStore  # noqa: E402
from kiro_adapters.inventory.jcode_fs import MAX_HISTORY_LIMIT  # noqa: E402
from kiro_adapters.inventory.model import CAP_ENROLL  # noqa: E402

SESSION_ID = "sess_containment_1700000000000_aaaaaaaaaaaaaaaa"


def _metadata_line(session_id: str = SESSION_ID) -> str:
    return json.dumps(
        {
            "_type": "metadata",
            "id": session_id,
            "model": "test-model",
            "created_at": "2026-10-28T00:00:00Z",
        }
    )


def _turn_line(index: int) -> str:
    return json.dumps(
        {"_type": "turn", "type": "assistant", "index": index, "text": "turn-{}".format(index)}
    )


class CrewStoreCase(unittest.TestCase):
    """A throwaway ``~/.kiro`` shaped tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kiro = Path(self.tmp.name) / ".kiro"
        (self.kiro / "session-index").mkdir(parents=True)
        (self.kiro / "sessions").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def ledger(self, session_path: str, op: str = "add") -> None:
        path = self.kiro / "session-index" / "workspace.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"op": op, "sessionPath": session_path, "at": 1700000000000})
                + "\n"
            )

    def session(self, relative: str, lines=None) -> Path:
        """A session directory with one jsonl, plus a readable file in it."""
        directory = self.kiro / "sessions" / relative
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / "sess_session.jsonl"
        body = lines if lines is not None else [_metadata_line(), _turn_line(1)]
        record.write_text("\n".join(body) + "\n", encoding="utf-8")
        return record

    def store(self) -> KiroCrewStore:
        return KiroCrewStore(self.kiro, host_id="host", namespace_id="ns")


class PathContainmentTests(CrewStoreCase):
    """A ledger path that resolves outside the store is refused, not read."""

    def test_absolute_session_path_is_refused(self):
        self.ledger("/etc/passwd")
        store = self.store()
        self.assertEqual(store.enumerate(), [])
        self.assertIsNone(store._resolve_session_file("/etc/passwd"))
        result = store.history(SESSION_ID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["events"], [])

    def test_parent_traversal_is_refused(self):
        self.ledger("../../x")
        store = self.store()
        self.assertEqual(store.enumerate(), [])
        self.assertIsNone(store._resolve_session_file("../../x"))

    def test_traversal_stays_refused_even_when_the_target_exists(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "sess_session.jsonl").write_text(
            _metadata_line() + "\n" + _turn_line(7) + "\n", encoding="utf-8"
        )
        self.ledger("../../outside")
        self.assertEqual(self.store().enumerate(), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_leaving_the_store_is_refused(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text(_metadata_line() + "\n" + _turn_line(99) + "\n")
        link_dir = self.kiro / "sessions" / "linked"
        link_dir.mkdir(parents=True)
        try:
            os.symlink(str(outside), str(link_dir / "sess_session.jsonl"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlink here")
        self.ledger("linked")
        store = self.store()
        self.assertEqual(store.enumerate(), [])
        self.assertIsNone(store._resolve_session_file("linked"))
        self.assertFalse(store.history(SESSION_ID)["ok"])

    def test_a_real_relative_session_still_resolves(self):
        record = self.session("hash_a/sess_a")
        self.ledger("hash_a/sess_a")
        store = self.store()
        envelopes = store.enumerate()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(store._resolve_session_file("hash_a/sess_a"), record)
        result = store.history(SESSION_ID, limit=5)
        self.assertTrue(result["ok"])
        self.assertEqual([event["payload"]["index"] for event in result["events"]], [1])

    def test_empty_and_dotdot_only_paths_are_refused(self):
        store = self.store()
        for path in ("", ".", "..", "./../..", "\x00bad"):
            with self.subTest(path=path):
                self.assertIsNone(store._resolve_session_file(path))


class HistoryBoundTests(CrewStoreCase):
    """``history`` reads a bounded tail and returns at most ``limit`` events."""

    def _big_session(self, count: int = 5000) -> Path:
        lines = [_metadata_line()] + [_turn_line(i) for i in range(count)]
        record = self.session("hash_big/sess_big", lines)
        self.ledger("hash_big/sess_big")
        return record

    def test_byte_cap_keeps_only_the_tail(self):
        record = self._big_session()
        store = self.store()
        self.assertEqual(len(store.enumerate()), 1)
        size = record.stat().st_size
        result = store.history(SESSION_ID, limit=3, max_bytes=4096, max_scan_lines=0)
        self.assertTrue(result["ok"])
        self.assertLess(record.stat().st_size, size + 1)
        indices = [event["payload"]["index"] for event in result["events"]]
        self.assertEqual(indices, [4997, 4998, 4999])

    def test_scan_line_cap_is_applied(self):
        self._big_session()
        store = self.store()
        store.enumerate()
        result = store.history(SESSION_ID, limit=2, max_bytes=10**7, max_scan_lines=5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(
            [event["payload"]["index"] for event in result["events"]], [4998, 4999]
        )

    def test_partial_first_line_of_the_window_is_dropped(self):
        self._big_session()
        store = self.store()
        store.enumerate()
        # A window that lands mid-record must not yield a half-record event.
        result = store.history(SESSION_ID, limit=50, max_bytes=64, max_scan_lines=0)
        self.assertTrue(result["ok"])
        for event in result["events"]:
            self.assertIn("index", event["payload"])

    def test_limit_zero_returns_no_events(self):
        self._big_session(count=5)
        store = self.store()
        store.enumerate()
        result = store.history(SESSION_ID, limit=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["returned"], 0)
        self.assertEqual(result["events"], [])

    def test_limit_is_clamped_to_the_shared_ceiling(self):
        # A caller-selected window is a bound; an unbounded window is not.
        self._big_session(count=300)
        store = self.store()
        store.enumerate()
        result = store.history(SESSION_ID, limit=10**6)
        self.assertTrue(result["ok"])
        self.assertEqual(result["returned"], MAX_HISTORY_LIMIT)
        self.assertEqual(len(result["events"]), MAX_HISTORY_LIMIT)


class FailureDetailHygieneTests(CrewStoreCase):
    """A failure detail carries bounded, redacted text, never control chars."""

    def test_path_and_exception_text_are_sanitized(self):
        self.session("hash_c/sess_c")
        self.ledger("hash_c/sess_c")
        store = self.store()
        self.assertEqual(len(store.enumerate()), 1)

        def boom(path, max_bytes, max_scan_lines):
            raise OSError("permission denied\n\x00/tmp/secret sk-abcdefghijklmnop1234")

        original = store._tail_lines
        store._tail_lines = boom
        try:
            result = store.history(SESSION_ID)
        finally:
            store._tail_lines = original
        self.assertFalse(result["ok"])
        self.assertNotIn("\n", result["detail"])
        self.assertNotIn("\x00", result["detail"])
        self.assertNotIn("sk-abcdefghijklmnop1234", result["detail"])
        self.assertLessEqual(len(result["detail"]), 2 * 512 + 64)

    def test_success_path_is_bounded(self):
        self.session("hash_d/sess_d")
        self.ledger("hash_d/sess_d")
        store = self.store()
        store.enumerate()
        result = store.history(SESSION_ID)
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["path"]), 512)


class CapabilityDeclarationTests(CrewStoreCase):
    """Capabilities state what the adapter can do, not what the record shows."""

    def test_enroll_is_unsupported_by_the_read_only_ledger(self):
        self.session("hash_e/sess_e")
        self.ledger("hash_e/sess_e")
        envelopes = self.store().enumerate()
        self.assertEqual(len(envelopes), 1)
        enroll = envelopes[0].capabilities[CAP_ENROLL]
        self.assertFalse(enroll.supported)
        self.assertIn("cannot enroll", enroll.reason)


if __name__ == "__main__":
    unittest.main()
