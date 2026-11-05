"""Bounded capture and fail-closed behavior for the Windows process table.

The module must never buffer an unbounded amount of child output, and every
consumer must treat a truncated, timed-out, or failed read as "no answer"
rather than as evidence. These tests exercise the reader against real child
processes (standard library only, no network) and the consumers against
synthetic capture results.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from kiro_adapters.inventory import _windows_table as wt  # noqa: E402


def capture(
    rc=0, stdout="", stderr="", truncated=False, timed_out=False, reader_stalled=False
):
    return wt.CaptureResult(
        rc, stdout, stderr, truncated, timed_out, reader_stalled
    )


def _python(code: str):
    return [sys.executable, "-c", code]


class BoundedCaptureTests(unittest.TestCase):
    """``_run_capture`` caps live streams and enforces the deadline."""

    def test_oversize_stdout_is_truncated_and_bounded(self):
        code = "import sys; sys.stdout.write('x' * 4_000_000); sys.stdout.flush()"
        out = wt._run_capture(_python(code), timeout=20, max_stdout_bytes=1000)
        self.assertTrue(out.truncated)
        self.assertTrue(out.truncated or out.timed_out)
        self.assertFalse(out.ok)
        self.assertLessEqual(len(out.stdout.encode("utf-8")), 1000)
        self.assertIsNotNone(out.returncode)

    def test_oversize_stderr_is_truncated_and_bounded(self):
        code = (
            "import sys; sys.stdout.write('ok'); sys.stdout.flush(); "
            "sys.stderr.write('e' * 4_000_000); sys.stderr.flush()"
        )
        out = wt._run_capture(_python(code), timeout=20, max_stderr_bytes=500)
        self.assertTrue(out.truncated)
        self.assertFalse(out.ok)
        self.assertLessEqual(len(out.stderr.encode("utf-8")), 500)

    def test_timeout_kills_the_child(self):
        started = time.monotonic()
        out = wt._run_capture(_python("import time; time.sleep(30)"), timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertTrue(out.timed_out)
        self.assertFalse(out.ok)
        self.assertLess(elapsed, 10.0)

    def test_normal_exit_returns_decoded_output(self):
        out = wt._run_capture(_python("print('hello')"), timeout=20)
        self.assertTrue(out.ok)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "hello")
        self.assertEqual(out.stderr, "")
        self.assertFalse(out.truncated)
        self.assertFalse(out.timed_out)
        self.assertFalse(out.reader_stalled)

    def test_nonzero_exit_is_not_ok(self):
        out = wt._run_capture(_python("import sys; sys.exit(7)"), timeout=20)
        self.assertEqual(out.returncode, 7)
        self.assertFalse(out.ok)

    def test_missing_executable_is_not_ok(self):
        out = wt._run_capture(["definitely-not-a-real-binary-xyz"], timeout=5)
        self.assertIsNone(out.returncode)
        self.assertFalse(out.ok)

    def test_large_chatter_on_one_stream_does_not_deadlock_the_other(self):
        code = (
            "import sys\n"
            "for _ in range(200):\n"
            "    sys.stderr.write('e' * 1000)\n"
            "    sys.stderr.flush()\n"
            "    sys.stdout.write('o' * 1000)\n"
            "    sys.stdout.flush()\n"
        )
        out = wt._run_capture(
            _python(code),
            timeout=20,
            max_stdout_bytes=1_000_000,
            max_stderr_bytes=1_000_000,
        )
        self.assertTrue(out.ok)
        self.assertEqual(len(out.stdout), 200_000)
        self.assertEqual(len(out.stderr), 200_000)

    def test_pipe_outliving_the_child_is_stalled_not_empty_success(self):
        """A grandchild holding the pipe must not look like empty success."""
        with S.temp_dir() as tmp:
            pidfile = tmp / "grandchild.pid"
            child = (
                "import subprocess, sys\n"
                "p = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(p.pid))\n"
            )
            started = time.monotonic()
            try:
                out = wt._run_capture(
                    [sys.executable, "-c", child, str(pidfile)], timeout=20
                )
            finally:
                pid = int(pidfile.read_text()) if pidfile.exists() else None
                if pid is not None:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
            elapsed = time.monotonic() - started
            self.assertEqual(out.returncode, 0)
            self.assertTrue(out.reader_stalled)
            self.assertFalse(out.ok)
            self.assertLess(elapsed, 10.0)

    def test_reader_thread_start_failure_kills_child_and_fails_closed(self):
        """Thread exhaustion must not leak the child or raise to the caller."""

        class BadThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        spawned = []
        real_popen = wt.subprocess.Popen

        def tracking_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        with mock.patch.object(wt.threading, "Thread", BadThread):
            with mock.patch.object(wt.subprocess, "Popen", tracking_popen):
                out = wt._run_capture(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=5,
                )
        self.assertIsNone(out.returncode)
        self.assertFalse(out.ok)
        self.assertFalse(out.reader_stalled)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(
            spawned[0].poll(), "child process leaked after thread failure"
        )

    def test_partial_thread_start_failure_does_not_raise_from_cleanup(self):
        """The second ``start()`` failing must not make cleanup raise.

        ``Thread.join`` raises RuntimeError on an *unstarted* thread. A stub
        whose ``join`` is a no-op hides exactly that, so this stub reproduces
        the real semantics: the first reader starts, the second does not, and
        joining the unstarted one raises. The handler must still fail closed
        instead of letting that RuntimeError escape.
        """

        class PartiallyStartingThread:
            started = False

            def __init__(self, index, registry, *args, **kwargs):
                self.index = index
                self.registry = registry

            def start(self):
                if self.index == 1:
                    raise RuntimeError("can't start new thread")
                self.started = True
                self.registry.append(self)

            def join(self, timeout=None):
                if not self.started:
                    raise RuntimeError("cannot join thread before it is started")

            def is_alive(self):
                return False

        registry = []
        counters = {"n": 0}
        real_popen = wt.subprocess.Popen
        spawned = []

        def make_thread(*args, **kwargs):
            index = counters["n"]
            counters["n"] += 1
            return PartiallyStartingThread(index, registry, *args, **kwargs)

        def tracking_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        with mock.patch.object(wt.threading, "Thread", make_thread):
            with mock.patch.object(wt.subprocess, "Popen", tracking_popen):
                out = wt._run_capture(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=5,
                )

        # The point of the test: no RuntimeError escaped.
        self.assertIsNone(out.returncode)
        self.assertFalse(out.ok)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(
            spawned[0].poll(), "child process leaked after partial thread failure"
        )


class TableReaderTests(unittest.TestCase):
    """``read_windows_table`` and ``windows_liveness`` fail closed."""

    def test_oversize_stdout_yields_no_table(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(truncated=True)):
            self.assertIsNone(wt.read_windows_table())

    def test_oversize_stderr_yields_no_table(self):
        with mock.patch.object(
            wt,
            "_run_capture",
            return_value=capture(stdout='"a.exe","7"\n', truncated=True),
        ):
            self.assertIsNone(wt.read_windows_table())

    def test_timeout_yields_no_table(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(timed_out=True)):
            self.assertIsNone(wt.read_windows_table())

    def test_nonzero_exit_yields_no_table(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(rc=1)):
            self.assertIsNone(wt.read_windows_table())

    def test_read_error_yields_no_table(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(rc=None)):
            self.assertIsNone(wt.read_windows_table())

    def test_valid_table_is_parsed(self):
        stdout = '"alpha.exe","10"\n"beta.exe","20"\n'
        with mock.patch.object(wt, "_run_capture", return_value=capture(stdout=stdout)):
            self.assertEqual(
                wt.read_windows_table(), {10: "alpha.exe", 20: "beta.exe"}
            )

    def test_liveness_is_unknown_on_truncation(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(truncated=True)):
            self.assertEqual(wt.windows_liveness(10), wt.UNKNOWN)

    def test_stalled_reader_yields_no_table(self):
        with mock.patch.object(
            wt, "_run_capture", return_value=capture(reader_stalled=True)
        ):
            self.assertIsNone(wt.read_windows_table())

    def test_stalled_reader_is_unknown_liveness(self):
        with mock.patch.object(
            wt, "_run_capture", return_value=capture(reader_stalled=True)
        ):
            self.assertEqual(wt.windows_liveness(10), wt.UNKNOWN)

    def test_liveness_is_unknown_on_timeout(self):
        with mock.patch.object(wt, "_run_capture", return_value=capture(timed_out=True)):
            self.assertEqual(wt.windows_liveness(10), wt.UNKNOWN)

    def test_liveness_is_unknown_on_stderr(self):
        with mock.patch.object(
            wt, "_run_capture", return_value=capture(stdout='"a.exe","10"\n', stderr="x")
        ):
            self.assertEqual(wt.windows_liveness(10), wt.UNKNOWN)

    def test_liveness_answers_alive_gone_and_unknown(self):
        with mock.patch.object(
            wt, "_run_capture", return_value=capture(stdout='"a.exe","10"\n')
        ):
            self.assertEqual(wt.windows_liveness(10), wt.ALIVE)
        with mock.patch.object(
            wt, "_run_capture", return_value=capture(stdout="INFO: No tasks\xa0are running which match the specified criteria.\n")
        ):
            self.assertEqual(wt.windows_liveness(10), wt.GONE)
        with mock.patch.object(wt, "_run_capture", return_value=capture(stdout="garbage\n")):
            self.assertEqual(wt.windows_liveness(10), wt.UNKNOWN)

    def test_liveness_requests_the_csv_shape_it_parses(self):
        """The argv must ask for the format the row parser actually reads.

        ``windows_liveness`` parses rows by splitting on '","' and reading field
        1 as the pid. That only works for ``tasklist /FO CSV``; without the flag
        tasklist prints space-aligned columns, every line yields one field, the
        parser skips them all and the function answers UNKNOWN for a live
        process.

        The test above cannot catch that, because it mocks ``_run_capture`` with
        CSV-shaped stdout no matter which flags were requested - the mock supplies
        the answer the parser wants rather than exercising the request. On a real
        Windows host the consequence was total: every liveness re-check answered
        UNKNOWN, so ``describe_many`` bound 0 of 2 pids. Asserting on argv is what
        a non-Windows host can actually verify.
        """
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return capture(stdout='"a.exe","10"\n')

        with mock.patch.object(wt, "_run_capture", side_effect=fake_run):
            self.assertEqual(wt.windows_liveness(10), wt.ALIVE)

        argv = seen[0]
        self.assertIn("/FO", argv, "tasklist format flag missing")
        self.assertEqual(argv[argv.index("/FO") + 1], "CSV")


class IdentityProbeTests(unittest.TestCase):
    """The CIM probe parses valid JSON and refuses every ambiguous read."""

    def _read(self, pids=(10,), result=None, shell="powershell.exe"):
        with mock.patch.object(wt, "_powershell_path", return_value=shell):
            with mock.patch.object(wt, "_run_capture", return_value=result):
                return wt._read_windows_process_identity_chunk(list(pids), set(pids))

    def test_valid_object_is_bound(self):
        payload = json.dumps(
            {
                "pid": 10,
                "creation": "2026-11-03T00:00:00.0000000Z",
                "executable": "C:\\x.exe",
            }
        )
        got = self._read(result=capture(stdout=payload))
        self.assertEqual(got, {10: ("2026-11-03T00:00:00.0000000Z", "C:\\x.exe")})

    def test_valid_list_and_bom_are_bound(self):
        payload = "\ufeff" + json.dumps(
            [
                {"pid": 10, "creation": "t1", "executable": "C:\\x.exe"},
                {"pid": 11, "creation": "t2", "executable": "C:\\y.exe"},
            ]
        )
        got = self._read(pids=(10, 11), result=capture(stdout=payload))
        self.assertEqual(
            got, {10: ("t1", "C:\\x.exe"), 11: ("t2", "C:\\y.exe")}
        )

    def test_unwanted_pid_is_dropped(self):
        payload = json.dumps({"pid": 99, "creation": "t", "executable": "C:\\x.exe"})
        self.assertEqual(self._read(result=capture(stdout=payload)), {})

    def test_empty_output_is_empty_binding(self):
        self.assertEqual(self._read(result=capture(stdout="  \n")), {})

    def test_truncated_output_fails_closed(self):
        payload = json.dumps({"pid": 10, "creation": "t", "executable": "C:\\x.exe"})
        self.assertIsNone(self._read(result=capture(stdout=payload, truncated=True)))

    def test_oversize_stderr_fails_closed(self):
        payload = json.dumps({"pid": 10, "creation": "t", "executable": "C:\\x.exe"})
        self.assertIsNone(
            self._read(result=capture(stdout=payload, stderr="boom", truncated=True))
        )

    def test_timeout_fails_closed(self):
        self.assertIsNone(self._read(result=capture(timed_out=True)))

    def test_stalled_reader_fails_closed(self):
        self.assertIsNone(self._read(result=capture(reader_stalled=True)))

    def test_nonzero_exit_fails_closed(self):
        payload = json.dumps({"pid": 10, "creation": "t", "executable": "C:\\x.exe"})
        self.assertIsNone(self._read(result=capture(rc=1, stdout=payload)))

    def test_malformed_json_fails_closed(self):
        self.assertIsNone(self._read(result=capture(stdout="{not json")))

    def test_missing_shell_fails_closed(self):
        self.assertIsNone(self._read(result=capture(stdout="{}"), shell=None))

    def test_powershell_command_requests_utf8_and_uses_resolved_shell(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return capture(stdout="")

        with mock.patch.object(wt, "_powershell_path", return_value="C:\\pwsh.exe"):
            with mock.patch.object(wt, "_run_capture", side_effect=fake_run):
                wt._read_windows_process_identity_chunk([10], {10})
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "C:\\pwsh.exe")
        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        self.assertIn("[Console]::OutputEncoding", argv[-1])
        self.assertIn("UTF8Encoding", argv[-1])
        self.assertIn("ProcessId=10", argv[-1])
        self.assertEqual(kwargs.get("timeout"), 15)

    def test_identity_read_fails_closed_when_one_chunk_fails(self):
        with mock.patch.object(wt, "_powershell_path", return_value="powershell.exe"):
            with mock.patch.object(
                wt, "_read_windows_process_identity_chunk", return_value=None
            ):
                self.assertIsNone(wt.read_windows_process_identities([10]))

    def test_no_valid_pids_makes_no_call(self):
        with mock.patch.object(wt, "_run_capture") as run:
            self.assertEqual(wt.read_windows_process_identities([0, -1, "x"]), {})
        run.assert_not_called()


class PowerShellResolutionTests(unittest.TestCase):
    """``powershell.exe`` is preferred, ``pwsh`` is the fallback, absent is None."""

    def setUp(self):
        wt._powershell_path.cache_clear()

    def tearDown(self):
        wt._powershell_path.cache_clear()

    def test_prefers_windows_powershell(self):
        def which(name):
            return {"powershell.exe": "C:\\ps.exe", "pwsh": "C:\\pwsh.exe"}.get(name)

        with mock.patch.object(wt.shutil, "which", side_effect=which):
            self.assertEqual(wt._powershell_path(), "C:\\ps.exe")

    def test_falls_back_to_pwsh(self):
        def which(name):
            return "C:\\pwsh.exe" if name == "pwsh" else None

        with mock.patch.object(wt.shutil, "which", side_effect=which):
            self.assertEqual(wt._powershell_path(), "C:\\pwsh.exe")

    def test_none_when_absent(self):
        with mock.patch.object(wt.shutil, "which", return_value=None):
            self.assertIsNone(wt._powershell_path())


class TerminateTests(unittest.TestCase):
    """A group kill is used only when the child still leads its own group."""

    class FakeProc:
        pid = 4242

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX-only")
    def test_killpg_used_when_child_leads_its_group(self):
        proc = self.FakeProc()
        with mock.patch.object(wt.os, "getpgid", return_value=proc.pid):
            with mock.patch.object(wt.os, "killpg") as killpg:
                with mock.patch.object(proc, "kill") as kill:
                    wt._terminate(proc)
        killpg.assert_called_once_with(proc.pid, 9)
        kill.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX-only")
    def test_pgid_mismatch_degrades_to_killing_only_the_child(self):
        proc = self.FakeProc()
        with mock.patch.object(wt.os, "getpgid", return_value=1):
            with mock.patch.object(wt.os, "killpg") as killpg:
                wt._terminate(proc)
        killpg.assert_not_called()
        self.assertTrue(proc.killed)


if __name__ == "__main__":
    unittest.main()
