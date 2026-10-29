"""Projection redaction tests: no raw user content escapes the envelope.

A title, a working directory and a native id are user-derived. The envelope is
consumed by other applications, so these tests pin that:

1. credentials inside a title or a directory are redacted,
2. control characters (newlines, NUL, ESC) never reach the projection, so
   source data cannot forge a log line or a failure detail,
3. a native id that is nothing but control characters is not an identity.

Standard library only, no network, no access to any real harness store.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import jcode_fs, kirocrew_fs  # noqa: E402
from kiro_adapters.inventory.model import (  # noqa: E402
    MAX_TITLE_CHARS,
    SOURCE_JCODE_FS,
    ProcessInstance,
    SessionEnvelope,
    make_session_uid,
    sanitize_native_id,
    sanitize_user_text,
)

JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)


def make_envelope(**overrides) -> SessionEnvelope:
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


class TitleRedactionTests(unittest.TestCase):
    def test_credential_in_a_title_is_redacted(self):
        env = make_envelope(title="debug {}".format(JWT))
        rendered = json.dumps(env.to_dict())
        self.assertNotIn(JWT, rendered)
        self.assertIn("<jwt:redacted>", env.to_dict()["title"])

    def test_title_control_characters_are_removed(self):
        env = make_envelope(title="first line\nsecond\tline\x00")
        self.assertNotIn("\n", env.title)
        self.assertNotIn("\t", env.title)
        self.assertNotIn("\x00", env.title)
        self.assertEqual(env.title, "first linesecondline")

    def test_title_is_bounded(self):
        env = make_envelope(title="x" * (MAX_TITLE_CHARS * 2))
        self.assertLessEqual(len(env.title), MAX_TITLE_CHARS)
        self.assertTrue(env.title.endswith("\u2026"))


class WorkspaceRedactionTests(unittest.TestCase):
    def test_credential_in_a_workspace_is_redacted(self):
        env = make_envelope(workspace="/Users/x/proj?token=abcdefgh123456")
        self.assertNotIn("abcdefgh123456", env.workspace)
        self.assertIn("<redacted>", env.workspace)

    def test_workspace_control_characters_are_removed(self):
        env = make_envelope(workspace="/Users/x/proj\n\x1b[31m")
        self.assertNotIn("\n", env.workspace)
        self.assertNotIn("\x1b", env.workspace)


class NativeIdTests(unittest.TestCase):
    def test_control_characters_never_reach_the_projection(self):
        env = make_envelope(native_session_id="abc\n\x00evil\r")
        self.assertEqual(env.native_session_id, "abcevil")
        # The uid is rebuilt from the sanitised id, so it cannot carry them.
        self.assertEqual(env.session_uid, make_session_uid(SOURCE_JCODE_FS, "abcevil"))
        rendered = json.dumps(env.to_dict())
        for char in ("\n", "\x00", "\r"):
            self.assertNotIn(char, rendered)

    def test_an_id_of_only_control_characters_is_not_an_identity(self):
        with self.assertRaises(ValueError):
            make_envelope(native_session_id="\x00\n\t")

    def test_evidence_ref_is_sanitised(self):
        env = make_envelope(evidence_ref="jcode-fs:alpha\n\x00")
        self.assertNotIn("\n", env.evidence_ref)
        self.assertEqual(env.evidence_ref, "jcode-fs:alpha")


class EnvironmentFieldRedactionTests(unittest.TestCase):
    """Host, namespace, harness version and executable path are text too.

    They are environment-derived rather than authored by us, so they get the
    same redaction and control-character stripping as a title or a workspace.
    """

    def test_host_id_control_characters_are_removed(self):
        env = make_envelope(host_id="host\n\x1b[31m\x00name")
        rendered = json.dumps(env.to_dict())
        for char in ("\n", "\x1b", "\x00"):
            self.assertNotIn(char, rendered)
        self.assertEqual(env.host_id, "host[31mname")

    def test_namespace_id_credential_is_redacted(self):
        env = make_envelope(namespace_id="ns?token=abcdefgh123456")
        self.assertNotIn("abcdefgh123456", env.namespace_id)
        self.assertIn("<redacted>", env.namespace_id)

    def test_harness_version_is_control_character_free(self):
        env = make_envelope(harness_version="0.99.1\r\n0.1\x00")
        self.assertEqual(env.harness_version, "0.99.10.1")
        self.assertNotIn("\r", json.dumps(env.to_dict()))

    def test_harness_version_stays_optional(self):
        self.assertIsNone(make_envelope().harness_version)

    def test_executable_path_is_redacted_and_stripped(self):
        env = make_envelope(
            process_instance=ProcessInstance(
                pid=7,
                start_identity="Mon Oct 20 00:00:00 2026",
                boot_id="boot-1",
                executable_path="/opt/app?token=abcdefgh123456\n\x00",
            )
        )
        path = env.process_instance.executable_path
        self.assertNotIn("abcdefgh123456", path)
        self.assertIn("<redacted>", path)
        self.assertNotIn("\n", path)
        self.assertNotIn("\x00", path)

    def test_executable_path_stays_optional(self):
        env = make_envelope(
            process_instance=ProcessInstance(
                pid=7, start_identity="sig", boot_id="boot-1"
            )
        )
        self.assertIsNone(env.process_instance.executable_path)


class MachineStateSanitisationTests(unittest.TestCase):
    """Machine-state fields keep their meaning: no redaction, no ellipsis."""

    def test_state_control_characters_are_removed(self):
        env = make_envelope(state="ru\nnning\x00")
        self.assertEqual(env.state, "running")

    def test_launch_mode_control_characters_are_removed(self):
        env = make_envelope(launch_mode="inter\tactive")
        self.assertEqual(env.launch_mode, "interactive")

    def test_source_updated_at_control_characters_are_removed(self):
        env = make_envelope(source_updated_at="2026-10-20T00:05:00Z\n")
        self.assertEqual(env.source_updated_at, "2026-10-20T00:05:00Z")

    def test_machine_values_are_not_redacted(self):
        # A token-shaped state must survive verbatim: consumers compare these
        # by equality and redaction would silently change a machine value.
        env = make_envelope(state="token=abcdefgh123456")
        self.assertEqual(env.state, "token=abcdefgh123456")
        self.assertNotIn("<redacted>", env.state)

    def test_ordinary_values_are_unchanged(self):
        env = make_envelope(
            state="running", launch_mode="interactive",
            source_updated_at="2026-10-20T00:05:00.000000Z",
        )
        self.assertEqual(env.state, "running")
        self.assertEqual(env.launch_mode, "interactive")
        self.assertEqual(env.source_updated_at, "2026-10-20T00:05:00.000000Z")


class SanitizerTests(unittest.TestCase):
    def test_sanitize_native_id_handles_empty_input(self):
        self.assertEqual(sanitize_native_id(None), "")
        self.assertEqual(sanitize_native_id(""), "")

    def test_sanitize_user_text_preserves_none(self):
        self.assertIsNone(sanitize_user_text(None, 10))


class AdapterFailureDetailTests(unittest.TestCase):
    """Failure details are built from the sanitised id, not the raw one."""

    def test_jcode_history_sanitises_its_id(self):
        source = inspect.getsource(jcode_fs.JcodeStore.history)
        self.assertIn("sanitize_native_id(native_id)", source)

    def test_kirocrew_history_sanitises_its_id(self):
        source = inspect.getsource(kirocrew_fs.KiroCrewStore.history)
        self.assertIn("sanitize_native_id(session_id)", source)


if __name__ == "__main__":
    unittest.main()
