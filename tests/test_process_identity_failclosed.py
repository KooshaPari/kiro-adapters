"""Fail-closed identity rules for :mod:`kiro_adapters.inventory.processes`.

An independent review found three ways the identity check could assert more
than it had observed:

* a recorded executable path was compared only when the current path could
  also be read, so an unreadable current path silently counted as a match;
* an unknown boot identity on either side could compare equal to another
  unknown, making every handle look valid;
* the Windows boot-id fallback used the creation time of the *current*
  process, which changes on every run and is therefore not a boot identity.

These tests pin the refusals. They are synthetic - the platform answer, the
process table, and the OS probes are stubbed - so no real Windows tooling
runs and no process is signalled. Standard library only, no network.
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import process_table  # noqa: E402
from kiro_adapters.inventory.model import ProcessInstance  # noqa: E402
from kiro_adapters.inventory.processes import same_process  # noqa: E402

PID = os.getpid()
START = "Mon Oct 19 08:38:05 2026"
BOOT = "boot-abc"
EXE = "/usr/local/bin/agent"


def recorded(exe=EXE, start=START, boot=BOOT):
    return ProcessInstance(
        pid=PID,
        start_identity=start,
        boot_id=boot,
        executable_path=exe,
    )


class IdentityProbe:
    """Stub the four probes :func:`same_process` consults."""

    def __init__(self, *, live=True, start=START, exe=EXE, boot=BOOT):
        self._patches = [
            mock.patch.object(process_table, "liveness",
                              return_value=process_table.ALIVE if live else process_table.GONE),
            mock.patch.object(process_table, "process_start", return_value=start),
            mock.patch.object(process_table, "executable_path", return_value=exe),
            mock.patch.object(process_table, "boot_id", return_value=boot),
        ]

    def __enter__(self):
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class ExecutableWitnessTests(unittest.TestCase):
    """An unreadable or absent executable path is not evidence of a match."""

    def test_matching_handle_is_accepted(self):
        with IdentityProbe():
            self.assertTrue(same_process(recorded(), PID))

    def test_unreadable_current_executable_fails_closed(self):
        """The expected path is recorded, but the current one cannot be read."""
        with IdentityProbe(exe=None):
            self.assertFalse(same_process(recorded(), PID))

    def test_absent_recorded_executable_fails_closed(self):
        """A record with no executable path is not a proven identity."""
        with IdentityProbe():
            self.assertFalse(same_process(recorded(exe=None), PID))

    def test_a_different_current_executable_is_rejected(self):
        with IdentityProbe(exe="/usr/bin/other"):
            self.assertFalse(same_process(recorded(), PID))


class UnknownIdentityTests(unittest.TestCase):
    """Unknown boot or start identities can never prove a match."""

    def test_unknown_recorded_boot_is_rejected(self):
        with IdentityProbe(boot=process_table.UNKNOWN_START):
            self.assertFalse(same_process(recorded(boot=process_table.UNKNOWN_START), PID))

    def test_unknown_current_boot_is_rejected(self):
        with IdentityProbe(boot=process_table.UNKNOWN_START):
            self.assertFalse(same_process(recorded(), PID))

    def test_two_unknown_boot_ids_do_not_match(self):
        """Two 'unknown' values must not be treated as the same boot."""
        with IdentityProbe(boot=process_table.UNKNOWN_START):
            self.assertFalse(same_process(recorded(boot=process_table.UNKNOWN_START), PID))

    def test_unknown_current_start_is_rejected(self):
        with IdentityProbe(start=process_table.UNKNOWN_START):
            self.assertFalse(same_process(recorded(), PID))

    def test_a_different_boot_is_rejected(self):
        with IdentityProbe(boot="boot-other"):
            self.assertFalse(same_process(recorded(), PID))

    def test_a_dead_pid_is_rejected(self):
        with IdentityProbe(live=False):
            self.assertFalse(same_process(recorded(), PID))


class WindowsBootIdentityTests(unittest.TestCase):
    """Windows must not invent a boot id from the current process."""

    def test_windows_boot_id_is_unknown(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "process_start", side_effect=AssertionError(
                    "a per-process start time must not stand in for a boot id"
                )
            ):
                self.assertEqual(
                    process_table.boot_id(), process_table.UNKNOWN_START
                )


if __name__ == "__main__":
    unittest.main()
