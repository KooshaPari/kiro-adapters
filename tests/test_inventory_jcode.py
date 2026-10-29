"""The jcode on-disk store adapter, read against a fixture store.

Every test builds its own store in a temporary directory. The real
``~/.jcode`` and ``~/.kcode`` trees are never read, never written and never
even referenced: the store directory is always passed in by the caller.

The committed fixture tree under ``tests/fixtures/jcode_store`` describes the
store layout. The tests copy it into a temp directory and then bend it, so
the fixture stays readable and the tests stay independent.

Standard library only. No network.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory.jcode_fs import (  # noqa: E402
    DEFAULT_MAX_RECORD_BYTES,
    MAX_HISTORY_LIMIT,
    JcodeStore,
)
from kiro_adapters.inventory import jcode_fs  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    CAP_DISCOVER_SESSION,
    CAP_ENROLL,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    CAP_READ_HISTORY,
    LAUNCH_HEADLESS,
    LAUNCH_INTERACTIVE,
    LAUNCH_UNKNOWN,
    SOURCE_JCODE_FS,
    STATE_EXITED,
    STATE_STALE,
)

FIXTURE_STORE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "jcode_store"
)

ALPHA = "session_alpha_1700000000000_aaaaaaaaaaaaaaaaaaaaaaaa"
BETA = "session_beta_1700000100000_bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
GAMMA = "session_gamma_1700000200000_cccccccccccccccccccccccccccc"
DELTA = "session_delta_1700000300000_dddddddddddddddddddddddddddd"
EPSILON = "session_epsilon_1700000400000_eeeeeeeeeeeeeeeeeeeeeeeeeeee"

# A pid that is below the ceiling on every supported platform and is not in
# the process table. It is a clean "the process is gone" negative control.
DEAD_PID = 41999

FAKE_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def copy_fixture_store(root):
    """Copy the committed fixture tree into ``root`` and return its path."""
    target = os.path.join(str(root), "jcode_store")
    shutil.copytree(FIXTURE_STORE, target)
    return target


def build_empty_store(root):
    """Create a bare store skeleton and return its path."""
    target = os.path.join(str(root), "store")
    for child in ("sessions", "active_pids", "client_sessions"):
        os.makedirs(os.path.join(target, child), exist_ok=True)
    return target


def write_session(store_dir, native_id, record=None, pid=None):
    """Write one session record, and optionally its active pid entry."""
    body = {
        "id": native_id,
        "title": "generated fixture",
        "created_at": "2026-10-20T00:00:00.000000Z",
        "updated_at": "2026-10-20T00:05:00.000000Z",
        "provider_key": "openai-compatible",
        "model": "MiniMax-M3",
        "working_dir": "/tmp/fixture/repo-one",
        "short_name": native_id,
        "status": "Active",
        "last_pid": pid,
        "last_active_at": "2026-10-20T00:05:00.000000Z",
        "is_debug": False,
        "saved": False,
    }
    if record is not None:
        body.update(record)
    path = os.path.join(store_dir, "sessions", native_id + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle)
    if pid is not None:
        pid_path = os.path.join(store_dir, "active_pids", native_id)
        with open(pid_path, "w", encoding="utf-8") as handle:
            handle.write(str(pid))
    return native_id


def envelope_for(envelopes, native_id):
    """The one envelope with this native id, or a clear failure."""
    matches = [e for e in envelopes if e.native_session_id == native_id]
    assert len(matches) == 1, "expected one envelope for {}, got {}".format(
        native_id, len(matches)
    )
    return matches[0]


class LaunchModeTests(unittest.TestCase):
    """Launch artifacts are hints; the file store cannot prove a mode."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_a_client_binding_does_not_prove_interactive(self):
        # A client_sessions entry is a hint, not proof: the mode stays unknown
        # and the artifact is surfaced separately as evidence.
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        self.assertEqual(envelope.launch_mode, LAUNCH_UNKNOWN)
        self.assertTrue(
            any("client_sessions" in hint for hint in envelope.launch_hints),
            "expected a client_sessions hint, got {}".format(envelope.launch_hints),
        )

    def test_an_input_session_artifact_does_not_prove_headless(self):
        envelope = envelope_for(self.store.enumerate(), BETA)
        self.assertEqual(envelope.launch_mode, LAUNCH_UNKNOWN)
        self.assertTrue(
            any("client-input-session" in hint for hint in envelope.launch_hints),
            "expected an input-session hint, got {}".format(envelope.launch_hints),
        )

    def test_a_session_with_neither_artifact_is_unknown_with_no_hints(self):
        envelope = envelope_for(self.store.enumerate(), EPSILON)
        self.assertEqual(envelope.launch_mode, LAUNCH_UNKNOWN)
        self.assertEqual(envelope.launch_hints, ())

    def test_no_session_in_the_fixture_store_claims_a_launch_mode(self):
        for envelope in self.store.enumerate():
            self.assertEqual(
                envelope.launch_mode,
                LAUNCH_UNKNOWN,
                "{} claimed mode {}".format(
                    envelope.native_session_id, envelope.launch_mode
                ),
            )


