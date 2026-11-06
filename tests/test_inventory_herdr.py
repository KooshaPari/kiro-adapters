"""HERDR's enrolled-agent snapshot is read safely, or not at all.

HERDR's one structured read is ``herdr api snapshot``, a CLI over a live
socket. These tests pin four things:

* the envelope really is built from the observed payload shape
  (``result.snapshot.agents``), with the fields the other readers emit;
* every failure mode -- missing binary, stopped server, timeout, oversized
  stdout, malformed JSON, a payload with no ``agents`` list -- degrades to
  zero sessions rather than raising;
* fields the adapter does not project (``tokens``, unknown keys) never reach
  an envelope, so a credential-shaped value cannot leak;
* the enrollment limitation survives into both the capability detail and the
  evidence line, and the source lives in ``CLI_SOURCES``, not in
  ``KNOWN_SOURCES``.

The whole suite runs with HERDR absent and with no socket: every read is
answered by an injected runner, except the one test that asks a store pointed
at an empty environment to find nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402,F401  (shared support import, parity with siblings)

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import herdr_fs, herdr_paths, registry  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    CAP_DISCOVER_SESSION,
    CAP_READ_HISTORY,
    SOURCE_HERDR_PANE,
    STATE_UNKNOWN,
    STATE_WORKING,
)


def _agent(pane_id: str = "wA:pH", status: str = "working") -> dict:
    """One ``agents`` entry in the observed shape."""
    return {
        "agent": "jcode",
        "agent_status": status,
        "cwd": "/work/repo",
        "focused": True,
        "foreground_cwd": "/work/repo",
        "pane_id": pane_id,
        "revision": 7,
        "state_change_seq": 41,
        "tab_id": "wA:t1",
        "terminal_id": "term-0123456789abcd",
        "terminal_title": "jcode [working]",
        "terminal_title_stripped": "jcode [working]",
        "tokens": {"mach": "host-a"},
        "workspace_id": "wA",
    }


def _snapshot(agents=None, version: str = "0.9.3") -> dict:
    """The observed envelope: ``{"id": ..., "result": {"snapshot": {...}}}``."""
    return {
        "id": "req-1",
        "result": {
            "type": "snapshot",
            "snapshot": {
                "agents": [_agent()] if agents is None else agents,
                "focused_pane_id": "wA:pH",
                "focused_tab_id": "wA:t1",
                "focused_workspace_id": "wA",
                "layouts": [],
                "panes": [],
                "protocol": 22,
                "tabs": [],
                "version": version,
                "workspaces": [],
            },
        },
    }


def _store(output, **kwargs) -> herdr_fs.HerdrStore:
    """A store whose one read returns ``output`` (or raises it, if callable)."""
    if callable(output):
        runner = output
    else:
        runner = lambda argv: output  # noqa: E731
    return herdr_fs.HerdrStore(binary="/fake/herdr", runner=runner, **({} or kwargs))


class ValidPayloadCase(unittest.TestCase):
    def test_one_agent_becomes_one_envelope(self):
        envelopes = _store(json.dumps(_snapshot())).enumerate()
        self.assertEqual(len(envelopes), 1)
        envelope = envelopes[0]
        self.assertEqual(envelope.source, SOURCE_HERDR_PANE)
        self.assertEqual(envelope.session_uid, "herdr-pane:wA:pH")
        self.assertEqual(envelope.native_session_id, "wA:pH")
        self.assertEqual(envelope.harness_kind, "jcode")
        self.assertEqual(envelope.harness_version, "0.9.3")
        self.assertEqual(envelope.workspace, "/work/repo")
        self.assertEqual(envelope.title, "jcode [working]")
        self.assertEqual(envelope.state, STATE_WORKING)
        # A snapshot records no pid we probed and no update time we observed.
        self.assertIsNone(envelope.process_instance)
        self.assertIsNone(envelope.source_updated_at)
        self.assertTrue(envelope.observed_at)

    def test_argv_is_binary_plus_snapshot_verb_with_no_shell(self):
        seen = []

        def runner(argv):
            seen.append(list(argv))
            return json.dumps(_snapshot())

        _store(runner).enumerate()
        self.assertEqual(seen, [["/fake/herdr", "api", "snapshot"]])

    def test_bare_snapshot_dict_is_also_accepted(self):
        payload = _snapshot().pop("result")
        envelopes = _store(json.dumps(payload["snapshot"])).enumerate()
        self.assertEqual(len(envelopes), 1)

    def test_limit_bounds_the_result(self):
        agents = [_agent("wA:p{}".format(i)) for i in range(10)]
        envelopes = _store(json.dumps(_snapshot(agents))).enumerate(limit=3)
        self.assertEqual(len(envelopes), 3)

    def test_unrecognised_state_word_is_unknown_but_preserved_in_evidence(self):
        payload = _snapshot([_agent(status="teleporting")])
        envelope = _store(json.dumps(payload)).enumerate()[0]
        self.assertEqual(envelope.state, STATE_UNKNOWN)
        self.assertIn("teleporting", envelope.evidence_ref)

    def test_entry_without_pane_id_is_skipped(self):
        broken = _agent()
        broken.pop("pane_id")
        envelopes = _store(json.dumps(_snapshot([broken]))).enumerate()
        self.assertEqual(envelopes, [])


class EnrollmentScopeCase(unittest.TestCase):
    def test_scope_note_is_in_the_proved_capability(self):
        envelope = _store(json.dumps(_snapshot())).enumerate()[0]
        capability = envelope.capabilities[CAP_DISCOVER_SESSION]
        self.assertTrue(capability.supported)
        self.assertIn("herdr-enrolled", capability.probe or "")

    def test_scope_note_is_in_the_evidence_line(self):
        envelope = _store(json.dumps(_snapshot())).enumerate()[0]
        self.assertIn(herdr_paths.ENROLLMENT_SCOPE_NOTE, envelope.evidence_ref)

    def test_history_is_a_declared_refusal_not_a_transcript(self):
        envelope = _store(json.dumps(_snapshot())).enumerate()[0]
        self.assertFalse(envelope.capabilities[CAP_READ_HISTORY].supported)
        result = _store(json.dumps(_snapshot())).history("wA:pH")
        self.assertFalse(result["ok"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["returned"], 0)


class EmptyAndMalformedCase(unittest.TestCase):
    def test_empty_agents_list(self):
        self.assertEqual(_store(json.dumps(_snapshot([]))).enumerate(), [])

    def test_empty_stdout(self):
        self.assertEqual(_store("").enumerate(), [])
        self.assertEqual(_store("   \n").enumerate(), [])

    def test_malformed_json(self):
        for payload in ("{", "not json", "null", "[]", '"text"'):
            self.assertEqual(_store(payload).enumerate(), [], msg=payload)

    def test_payload_without_snapshot_object(self):
        self.assertEqual(_store(json.dumps({"id": "x", "result": {}})).enumerate(), [])

    def test_agents_not_a_list(self):
        payload = _snapshot()
        payload["result"]["snapshot"]["agents"] = {"oops": True}
        self.assertEqual(_store(json.dumps(payload)).enumerate(), [])

    def test_non_dict_agent_records_are_skipped(self):
        payload = _snapshot(["string", 3, None, _agent()])
        self.assertEqual(len(_store(json.dumps(payload)).enumerate()), 1)


class FailureModesCase(unittest.TestCase):
    def test_missing_binary_contributes_nothing(self):
        store = herdr_fs.HerdrStore(environ={"PATH": "/nonexistent"})
        self.assertFalse(store.exists())
        self.assertIsNone(store.binary)
        self.assertEqual(store.enumerate(), [])

    def test_missing_binary_ignores_injected_runner_output(self):
        # No binary means no argv can be built, so even a yielding runner is
        # never consulted: absence of the executable is not a parse problem.
        called = []
        store = herdr_fs.HerdrStore(
            environ={"PATH": "/nonexistent"},
            runner=lambda argv: called.append(argv) or json.dumps(_snapshot()),
        )
        self.assertEqual(store.enumerate(), [])
        self.assertEqual(called, [])

    def test_stopped_server_exits_nonzero(self):
        def runner(argv):
            raise OSError("herdr exited 1")

        self.assertEqual(_store(runner).enumerate(), [])

    def test_timeout_degrades_to_zero(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5.0)

        self.assertEqual(_store(runner).enumerate(), [])

    def test_unexpected_runner_exception_does_not_escape(self):
        def runner(argv):
            raise RuntimeError("boom")

        self.assertEqual(_store(runner).enumerate(), [])

    def test_oversized_output_is_refused(self):
        payload = json.dumps(_snapshot())
        store = herdr_fs.HerdrStore(
            binary="/fake/herdr",
            runner=lambda argv: payload,
            max_stdout=32,
        )
        self.assertEqual(store.enumerate(), [])

    def test_non_string_runner_output_is_refused(self):
        self.assertEqual(_store(lambda argv: None).enumerate(), [])
        self.assertEqual(_store(lambda argv: b"{}").enumerate(), [])

    def test_timeout_and_binary_resolution_precedence(self):
        self.assertEqual(herdr_paths.resolve_timeout("2.5", {}), 2.5)
        self.assertEqual(
            herdr_paths.resolve_timeout("999", {}), herdr_paths.MAX_TIMEOUT_SECS
        )
        self.assertEqual(
            herdr_paths.resolve_timeout("nope", {}), herdr_paths.DEFAULT_TIMEOUT_SECS
        )
        self.assertEqual(
            herdr_paths.resolve_timeout("0", {}), herdr_paths.DEFAULT_TIMEOUT_SECS
        )
        self.assertIsNone(herdr_paths.resolve_binary("", {"PATH": "/nonexistent"}))
        self.assertEqual(
            herdr_paths.resolve_binary("/fake/herdr", {"PATH": "/nonexistent"}),
            "/fake/herdr",
        )


class RedactionCase(unittest.TestCase):
    def test_unprojected_mappings_and_unknown_keys_never_reach_the_envelope(self):
        agent = _agent()
        agent["tokens"] = {"mach": "host-a", "api_key": "sk-live-DEADBEEF"}
        agent["scroll"] = {"buffer": "SECRET-TERMINAL-DUMP"}
        agent["resume"] = {"argv": ["herdr", "--token", "sk-live-DEADBEEF"]}
        envelope = _store(json.dumps(_snapshot([agent]))).enumerate()[0]
        rendered = repr(envelope)
        for needle in ("DEADBEEF", "SECRET-TERMINAL-DUMP", "api_key", "tokens", "scroll"):
            self.assertNotIn(needle, rendered)

    def test_control_characters_in_title_are_stripped(self):
        agent = _agent()
        agent["terminal_title_stripped"] = "ok\x1b[31mred\x07"
        envelope = _store(json.dumps(_snapshot([agent]))).enumerate()[0]
        self.assertNotIn("\x1b", envelope.title or "")
        self.assertNotIn("\x07", envelope.title or "")

    def test_allowlist_is_an_allowlist(self):
        # Every projected key is a scalar field the adapter understands; the
        # mapping-valued keys are absent by construction.
        self.assertNotIn("tokens", herdr_paths.PROJECTED_KEYS)
        self.assertNotIn("scroll", herdr_paths.PROJECTED_KEYS)
        self.assertIn("pane_id", herdr_paths.PROJECTED_KEYS)


class RegistryPlacementCase(unittest.TestCase):
    def test_herdr_is_cli_backed_not_file_backed(self):
        self.assertIn(SOURCE_HERDR_PANE, registry.CLI_SOURCES)
        self.assertNotIn(SOURCE_HERDR_PANE, registry.KNOWN_SOURCES)
        self.assertNotIn(SOURCE_HERDR_PANE, registry.FILE_SOURCES)

    def test_default_discovery_never_touches_herdr(self):
        with S.no_real_stores():
            called = []

            def factory(**kwargs):
                return herdr_fs.HerdrStore(
                    binary="/fake/herdr",
                    runner=lambda argv: called.append(argv) or json.dumps(_snapshot()),
                    **kwargs
                )

            with mock.patch.object(registry.herdr_fs, "default_store", factory):
                envelopes = registry.enumerate_sessions()
            self.assertEqual(called, [])
            self.assertNotIn(SOURCE_HERDR_PANE, {e.source for e in envelopes})

    def test_opt_in_surfaces_herdr_envelopes(self):
        with S.no_real_stores():
            def factory(**kwargs):
                return herdr_fs.HerdrStore(
                    binary="/fake/herdr",
                    runner=lambda argv: json.dumps(_snapshot()),
                    **kwargs
                )

            with mock.patch.object(registry.herdr_fs, "default_store", factory):
                envelopes = registry.enumerate_sessions(include_cli=True)
            herdr = [e for e in envelopes if e.source == SOURCE_HERDR_PANE]
            self.assertEqual(len(herdr), 1)
            self.assertEqual(herdr[0].session_uid, "herdr-pane:wA:pH")

    def test_opt_in_uid_parses_and_history_is_refused(self):
        with S.no_real_stores():
            def factory(**kwargs):
                return herdr_fs.HerdrStore(
                    binary="/fake/herdr",
                    runner=lambda argv: json.dumps(_snapshot()),
                    **kwargs
                )

            with mock.patch.object(registry.herdr_fs, "default_store", factory):
                self.assertIsNone(registry.parse_session_uid("herdr-pane:wA:pH"))
                self.assertEqual(
                    registry.parse_session_uid("herdr-pane:wA:pH", include_cli=True),
                    (SOURCE_HERDR_PANE, "wA:pH"),
                )
                found = registry.find_session("herdr-pane:wA:pH", include_cli=True)
                self.assertIsNotNone(found)
                result = registry.session_history("herdr-pane:wA:pH", include_cli=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["source"], SOURCE_HERDR_PANE)
            self.assertEqual(result["returned"], 0)

    def test_unavailable_sources_reports_herdr_only_under_opt_in(self):
        names = [s["source"] for s in registry.available_sources()]
        self.assertNotIn(SOURCE_HERDR_PANE, names)
        names = [s["source"] for s in registry.available_sources(include_cli=True)]
        self.assertIn(SOURCE_HERDR_PANE, names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
