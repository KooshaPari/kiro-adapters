"""Fail-closed identity rules for :mod:`kiro_adapters.inventory.processes`.

An independent review found three ways the identity check could assert more
than it had observed:

* a recorded executable path was compared only when the current path could
  also be read, so an unreadable current path silently counted as a match;
* an unknown boot identity on either side could compare equal to another
  unknown, making every handle look valid;
* the Windows boot-id fallback used the creation time of the *current*
  process, which changes on every run and is therefore not a boot identity.

The follow-up review added two more:

* the recorded pid was never compared with the pid being asked about;
* a one-second ``lstart`` cannot separate a pid recycled inside the same
  second, so only a subsecond platform marker may be compared.

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

# The identity shape the product ACCEPTS differs per platform, because the probe it
# trusts differs: POSIX admits `proc-stat:`/`proc-bsdinfo:` subsecond starts, while
# Windows admits only a CIM creation timestamp and refuses anything else regardless
# of how plausible it looks. Fixtures that inject the POSIX shape therefore make the
# suite fail on Windows for a reason that has nothing to do with the behaviour under
# test - which is exactly what happened: 3 tests failed on the real host.
#
# So build the fixtures from the same platform predicate the product uses. These
# tests then exercise the SAME logic on both platforms instead of asserting a POSIX
# assumption everywhere.
if process_table.is_windows():
    START = "cim-creation:2026-11-03T03:39:42.6681070Z"
    BOOT = "cim-boot:2026-11-03T03:39:42.6681070Z"
    EXE = r"C:\Windows\System32\agent.exe"
else:
    START = "proc-stat:123456789"
    BOOT = "boot-abc"
    EXE = "/usr/local/bin/agent"

COARSE = "lstart:Mon Oct 19 08:38:05 2026"


def recorded(exe=EXE, start=START, boot=BOOT, pid=PID):
    return ProcessInstance(
        pid=pid,
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


class PidMatchTests(unittest.TestCase):
    """A handle for one pid can never answer for another."""

    def test_a_handle_for_another_pid_is_rejected(self):
        with IdentityProbe():
            self.assertFalse(same_process(recorded(pid=PID + 1), PID))

    def test_a_handle_for_the_same_pid_is_accepted(self):
        with IdentityProbe():
            self.assertTrue(same_process(recorded(pid=PID), PID))

    def test_a_non_numeric_pid_is_rejected(self):
        with IdentityProbe():
            self.assertFalse(same_process(recorded(), "not-a-pid"))

    def test_a_missing_recorded_pid_is_rejected_without_raising(self):
        with IdentityProbe():
            self.assertFalse(same_process(recorded(pid=None), PID))

    def test_a_fractional_pid_is_not_truncated_into_a_match(self):
        # 4242.9 must not answer for pid 4242.
        with IdentityProbe():
            self.assertFalse(same_process(recorded(pid=float(PID) + 0.5), PID))

    def test_a_boolean_pid_is_rejected(self):
        # ``True`` subclasses ``int``; it must not answer for pid 1.
        with IdentityProbe():
            self.assertFalse(same_process(recorded(pid=True), 1))

    def test_a_digit_string_pid_is_accepted(self):
        with IdentityProbe():
            self.assertTrue(same_process(recorded(pid=str(PID)), PID))

    def test_a_unicode_digit_pid_is_refused_without_raising(self):
        # ``"\u00b2".isdigit()`` is True but ``int("\u00b2")`` raises
        # ValueError. A refusal must never escape as an exception.
        with IdentityProbe():
            self.assertFalse(same_process(recorded(pid="\u00b2"), PID))
            self.assertFalse(same_process(recorded(pid="\u0660\u0661"), PID))


class StartPrecisionTests(unittest.TestCase):
    """A coarse one-second start time is not proof of identity."""

    def test_a_coarse_recorded_start_is_rejected(self):
        with IdentityProbe(start=COARSE):
            self.assertFalse(same_process(recorded(start=COARSE), PID))

    def test_a_coarse_current_start_is_rejected(self):
        with IdentityProbe(start=COARSE):
            self.assertFalse(same_process(recorded(), PID))

    def test_a_legacy_bare_lstart_string_is_rejected(self):
        legacy = "Mon Oct 19 08:38:05 2026"
        with IdentityProbe(start=legacy):
            self.assertFalse(same_process(recorded(start=legacy), PID))

    def test_precision_is_decided_by_the_shared_predicate(self):
        self.assertTrue(process_table.is_precise_start("proc-stat:42"))
        self.assertTrue(process_table.is_precise_start("proc-bsdinfo:1.000001"))
        self.assertFalse(process_table.is_precise_start(COARSE))
        self.assertFalse(process_table.is_precise_start(process_table.UNKNOWN_START))
        self.assertFalse(process_table.is_precise_start(None))
        self.assertFalse(process_table.is_precise_start(""))


class LinuxStatParsingTests(unittest.TestCase):
    """The /proc stat parser finds field 22 even when comm contains parens."""

    @staticmethod
    def stat(comm: str, starttime: str = "987654") -> str:
        # Fields 1-2 (pid, comm), then state and the rest through field 22.
        middle = " ".join(["S"] + ["0"] * 18)  # fields 3..21
        return "4242 ({}) {} {} 0 0\n".format(comm, middle, starttime)

    def test_plain_command_name_parses(self):
        self.assertEqual(
            process_table._parse_proc_stat_start(self.stat("python3")),
            "proc-stat:987654",
        )

    def test_command_name_with_spaces_and_parens_parses(self):
        self.assertEqual(
            process_table._parse_proc_stat_start(self.stat("a) b (c)")),
            "proc-stat:987654",
        )

    def test_truncated_line_is_refused(self):
        self.assertIsNone(process_table._parse_proc_stat_start("4242 (x) S 0 0"))

    def test_missing_parenthesis_is_refused(self):
        self.assertIsNone(process_table._parse_proc_stat_start("garbage"))

    def test_non_numeric_starttime_is_refused(self):
        self.assertIsNone(
            process_table._parse_proc_stat_start(self.stat("x", starttime="NaN"))
        )


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

    def test_windows_boot_id_is_unknown_when_cim_is_unavailable(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "windows_boot_epoch", return_value=None
            ), mock.patch.object(
                process_table, "process_start", side_effect=AssertionError(
                    "a per-process start time must not stand in for a boot id"
                )
            ):
                self.assertEqual(
                    process_table.boot_id(), process_table.UNKNOWN_START
                )

    def test_same_process_refuses_every_windows_handle(self):
        """Even a matching creation time and path cannot prove identity."""
        identity = "2026-11-03T00:00:00.0000000Z"
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "liveness", return_value=process_table.ALIVE
            ):
                with mock.patch.object(
                    process_table, "process_start", return_value=identity
                ):
                    with mock.patch.object(
                        process_table, "executable_path", return_value="C:\\x.exe"
                    ):
                        handle = recorded(
                            exe="C:\\x.exe",
                            start=identity,
                            boot=process_table.UNKNOWN_START,
                        )
                        self.assertFalse(same_process(handle, PID))


if __name__ == "__main__":
    unittest.main()