class IdentityTests(unittest.TestCase):
    """One cwd can hold many sessions, and one pid can serve many sessions."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_two_sessions_in_one_cwd_keep_distinct_identities(self):
        envelopes = self.store.enumerate()
        alpha = envelope_for(envelopes, ALPHA)
        beta = envelope_for(envelopes, BETA)
        self.assertEqual(alpha.workspace, beta.workspace)
        self.assertNotEqual(alpha.session_uid, beta.session_uid)
        self.assertNotEqual(alpha.native_session_id, beta.native_session_id)

    def test_the_session_uid_is_namespaced_by_the_source(self):
        envelopes = self.store.enumerate()
        alpha = envelope_for(envelopes, ALPHA)
        self.assertEqual(alpha.session_uid, "{}:{}".format(SOURCE_JCODE_FS, ALPHA))

    def test_two_sessions_served_by_one_pid_are_both_enumerated(self):
        envelopes = self.store.enumerate()
        alpha = envelope_for(envelopes, ALPHA)
        gamma = envelope_for(envelopes, GAMMA)
        self.assertNotEqual(alpha.session_uid, gamma.session_uid)
        self.assertNotEqual(alpha.native_session_id, gamma.native_session_id)

    def test_two_sessions_served_by_one_live_pid_report_that_pid(self):
        """A pid is only visible on the envelope through a live process."""
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            live = os.getpid()
            write_session(store_dir, "session_live_one", pid=live)
            write_session(store_dir, "session_live_two", pid=live)
            envelopes = JcodeStore(store_dir).enumerate()
        self.assertEqual(len(envelopes), 2)
        for envelope in envelopes:
            self.assertIsNotNone(envelope.process_instance)
            self.assertEqual(envelope.process_instance.pid, live)

    def test_a_session_without_a_record_id_uses_the_file_stem(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, "session_without_an_id", record={"id": None})
            envelopes = JcodeStore(store_dir).enumerate()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].native_session_id, "session_without_an_id")


class StateTests(unittest.TestCase):
    """A missing heartbeat is not proof of exit."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_a_session_with_no_active_pid_is_stale(self):
        envelopes = self.store.enumerate()
        self.assertEqual(envelope_for(envelopes, DELTA).state, STATE_STALE)

    def test_a_session_whose_pid_is_gone_is_exited(self):
        envelopes = self.store.enumerate()
        self.assertEqual(envelope_for(envelopes, EPSILON).state, STATE_EXITED)

    def test_a_dead_pid_carries_no_process_identity(self):
        envelopes = self.store.enumerate()
        self.assertIsNone(envelope_for(envelopes, EPSILON).process_instance)

    def test_a_dead_pid_is_still_exited_when_the_pid_is_in_range(self):
        """The verdict does not depend on the pid being out of range."""
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, "session_dead", pid=DEAD_PID)
            envelopes = JcodeStore(store_dir).enumerate()
        self.assertEqual(len(envelopes), 1)
        self.assertIsNone(envelopes[0].process_instance)
        self.assertEqual(envelopes[0].state, STATE_EXITED)


