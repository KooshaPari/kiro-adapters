"""Pid-reuse safety for the process liveness helpers.

``kill -0`` only answers "does this pid exist right now". A pid that was
recycled onto an unrelated process still answers yes, so the helpers here bind
a pid to the OS's own record of when the process started. These tests pin the
two refusals that matter: a recycled pid and a handle from before a reboot
must both fail to match.

Standard library only. No network, no access to any real harness store.
"""

from __future__ import annotations

import os
import platform
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import process_table  # noqa: E402
from kiro_adapters.inventory.model import ProcessInstance  # noqa: E402
from kiro_adapters.inventory.processes import (  # noqa: E402
    boot_id,
    describe,
    describe_many,
    is_alive,
    same_process,
    scan,
)

POSIX = platform.system() != "Windows"

# A pid above the usual pid ceiling on this host. It can never be alive, so a
# lookup for it is a clean negative control.
DEAD_PID = 999999


class LivenessTests(unittest.TestCase):
    """The weak check: does this pid exist right now."""

    def test_the_current_process_is_alive(self):
        self.assertTrue(is_alive(os.getpid()))

    def test_an_unused_pid_is_not_alive(self):
        self.assertFalse(is_alive(DEAD_PID))

    def test_pid_zero_is_not_alive(self):
        self.assertFalse(is_alive(0))

    def test_a_negative_pid_is_not_alive(self):
        self.assertFalse(is_alive(-1))


class BootIdTests(unittest.TestCase):
    """The boot id separates handles taken across a reboot."""

    def test_boot_id_is_not_empty(self):
        self.assertTrue(boot_id())

    def test_boot_id_is_stable_within_one_boot(self):
        self.assertEqual(boot_id(), boot_id())

    def test_a_fake_boot_id_differs_from_the_real_one(self):
        self.assertNotEqual(boot_id(), "not-a-real-boot-id")


class DescribeTests(unittest.TestCase):
    """A live pid becomes a process instance bound tightly enough to trust."""

    def test_describe_returns_the_current_process(self):
        instance = describe(os.getpid())
        self.assertIsNotNone(instance)
        self.assertEqual(instance.pid, os.getpid())

    @unittest.skipUnless(POSIX, "a POSIX start identity is required")
    def test_describe_records_a_real_start_identity(self):
        instance = describe(os.getpid())
        self.assertNotEqual(instance.start_identity, "unknown")
        self.assertTrue(instance.start_identity)

    def test_describe_returns_none_for_a_dead_pid(self):
        self.assertIsNone(describe(DEAD_PID))

    def test_describe_returns_none_for_pid_zero(self):
        self.assertIsNone(describe(0))


class SameProcessTests(unittest.TestCase):
    """A match must be proven, never assumed from the pid alone."""

    def test_a_freshly_taken_handle_matches_the_live_process(self):
        instance = describe(os.getpid())
        self.assertTrue(same_process(instance, os.getpid()))

    def test_a_missing_handle_matches_nothing(self):
        self.assertFalse(same_process(None, os.getpid()))

    def test_a_handle_for_a_dead_pid_matches_nothing(self):
        self.assertFalse(same_process(describe(DEAD_PID), DEAD_PID))

    def test_a_recycled_pid_is_rejected(self):
        """A live pid whose start time differs is a different process."""
        impostor = ProcessInstance(
            pid=os.getpid(),
            start_identity="Tue Oct 27 23:39:05 2026",
            boot_id=boot_id(),
        )
        self.assertFalse(same_process(impostor, os.getpid()))

    def test_a_handle_from_before_a_reboot_is_rejected(self):
        """A handle recorded before a reboot can never match after it."""
        instance = describe(os.getpid())
        self.assertIsNotNone(instance)
        from_before_the_reboot = ProcessInstance(
            pid=instance.pid,
            start_identity=instance.start_identity,
            boot_id="a-different-boot",
        )
        self.assertFalse(same_process(from_before_the_reboot, os.getpid()))

    def test_a_handle_with_an_unknown_start_identity_matches_nothing(self):
        unknown = ProcessInstance(
            pid=os.getpid(),
            start_identity="unknown",
            boot_id=boot_id(),
        )
        self.assertFalse(same_process(unknown, os.getpid()))


