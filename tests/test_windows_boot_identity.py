"""Windows boot epoch and the composed Windows identity decision.

The boot epoch is read from ``Win32_OperatingSystem.LastBootUpTime`` through
the bounded capture path. These tests pin the honest answers: a genuine value
is accepted, every failed or malformed read is ``unknown``, and a Windows
process match requires the boot epoch, exact pid, CIM creation time, and
executable path together. All OS access is mocked; nothing runs PowerShell.
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import _windows_boot as wb  # noqa: E402
from kiro_adapters.inventory import process_table  # noqa: E402
from kiro_adapters.inventory._windows_table import CaptureResult  # noqa: E402
from kiro_adapters.inventory.model import ProcessInstance  # noqa: E402
from kiro_adapters.inventory.processes import same_process  # noqa: E402

PID = 4242
EPOCH = "2026-11-03T00:00:00.0000000Z"
CREATION = process_table.WINDOWS_START_PREFIX + "2026-11-03T01:02:03.0000000Z"
BOOT = "cim-boot:" + EPOCH
EXE = "C:\\Program Files\\Agent\\agent.exe"


def capture(rc=0, stdout="", stderr="", truncated=False, timed_out=False,
            reader_stalled=False):
    return CaptureResult(rc, stdout, stderr, truncated, timed_out, reader_stalled)


class BootEpochReadTests(unittest.TestCase):
    """One bounded read; every failure mode is unknown, never a value."""

    def setUp(self):
        wb.windows_boot_epoch.cache_clear()

    def tearDown(self):
        wb.windows_boot_epoch.cache_clear()

    def _read(self, result, shell="powershell.exe"):
        with mock.patch.object(wb, "_powershell_path", return_value=shell):
            with mock.patch.object(wb, "_run_capture", return_value=result):
                return wb.read_windows_boot_epoch()

    def test_genuine_value_is_accepted(self):
        self.assertEqual(self._read(capture(stdout=EPOCH + "\r\n")), EPOCH)

    def test_bom_is_stripped(self):
        self.assertEqual(self._read(capture(stdout="\ufeff" + EPOCH)), EPOCH)

    def test_missing_shell_is_unknown(self):
        self.assertIsNone(self._read(capture(stdout=EPOCH), shell=None))

    def test_nonzero_exit_is_unknown(self):
        self.assertIsNone(self._read(capture(rc=1, stdout=EPOCH)))

    def test_timeout_is_unknown(self):
        self.assertIsNone(self._read(capture(timed_out=True, stdout=EPOCH)))

    def test_truncation_is_unknown(self):
        self.assertIsNone(self._read(capture(truncated=True, stdout=EPOCH)))

    def test_stalled_reader_is_unknown(self):
        self.assertIsNone(self._read(capture(reader_stalled=True, stdout=EPOCH)))

    def test_stderr_is_unknown(self):
        self.assertIsNone(self._read(capture(stdout=EPOCH, stderr="boom")))

    def test_empty_or_multiline_is_unknown(self):
        self.assertIsNone(self._read(capture(stdout="  \n")))
        self.assertIsNone(self._read(capture(stdout=EPOCH + "\n" + EPOCH)))

    def test_malformed_output_is_unknown(self):
        for bad in ("not a time", "2026-11-03 00:00:00", "2026-11-03T00:00:00Z",
                    "2026-13-99T99:99:99.0000000Z"):
            self.assertIsNone(self._read(capture(stdout=bad)), bad)

    def test_command_uses_bounded_capture_and_read_only_cim(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return capture(stdout=EPOCH)

        with mock.patch.object(wb, "_powershell_path", return_value="C:\\ps.exe"):
            with mock.patch.object(wb, "_run_capture", side_effect=fake_run):
                wb.read_windows_boot_epoch()
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "C:\\ps.exe")
        self.assertIn("Get-CimInstance Win32_OperatingSystem", argv[-1])
        self.assertIn("LastBootUpTime", argv[-1])
        self.assertEqual(kwargs.get("timeout"), wb._TIMEOUT_SECONDS)

    def test_script_reaching_powershell_has_no_unrendered_braces(self):
        """The preamble is brace-escaped for ``str.format``; it must be rendered.

        ``_POWERSHELL_PREAMBLE`` is written as ``try {{ ... }} catch {{ }}`` so
        that callers can ``.format()`` it. A caller that concatenates it without
        rendering sends ``{{ }}`` to PowerShell, which does not error: it treats
        the doubled braces as a nested script block and emits that block's text
        to stdout. The output then has two lines, and this module's single-line
        guard rejects it, so the value silently reads as unknown.

        That is not hypothetical: it is exactly why ``boot_id()`` returned
        ``unknown`` on a real Windows host - exit 0, empty stderr - while every
        mocked test here passed. The mock returned a value and never executed the
        script, so the only thing a non-Windows host can actually check is the
        argv text. That is what this test checks.
        """
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return capture(stdout=EPOCH)

        with mock.patch.object(wb, "_powershell_path", return_value="ps.exe"):
            with mock.patch.object(wb, "_run_capture", side_effect=fake_run):
                wb.read_windows_boot_epoch()

        script = calls[0][-1]
        self.assertNotIn("{{", script, "double braces reached PowerShell")
        self.assertNotIn("}}", script, "double braces reached PowerShell")
        # And the rendering must not have eaten the deliberate block.
        self.assertIn("{ exit 1 }", script)

    def test_epoch_is_cached_per_process_run(self):
        with mock.patch.object(wb, "_powershell_path", return_value="ps.exe"):
            with mock.patch.object(
                wb, "_run_capture", return_value=capture(stdout=EPOCH)
            ) as run:
                self.assertEqual(wb.windows_boot_epoch(), EPOCH)
                self.assertEqual(wb.windows_boot_epoch(), EPOCH)
        run.assert_called_once()

    def test_failed_epoch_is_cached_too(self):
        with mock.patch.object(wb, "_powershell_path", return_value="ps.exe"):
            with mock.patch.object(
                wb, "_run_capture", return_value=capture(timed_out=True)
            ) as run:
                self.assertIsNone(wb.windows_boot_epoch())
                self.assertIsNone(wb.windows_boot_epoch())
        run.assert_called_once()


class WindowsBootIdTests(unittest.TestCase):
    """``boot_id`` places Windows on a real epoch or says unknown."""

    def test_epoch_becomes_a_prefixed_boot_id(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "windows_boot_epoch", return_value=EPOCH
            ):
                self.assertEqual(process_table.boot_id(), "cim-boot:" + EPOCH)
                self.assertNotEqual(
                    process_table.boot_id(), process_table.UNKNOWN_START
                )

    def test_unavailable_epoch_stays_unknown(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "windows_boot_epoch", return_value=None
            ):
                self.assertEqual(
                    process_table.boot_id(), process_table.UNKNOWN_START
                )


class WindowsSameProcessTests(unittest.TestCase):
    """The composed Windows identity never accepts a partial match."""

    def _decide(self, *, recorded, live=True, start=CREATION, exe=EXE,
                boot=BOOT):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "liveness",
                return_value=process_table.ALIVE if live else process_table.GONE,
            ), mock.patch.object(
                process_table, "process_start", return_value=start
            ), mock.patch.object(
                process_table, "executable_path", return_value=exe
            ), mock.patch.object(
                process_table, "boot_id", return_value=boot
            ):
                return same_process(recorded, PID)

    def _recorded(self, **kwargs):
        values = dict(pid=PID, start_identity=CREATION, boot_id=BOOT,
                      executable_path=EXE)
        values.update(kwargs)
        return ProcessInstance(**values)

    def test_every_component_present_is_accepted(self):
        self.assertTrue(self._decide(recorded=self._recorded()))

    def test_unknown_boot_is_refused(self):
        self.assertFalse(
            self._decide(
                recorded=self._recorded(),
                boot=process_table.UNKNOWN_START,
            )
        )

    def test_different_boot_is_refused(self):
        self.assertFalse(self._decide(recorded=self._recorded(), boot="cim-boot:other"))

    def test_creation_time_alone_is_not_enough(self):
        """A matching creation time with a different pid is a different process."""
        self.assertFalse(
            self._decide(recorded=self._recorded(pid=PID + 1))
        )

    def test_creation_time_mismatch_is_refused(self):
        self.assertFalse(
            self._decide(
                recorded=self._recorded(), start=CREATION + "-other"
            )
        )

    def test_coarse_identity_is_refused(self):
        self.assertFalse(
            self._decide(
                recorded=self._recorded(start_identity="lstart:Mon Oct 19 2026")
            )
        )

    def test_missing_recorded_executable_is_refused(self):
        self.assertFalse(
            self._decide(recorded=self._recorded(executable_path=None))
        )

    def test_unreadable_current_executable_is_refused(self):
        self.assertFalse(self._decide(recorded=self._recorded(), exe=None))

    def test_different_executable_is_refused(self):
        self.assertFalse(
            self._decide(recorded=self._recorded(), exe="C:\\other.exe")
        )

    def test_dead_pid_is_refused(self):
        self.assertFalse(self._decide(recorded=self._recorded(), live=False))

    def test_non_cim_start_shape_is_refused_on_windows(self):
        self.assertFalse(
            self._decide(
                recorded=self._recorded(start_identity="proc-stat:123"),
                start="proc-stat:123",
            )
        )


if __name__ == "__main__":
    unittest.main()