class RobustnessTests(unittest.TestCase):
    """One broken record must not take the whole inventory down."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store_dir = copy_fixture_store(self.root)
        self.store = JcodeStore(self.store_dir)

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_a_malformed_record_does_not_break_the_inventory(self):
        broken = os.path.join(self.store_dir, "sessions", "session_broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json at all")
        envelopes = self.store.enumerate()
        ids = [envelope.native_session_id for envelope in envelopes]
        self.assertNotIn("session_broken", ids)
        self.assertIn(ALPHA, ids)
        self.assertIn(BETA, ids)

    def test_an_empty_store_enumerates_to_nothing(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            self.assertEqual(JcodeStore(store_dir).enumerate(), [])

    def test_a_missing_store_enumerates_to_nothing(self):
        with S.temp_dir() as root:
            missing = JcodeStore(os.path.join(str(root), "not_a_store"))
            self.assertEqual(missing.enumerate(), [])

    def test_exists_is_false_for_a_missing_store(self):
        with S.temp_dir() as root:
            missing = JcodeStore(os.path.join(str(root), "not_a_store"))
            self.assertFalse(missing.exists())

    def test_exists_is_true_for_the_fixture_store(self):
        self.assertTrue(self.store.exists())


class RecordSizeCapTests(unittest.TestCase):
    """A record larger than the byte cap is refused, never half-parsed."""

    # Small enough to exercise the cap without writing megabytes in a test.
    CAP = 512

    def _write_oversized_record(self, store_dir, native_id, size):
        """A syntactically valid record padded past ``size`` bytes."""
        path = os.path.join(store_dir, "sessions", native_id + ".json")
        body = {
            "id": native_id,
            "title": "oversized fixture",
            "status": "Active",
            "messages": [{"id": "m1", "role": "user", "content": "x" * size}],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        return path

    def test_an_oversized_record_is_skipped_by_enumeration(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, "session_small")
            oversized = self._write_oversized_record(
                store_dir, "session_huge", self.CAP * 4
            )
            self.assertGreater(os.path.getsize(oversized), self.CAP)
            envelopes = JcodeStore(store_dir, max_record_bytes=self.CAP).enumerate()
        ids = [envelope.native_session_id for envelope in envelopes]
        self.assertNotIn("session_huge", ids)
        # Positive control: the cap must not hide records that fit.
        self.assertIn("session_small", ids)

    def test_history_of_an_oversized_record_fails_closed(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            self._write_oversized_record(store_dir, "session_huge", self.CAP * 4)
            result = JcodeStore(
                store_dir, max_record_bytes=self.CAP
            ).history("session_huge", limit=10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["messages"], [])
        # No partial transcript leaked out of the refused read.
        self.assertNotIn("returned", result)

    def test_a_record_exactly_at_the_cap_is_still_read(self):
        """Boundary: the cap is a maximum, not a strict upper bound."""
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            path = write_session(store_dir, "session_at_cap") + ".json"
            full = os.path.join(store_dir, "sessions", path)
            size = os.path.getsize(full)
            store = JcodeStore(store_dir, max_record_bytes=size)
            record = store._read_record(store.sessions_dir / path)
            envelopes = store.enumerate()
        self.assertIsNotNone(record)
        self.assertEqual([e.native_session_id for e in envelopes], ["session_at_cap"])

    def test_a_zero_cap_reads_nothing(self):
        """Fail-closed at the degenerate bound rather than reading unbounded."""
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, "session_any")
            envelopes = JcodeStore(store_dir, max_record_bytes=0).enumerate()
        self.assertEqual(envelopes, [])

    def test_the_default_cap_still_reads_ordinary_records(self):
        """The guard must not change behaviour for normal stores."""
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, "session_normal")
            envelopes = JcodeStore(store_dir).enumerate()
        self.assertEqual([e.native_session_id for e in envelopes], ["session_normal"])
        self.assertGreater(DEFAULT_MAX_RECORD_BYTES, self.CAP)

    def test_the_fixture_store_is_unaffected_by_the_cap(self):
        with S.temp_dir() as root:
            store = JcodeStore(copy_fixture_store(root))
            ids = [e.native_session_id for e in store.enumerate()]
            history = store.history(ALPHA, limit=2)
        self.assertIn(ALPHA, ids)
        self.assertTrue(history["ok"])


class ProjectionTests(unittest.TestCase):
    """The inventory is a projection, not a transcript dump."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_the_fixture_record_really_does_carry_a_transcript(self):
        """Guard against a vacuous projection test."""
        path = os.path.join(self.store.store_dir, "sessions", ALPHA + ".json")
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertIn("messages", record)
        self.assertIn("env_snapshots", record)

    def test_the_projection_carries_no_transcript(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        data = envelope.to_dict()
        self.assertNotIn("messages", data)
        self.assertNotIn("env_snapshots", data)

    def test_the_projection_carries_the_identity_fields(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        self.assertEqual(envelope.title, "Interactive fixture session")
        self.assertEqual(envelope.workspace, "/tmp/fixture/repo-one")
        self.assertEqual(envelope.evidence_ref, "jcode-fs:{}".format(ALPHA))

    def test_source_updated_at_comes_from_the_record(self):
        envelopes = self.store.enumerate()
        envelope = envelope_for(envelopes, ALPHA)
        self.assertEqual(envelope.source_updated_at, "2026-10-20T00:05:00.000000Z")

    def test_observed_at_is_present_and_separate_from_source_updated_at(self):
        envelopes = self.store.enumerate()
        envelope = envelope_for(envelopes, ALPHA)
        self.assertTrue(envelope.observed_at)
        self.assertNotEqual(envelope.observed_at, envelope.source_updated_at)

    def test_no_session_is_externally_writable_from_file_evidence(self):
        # A client_sessions file naming a pid is not proof that a human can
        # type into the session, so nothing here is externally writable.
        for envelope in self.store.enumerate():
            self.assertFalse(
                envelope.externally_writable,
                "{} was marked externally writable from file hints".format(
                    envelope.native_session_id
                ),
            )

    def test_launch_hints_are_serialized_as_a_list(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        data = envelope.to_dict()
        self.assertIsInstance(data["launch_hints"], list)
        self.assertTrue(data["launch_hints"])


class CapabilityTests(unittest.TestCase):
    """Capabilities carry the evidence that backs them."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_joining_a_live_session_is_declared_unsupported_with_a_reason(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        capability = envelope.capability(CAP_JOIN_LIVE)
        self.assertFalse(capability.supported)
        self.assertTrue(capability.reason)

    def test_discovering_a_session_is_declared_supported_with_a_probe(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        capability = envelope.capability(CAP_DISCOVER_SESSION)
        self.assertTrue(capability.supported)
        self.assertTrue(capability.probe)
        self.assertTrue(capability.observed_at)

    def test_reading_history_is_declared_supported_with_a_probe(self):
        envelope = envelope_for(self.store.enumerate(), ALPHA)
        capability = envelope.capability(CAP_READ_HISTORY)
        self.assertTrue(capability.supported)
        self.assertTrue(capability.probe)


class HistoryTests(unittest.TestCase):
    """History is bounded and redacted."""

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(copy_fixture_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_history_is_bounded_by_the_limit(self):
        result = self.store.history(ALPHA, limit=2)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["returned"], 2)

    def test_history_reports_the_full_message_count(self):
        result = self.store.history(ALPHA, limit=2)
        self.assertEqual(result["total_messages"], 4)
        self.assertGreater(result["total_messages"], len(result["messages"]))

    def test_history_redacts_a_secret_in_the_transcript(self):
        result = self.store.history(ALPHA, limit=10)
        self.assertNotIn(FAKE_SECRET, json.dumps(result))

    def test_history_of_an_unknown_session_fails_cleanly(self):
        result = self.store.history("session_that_does_not_exist")
        self.assertFalse(result["ok"])
        self.assertEqual(result["messages"], [])

    # -- path confinement --------------------------------------------------
    def _write_outside_record(self):
        """A valid-looking transcript that lives outside the store."""
        outside = os.path.join(self.root, "outside.json")
        with open(outside, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "id": "outside",
                    "messages": [
                        {"id": "m1", "role": "user", "content": "leak " + FAKE_SECRET}
                    ],
                },
                handle,
            )
        return outside

    def _assert_refused(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(result["messages"], [])
        self.assertNotIn(FAKE_SECRET, json.dumps(result))

    def test_history_refuses_a_parent_traversal_id(self):
        outside = self._write_outside_record()
        self.assertTrue(os.path.isfile(outside))
        self._assert_refused(self.store.history("../../outside"))

    def test_history_refuses_a_nested_traversal_id(self):
        self._write_outside_record()
        self._assert_refused(self.store.history("../outside"))

    def test_history_refuses_an_absolute_path_id(self):
        outside = self._write_outside_record()
        self._assert_refused(self.store.history(os.path.splitext(outside)[0]))

    def test_history_refuses_a_separator_in_the_id(self):
        self._assert_refused(self.store.history("sub/" + ALPHA))

    def test_history_refuses_a_symlink_that_leaves_the_store(self):
        outside = self._write_outside_record()
        link = os.path.join(self.store.sessions_dir, "session_evil.json")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this host")
        self._assert_refused(self.store.history("session_evil"))

    def test_history_still_reads_a_record_inside_the_store(self):
        # Positive control: the confinement must not break the normal case.
        result = self.store.history(ALPHA, limit=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_messages"], 4)


class ReadOutcomeCapabilityTests(unittest.TestCase):
    """H2: a refused or capped read proves nothing about the record."""

    RECORD_CAPS = (CAP_READ_HISTORY, CAP_OBSERVE_SNAPSHOT)

    def setUp(self):
        self.tmp = S.temp_dir()
        self.root = self.tmp.__enter__()
        self.store = JcodeStore(build_empty_store(self.root))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_a_refused_read_does_not_prove_a_record_capability(self):
        caps = self.store._capabilities(None, None, record_read=False)
        for name in self.RECORD_CAPS:
            capability = caps[name]
            self.assertFalse(capability.supported, name)
            self.assertTrue(capability.reason, name)
            self.assertIsNone(capability.probe, name)

    def test_an_observed_read_still_proves_a_record_capability(self):
        caps = self.store._capabilities(None, None, record_read=True)
        for name in self.RECORD_CAPS:
            self.assertTrue(caps[name].supported, name)
            self.assertTrue(caps[name].probe, name)

    def test_enroll_stays_unsupported_even_when_the_record_was_observed(self):
        # A read-only file store cannot enroll an existing session; enrollment
        # belongs to the harness's own session path, so a successful record
        # read must not promote CAP_ENROLL.
        for record_read in (False, True):
            caps = self.store._capabilities(None, None, record_read=record_read)
            enroll = caps[CAP_ENROLL]
            self.assertFalse(enroll.supported, record_read)
            self.assertIsNone(enroll.probe, record_read)
            self.assertIn("enrollment requires", enroll.reason)

    def test_a_capped_store_emits_no_session_so_no_probe_is_claimed(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, ALPHA, record={"messages": []})
            envelopes = JcodeStore(store_dir, max_record_bytes=0).enumerate()
        self.assertEqual(envelopes, [])


class BoundTests(unittest.TestCase):
    """M1/M2/M5: caller-selected and directory-driven sizes are capped."""

    def test_the_history_limit_is_capped(self):
        total = MAX_HISTORY_LIMIT + 50
        messages = [
            {"id": "m{}".format(index), "role": "user", "content": "x"}
            for index in range(total)
        ]
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(store_dir, ALPHA, record={"messages": messages})
            result = JcodeStore(store_dir).history(ALPHA, limit=10_000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["returned"], MAX_HISTORY_LIMIT)
        self.assertEqual(result["total_messages"], total)

    def test_a_negative_history_limit_returns_no_messages(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            write_session(
                store_dir,
                ALPHA,
                record={"messages": [{"id": "m1", "role": "user", "content": "x"}]},
            )
            result = JcodeStore(store_dir).history(ALPHA, limit=-5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["returned"], 0)

    def test_session_file_enumeration_stops_at_the_ceiling(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            for index in range(5):
                write_session(store_dir, "session_{}".format(index))
            with mock.patch.object(jcode_fs, "MAX_SESSION_FILES", 2):
                files = JcodeStore(store_dir)._session_files()
        self.assertEqual(len(files), 2)

    def test_the_input_session_hint_scan_stops_at_the_ceiling(self):
        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            for index in range(4):
                path = os.path.join(
                    store_dir, "client-input-session_{}".format(index)
                )
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"session": "session_{}".format(index)}, handle)
            with mock.patch.object(jcode_fs, "MAX_INPUT_SESSION_HINTS", 1):
                found = JcodeStore(store_dir)._input_session_hints()
        self.assertEqual(len(found), 1)


class UnverifiableProcessIdentityTests(unittest.TestCase):
    """A pid file is a claimed pointer; the envelope never promotes it.

    Synthetic only: the store is a temporary directory and the process
    probes are stubbed, so no real session or host process is read.
    """

    def _envelope_for(self, verdict):
        import unittest.mock as mock

        from kiro_adapters.inventory import processes
        from kiro_adapters.inventory.model import STATE_UNKNOWN

        with S.temp_dir() as root:
            store_dir = build_empty_store(root)
            live = os.getpid()
            write_session(store_dir, "session_unbound", pid=live)
            with mock.patch.object(processes, "describe_many", return_value={}):
                with mock.patch.object(
                    processes, "liveness_many", return_value={live: verdict}
                ):
                    envelopes = JcodeStore(store_dir).enumerate()
        self.assertEqual(len(envelopes), 1)
        return envelopes[0], STATE_UNKNOWN

    def test_a_live_but_unidentifiable_pid_carries_no_process_identity(self):
        from kiro_adapters.inventory import processes

        envelope, _ = self._envelope_for(processes.ALIVE)
        self.assertIsNone(envelope.process_instance)

    def test_a_live_but_unidentifiable_pid_is_not_reported_exited(self):
        from kiro_adapters.inventory import processes
        from kiro_adapters.inventory.model import STATE_EXITED

        envelope, _ = self._envelope_for(processes.ALIVE)
        self.assertNotEqual(envelope.state, STATE_EXITED)

    def test_an_unanswerable_probe_is_unknown_not_exited(self):
        from kiro_adapters.inventory import processes
        from kiro_adapters.inventory.model import STATE_EXITED

        envelope, state_unknown = self._envelope_for(processes.UNKNOWN)
        self.assertIsNone(envelope.process_instance)
        self.assertEqual(envelope.state, state_unknown)
        self.assertNotEqual(envelope.state, STATE_EXITED)


if __name__ == "__main__":
    unittest.main()