class DescribeManyTests(unittest.TestCase):
    """The batched read must answer every pid it was asked about."""

    def test_an_empty_request_describes_nothing(self):
        self.assertEqual(describe_many([]), {})

    def test_the_current_process_is_described(self):
        found = describe_many([os.getpid()])
        self.assertIn(os.getpid(), found)
        self.assertEqual(found[os.getpid()].pid, os.getpid())

    def test_the_batched_start_identity_matches_the_single_read(self):
        single = describe(os.getpid())
        batched = describe_many([os.getpid()])
        self.assertEqual(single.start_identity, batched[os.getpid()].start_identity)

    def test_a_dead_pid_is_simply_absent(self):
        found = describe_many([DEAD_PID])
        self.assertNotIn(DEAD_PID, found)

    def test_a_live_pid_is_still_found_next_to_a_dead_one(self):
        """A dead pid must not poison the whole batch."""
        found = describe_many([os.getpid(), DEAD_PID])
        self.assertIn(os.getpid(), found)
        self.assertNotIn(DEAD_PID, found)


class ScanTests(unittest.TestCase):
    """The discovery snapshot must actually see this user's processes."""

    def test_scan_includes_the_current_process(self):
        found = scan()
        self.assertIn(os.getpid(), found)

    def test_scan_reports_a_start_identity_for_the_current_process(self):
        found = scan()
        self.assertNotEqual(found[os.getpid()].start_identity, "unknown")


class TempDirUsageTests(unittest.TestCase):
    """The helpers never touch the real home directory."""

    def test_a_temporary_home_does_not_change_the_boot_id(self):
        with S.temp_dir() as tmp:
            with S.env_vars(HOME=str(tmp)):
                self.assertEqual(boot_id(), boot_id())


class WindowsProbeTests(unittest.TestCase):
    """Windows liveness must not be a signal, and unknown is not gone.

    Every case here is synthetic: the platform answer and the OS call are
    stubbed, so no real tasklist runs and no real process is signalled.
    """

    def test_a_windows_probe_never_calls_os_kill(self):
        """``os.kill(pid, 0)`` terminates on Windows; it must not be reached."""

        def explode(*_args, **_kwargs):
            raise AssertionError("os.kill must never be used on Windows")

        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table, "windows_liveness", return_value=process_table.ALIVE
            ):
                with mock.patch.object(process_table.os, "kill", side_effect=explode):
                    verdict = process_table.liveness(os.getpid())
        self.assertEqual(verdict, process_table.ALIVE)

    def test_a_failed_windows_probe_is_unknown_not_gone(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(
                process_table.subprocess, "run", side_effect=OSError("no tasklist")
            ):
                verdict = process_table.liveness(os.getpid())
        self.assertEqual(verdict, process_table.UNKNOWN)
        self.assertNotEqual(verdict, process_table.GONE)

    def test_an_unreadable_windows_table_leaves_pids_unknown(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(process_table, "windows_table", return_value=None):
                verdicts = process_table.liveness_many([os.getpid()])
        self.assertEqual(verdicts, {os.getpid(): process_table.UNKNOWN})

    def test_a_gone_windows_pid_is_still_reported_gone(self):
        """The positive control: absence from a read table is evidence."""
        with mock.patch.object(process_table, "is_windows", return_value=True):
            with mock.patch.object(process_table, "windows_table", return_value={}):
                verdicts = process_table.liveness_many([DEAD_PID])
        self.assertEqual(verdicts, {DEAD_PID: process_table.GONE})


class UnverifiableIdentityTests(unittest.TestCase):
    """A pid with no start identity is not a process identity."""

    def test_describe_omits_a_process_with_no_start_identity(self):
        with mock.patch.object(
            process_table,
            "process_start",
            return_value=process_table.UNKNOWN_START,
        ):
            self.assertIsNone(describe(os.getpid()))

    def test_describe_many_omits_a_process_with_no_start_identity(self):
        unbound = ProcessInstance(
            pid=os.getpid(),
            start_identity=process_table.UNKNOWN_START,
            boot_id=boot_id(),
        )
        with mock.patch.object(
            process_table, "cached_scan", return_value={os.getpid(): unbound}
        ):
            self.assertEqual(describe_many([os.getpid()]), {})

    def test_describe_many_drops_a_table_entry_that_has_since_exited(self):
        stale = ProcessInstance(
            pid=DEAD_PID,
            start_identity="Mon Oct 19 08:38:05 2026",
            boot_id=boot_id(),
        )
        with mock.patch.object(
            process_table, "cached_scan", return_value={DEAD_PID: stale}
        ):
            self.assertEqual(describe_many([DEAD_PID]), {})

    def test_describe_many_binds_nothing_on_windows(self):
        with mock.patch.object(process_table, "is_windows", return_value=True):
            self.assertEqual(describe_many([os.getpid()]), {})


if __name__ == "__main__":
    unittest.main()
