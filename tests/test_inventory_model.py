"""Contract tests for the normalized session envelope.

The envelope module claims to enforce three rules in code rather than in prose:

1. a pid alone never identifies a session,
2. ``observed_at`` and ``source_updated_at`` are separate fields,
3. a capability is only supported when the adapter that claimed it recorded
   the probe.

These tests pin those rules. Standard library only, no network, no access to
any real harness store.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory.model import (  # noqa: E402
    CAP_DISCOVER_SESSION,
    CAP_JOIN_LIVE,
    CAP_OBSERVE_SNAPSHOT,
    LAUNCH_HEADLESS,
    LAUNCH_INTERACTIVE,
    SOURCE_JCODE_FS,
    SOURCE_KCODE_FS,
    STATE_RUNNING,
    Capability,
    ProcessInstance,
    SessionEnvelope,
    envelope_sort_key,
    make_session_uid,
    merge_capabilities,
    now_iso,
    summarize,
)


def make_envelope(**overrides) -> SessionEnvelope:
    """A minimal, valid envelope with the given fields replaced."""
    fields = {
        "session_uid": "jcode-fs:alpha",
        "native_session_id": "alpha",
        "source": SOURCE_JCODE_FS,
        "host_id": "test-host",
        "namespace_id": "test-namespace",
        "harness_kind": "jcode",
    }
    fields.update(overrides)
    return SessionEnvelope(**fields)


class CapabilityProofTests(unittest.TestCase):
    """A capability is only supported when it carries its own evidence."""

    def test_supported_capability_without_a_probe_is_rejected(self):
        with self.assertRaises(ValueError):
            Capability(supported=True)

    def test_supported_capability_without_an_observed_at_is_rejected(self):
        with self.assertRaises(ValueError):
            Capability(supported=True, probe="ps -o lstart=")

    def test_unsupported_capability_without_a_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            Capability(supported=False)

    def test_proved_records_a_probe_and_an_observation_time(self):
        capability = Capability.proved("jcode sessions/*.json")
        self.assertTrue(capability.supported)
        self.assertEqual(capability.probe, "jcode sessions/*.json")
        self.assertTrue(capability.observed_at)
        self.assertIsNone(capability.reason)

    def test_unsupported_records_the_reason(self):
        capability = Capability.unsupported("the file store cannot resume")
        self.assertFalse(capability.supported)
        self.assertEqual(capability.reason, "the file store cannot resume")
        self.assertIsNone(capability.probe)
        self.assertIsNone(capability.observed_at)

    def test_proved_honours_an_explicit_observation_time(self):
        capability = Capability.proved("ps -o lstart=", observed_at="2026-10-20T00:00:00Z")
        self.assertEqual(capability.observed_at, "2026-10-20T00:00:00Z")

class SessionEnvelopeIdentityTests(unittest.TestCase):
    """A session without a native id is never an identity."""

    def test_empty_native_session_id_is_rejected(self):
        with self.assertRaises(ValueError):
            make_envelope(native_session_id="")

    def test_empty_session_uid_is_rejected(self):
        with self.assertRaises(ValueError):
            make_envelope(session_uid="")

    def test_undeclared_capability_is_unsupported_with_a_reason(self):
        envelope = make_envelope()
        capability = envelope.capability("never_declared")
        self.assertFalse(capability.supported)
        self.assertTrue(capability.reason)
        self.assertIn(SOURCE_JCODE_FS, capability.reason)

    def test_undeclared_capability_is_not_reported_as_supported(self):
        envelope = make_envelope()
        self.assertFalse(envelope.supports("never_declared"))

    def test_declared_capability_is_reported_as_supported(self):
        envelope = make_envelope(
            capabilities={CAP_DISCOVER_SESSION: Capability.proved("sessions/*.json")}
        )
        self.assertTrue(envelope.supports(CAP_DISCOVER_SESSION))
        self.assertTrue(envelope.capability(CAP_DISCOVER_SESSION).observed_at)


class SessionUidTests(unittest.TestCase):
    """Session uids are namespaced by the source that produced them."""

    def test_uid_joins_source_and_native_id(self):
        self.assertEqual(make_session_uid("a", "b"), "a:b")

    def test_two_sources_with_the_same_native_id_do_not_collide(self):
        from_jcode = make_session_uid(SOURCE_JCODE_FS, "shared-native-id")
        from_kcode = make_session_uid(SOURCE_KCODE_FS, "shared-native-id")
        self.assertNotEqual(from_jcode, from_kcode)

    def test_the_same_source_and_id_are_stable(self):
        self.assertEqual(
            make_session_uid(SOURCE_JCODE_FS, "alpha"),
            make_session_uid(SOURCE_JCODE_FS, "alpha"),
        )


class MergeCapabilityTests(unittest.TestCase):
    """Merging never invents support and never drops proven support."""

    def test_support_from_either_side_survives_the_merge(self):
        primary = {CAP_JOIN_LIVE: Capability.proved("acp session/load")}
        secondary = {CAP_JOIN_LIVE: Capability.unsupported("no protocol here")}
        merged = merge_capabilities(primary, secondary)
        self.assertTrue(merged[CAP_JOIN_LIVE].supported)

    def test_support_from_the_secondary_side_survives_the_merge(self):
        primary = {CAP_JOIN_LIVE: Capability.unsupported("no protocol here")}
        secondary = {CAP_JOIN_LIVE: Capability.proved("acp session/load")}
        merged = merge_capabilities(primary, secondary)
        self.assertTrue(merged[CAP_JOIN_LIVE].supported)

    def test_a_capability_neither_side_declared_stays_undeclared(self):
        merged = merge_capabilities(
            {CAP_DISCOVER_SESSION: Capability.proved("sessions/*.json")},
            {CAP_OBSERVE_SNAPSHOT: Capability.proved("sessions/*.json fields")},
        )
        self.assertNotIn(CAP_JOIN_LIVE, merged)
        envelope = make_envelope(capabilities=merged)
        self.assertFalse(envelope.capability(CAP_JOIN_LIVE).supported)
        self.assertTrue(envelope.capability(CAP_JOIN_LIVE).reason)

    def test_a_capability_declared_unsupported_by_both_stays_unsupported(self):
        reason = "the file store is observation only"
        merged = merge_capabilities(
            {CAP_JOIN_LIVE: Capability.unsupported(reason)},
            {CAP_JOIN_LIVE: Capability.unsupported(reason)},
        )
        self.assertFalse(merged[CAP_JOIN_LIVE].supported)
        self.assertTrue(merged[CAP_JOIN_LIVE].reason)

    def test_merging_does_not_mutate_either_input(self):
        primary = {CAP_JOIN_LIVE: Capability.proved("acp session/load")}
        secondary = {CAP_OBSERVE_SNAPSHOT: Capability.proved("sessions/*.json fields")}
        merge_capabilities(primary, secondary)
        self.assertEqual(list(primary), [CAP_JOIN_LIVE])
        self.assertEqual(list(secondary), [CAP_OBSERVE_SNAPSHOT])


class SortKeyTests(unittest.TestCase):
    """Two readers must see the same list order."""

    def _sample(self):
        return [
            make_envelope(
                session_uid="jcode-fs:a",
                native_session_id="a",
                source_updated_at="2026-10-20T00:01:00Z",
            ),
            make_envelope(
                session_uid="jcode-fs:b",
                native_session_id="b",
                source_updated_at="2026-10-20T00:03:00Z",
            ),
            make_envelope(
                session_uid="kcode-fs:c",
                native_session_id="c",
                source=SOURCE_KCODE_FS,
                source_updated_at="2026-10-20T00:02:00Z",
            ),
        ]

    def test_two_orderings_of_the_same_list_sort_identically(self):
        first = sorted(self._sample(), key=envelope_sort_key)
        second = sorted(list(reversed(self._sample())), key=envelope_sort_key)
        self.assertEqual(
            [env.native_session_id for env in first],
            [env.native_session_id for env in second],
        )

    def test_the_order_is_earliest_update_first(self):
        ordered = sorted(self._sample(), key=envelope_sort_key)
        self.assertEqual([env.native_session_id for env in ordered], ["a", "c", "b"])

    def test_a_missing_update_time_sorts_before_a_dated_one(self):
        undated = make_envelope(
            session_uid="jcode-fs:undated",
            native_session_id="undated",
            source_updated_at=None,
        )
        dated = make_envelope(
            session_uid="jcode-fs:dated",
            native_session_id="dated",
            source_updated_at="2026-10-20T00:01:00Z",
        )
        ordered = sorted([dated, undated], key=envelope_sort_key)
        self.assertEqual(ordered[0].native_session_id, "undated")


class SummarizeTests(unittest.TestCase):
    """The summary counts, it never collapses distinct dimensions."""

    def _sample(self):
        return [
            make_envelope(
                session_uid="jcode-fs:a",
                native_session_id="a",
                source=SOURCE_JCODE_FS,
                launch_mode=LAUNCH_INTERACTIVE,
                state=STATE_RUNNING,
            ),
            make_envelope(
                session_uid="jcode-fs:b",
                native_session_id="b",
                source=SOURCE_JCODE_FS,
                launch_mode=LAUNCH_HEADLESS,
                state=STATE_RUNNING,
            ),
            make_envelope(
                session_uid="kcode-fs:c",
                native_session_id="c",
                source=SOURCE_KCODE_FS,
                launch_mode=LAUNCH_INTERACTIVE,
                state=STATE_RUNNING,
            ),
        ]

    def test_total_matches_the_number_of_envelopes(self):
        sample = self._sample()
        self.assertEqual(summarize(sample)["total"], len(sample))

    def test_counts_are_grouped_by_source(self):
        summary = summarize(self._sample())
        self.assertEqual(summary["by_source"][SOURCE_JCODE_FS], 2)
        self.assertEqual(summary["by_source"][SOURCE_KCODE_FS], 1)

    def test_counts_are_grouped_by_launch_mode(self):
        summary = summarize(self._sample())
        self.assertEqual(summary["by_launch_mode"][LAUNCH_INTERACTIVE], 2)
        self.assertEqual(summary["by_launch_mode"][LAUNCH_HEADLESS], 1)

    def test_counts_are_grouped_by_state(self):
        summary = summarize(self._sample())
        self.assertEqual(summary["by_state"][STATE_RUNNING], 3)

    def test_an_empty_list_summarizes_to_zero(self):
        summary = summarize([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["by_source"], {})


class SerializationTests(unittest.TestCase):
    """The projection survives a JSON round trip."""

    def test_to_dict_round_trips_through_json(self):
        envelope = make_envelope(
            capabilities={
                CAP_DISCOVER_SESSION: Capability.proved("jcode sessions/*.json"),
                CAP_JOIN_LIVE: Capability.unsupported("observation only"),
            },
            process_instance=ProcessInstance(
                pid=41001,
                start_identity="Tue Oct 27 23:39:05 2026",
                boot_id="sec = 1792424285",
                executable_path="/usr/local/bin/jcode",
            ),
            launch_mode=LAUNCH_INTERACTIVE,
            workspace="/tmp/fixture/repo-one",
            title="Interactive fixture session",
            source_updated_at="2026-10-20T00:05:00.000000Z",
        )
        encoded = json.dumps(envelope.to_dict())
        decoded = json.loads(encoded)
        self.assertEqual(decoded["session_uid"], envelope.session_uid)
        self.assertEqual(decoded["native_session_id"], "alpha")
        self.assertEqual(decoded["launch_mode"], LAUNCH_INTERACTIVE)
        self.assertEqual(decoded["process_instance"]["pid"], 41001)
        self.assertTrue(decoded["capabilities"][CAP_DISCOVER_SESSION]["supported"])
        self.assertFalse(decoded["capabilities"][CAP_JOIN_LIVE]["supported"])
        self.assertEqual(decoded["capabilities"][CAP_JOIN_LIVE]["reason"], "observation only")

    def test_to_dict_keeps_observed_at_and_source_updated_at_apart(self):
        envelope = make_envelope(
            observed_at="2026-10-20T00:09:00Z",
            source_updated_at="2026-10-20T00:05:00Z",
        )
        data = envelope.to_dict()
        self.assertEqual(data["observed_at"], "2026-10-20T00:09:00Z")
        self.assertEqual(data["source_updated_at"], "2026-10-20T00:05:00Z")
        self.assertNotEqual(data["observed_at"], data["source_updated_at"])


class ObservationTimeTests(unittest.TestCase):
    """``observed_at`` is always UTC, whatever the local timezone says."""

    def test_observed_at_is_utc_regardless_of_the_timezone(self):
        with S.env_vars(TZ="Asia/Tokyo"):
            stamp = now_iso()
        self.assertTrue(stamp.endswith("Z"))
        self.assertIn("T", stamp)

    def test_observed_at_is_written_to_disk_and_read_back(self):
        with S.temp_dir() as tmp:
            envelope = make_envelope(observed_at="2026-10-20T00:09:00Z")
            path = os.path.join(str(tmp), "envelope.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope.to_dict()))
            with open(path, "r", encoding="utf-8") as handle:
                decoded = json.load(handle)
        self.assertEqual(decoded["observed_at"], "2026-10-20T00:09:00Z")

if __name__ == "__main__":
    unittest.main()
