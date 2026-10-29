"""Regression tests for the jcode bridge: task store, task runner and MCP surface.

Every test in this file uses a fake POSIX executable and a throwaway home
directory. Nothing here touches a real ``jcode`` binary, a real user task
directory or a live configuration file.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)
from jcode_bridge import mcp_server, tasks  # noqa: E402
from jcode_bridge.backend import JcodeBackend  # noqa: E402

PACKAGE_ROOT = Path(tasks.__file__).resolve().parents[1]

_ARGV_ERRORS = ("unrecognized arguments", "the following arguments are required")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _wait_for_json(path: Path, timeout: float = 25.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            if isinstance(payload, dict):
                return payload
        time.sleep(0.05)
    raise AssertionError("timed out waiting for {}".format(path))


def _wait_for_file(path: Path, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_until_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


class BridgeTestCase(unittest.TestCase):
    """Shared plumbing: a scratch home, a scratch bin directory, no live config."""

    def setUp(self) -> None:
        ctx = S.temp_dir()
        self.tmp = Path(ctx.__enter__())
        self.addCleanup(ctx.__exit__, None, None, None)
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

    def backend(self, binary: Optional[str] = None) -> JcodeBackend:
        return JcodeBackend(binary=binary, home=str(self.home))

    def task_dir(self, backend: JcodeBackend, result: Any) -> Path:
        task_id = result.data["task_id"]
        return backend.tasks_dir / task_id


class TaskIdSafetyTests(BridgeTestCase):
    def test_traversal_task_ids_are_rejected(self):
        backend = self.backend()
        root = backend.tasks_dir.resolve()
        for bad in (".", "..", "a/b", "../escape", "a\\b", "x y", "", "a;b", "$HOME"):
            with self.subTest(task_id=bad):
                path, problem = tasks._task_dir(backend, bad)
                self.assertIsNotNone(problem, "expected a rejection for {!r}".format(bad))
                self.assertFalse(problem.ok)
                resolved = Path(path).resolve()
                self.assertTrue(
                    resolved == root or root in resolved.parents,
                    "task id {!r} escaped the tasks root: {}".format(bad, resolved),
                )

    def test_a_plain_task_id_resolves_to_a_child_of_the_tasks_root(self):
        backend = self.backend()
        path, problem = tasks._task_dir(backend, "t-123-abc")
        self.assertIsNone(problem)
        self.assertEqual(Path(path).parent, backend.tasks_dir)

    def test_task_lookups_refuse_traversal_ids(self):
        backend = self.backend()
        for action in (backend.status, backend.output, backend.cancel):
            with self.subTest(action=action.__name__):
                result = action("..")
                self.assertFalse(result.ok)


class FilePermissionTests(BridgeTestCase):
    def test_task_directory_and_files_are_owner_only(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        result = backend.start("hello")
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        self.assertEqual(_mode(task_dir), 0o700, "task directory mode")
        self.assertEqual(_mode(task_dir / "meta.json"), 0o600, "meta.json mode")
        _wait_for_json(task_dir / "result.json")
        for name in ("meta.json", "runner.json", "stdout.log", "stderr.log", "result.json"):
            path = task_dir / name
            if path.exists():
                self.assertEqual(_mode(path), 0o600, "{} mode".format(name))

    def test_the_tasks_root_is_not_group_or_world_accessible(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        result = backend.start("hello")
        S.assert_result_ok(result)
        root = backend.tasks_dir
        self.assertTrue(root.is_dir())
        self.assertEqual(
            _mode(root) & 0o077, 0, "tasks root leaks group or world bits: {:o}".format(_mode(root))
        )


class TimeoutValidationTests(BridgeTestCase):
    BAD_TIMEOUTS = (0, -1.0, float("nan"), float("inf"), 86401.0)

    def test_start_rejects_unusable_timeouts(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        for bad in self.BAD_TIMEOUTS:
            with self.subTest(timeout=bad):
                result = backend.start("hello", timeout=bad)
                self.assertFalse(result.ok, "timeout {!r} was accepted".format(bad))
        root = backend.tasks_dir
        if root.exists():
            self.assertEqual(list(root.iterdir()), [], "a rejected start left a task behind")

    def test_run_rejects_unusable_timeouts(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        for bad in self.BAD_TIMEOUTS:
            with self.subTest(timeout=bad):
                result = backend.run("hello", timeout=bad)
                self.assertFalse(result.ok, "timeout {!r} was accepted".format(bad))

    def test_the_upper_bound_timeout_is_accepted(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        result = backend.start("hello", timeout=86400.0)
        S.assert_result_ok(result)


class TimeoutIsolationTests(BridgeTestCase):
    def test_a_timeout_still_writes_a_result_and_kills_the_child(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "exec sleep 30")
        backend = self.backend(fake)
        result = backend.start("long", timeout=1.0)
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        payload = _wait_for_json(task_dir / "result.json", timeout=30.0)
        self.assertTrue(payload.get("timed_out"), "result.json did not record the timeout")
        runner = _wait_for_json(task_dir / "runner.json", timeout=15.0)
        self.assertTrue(
            _wait_until_gone(int(runner["child_pid"]), timeout=10.0),
            "the timed out child survived",
        )
        status = backend.status(result.data["task_id"])
        self.assertIn(status.data.get("state"), ("failed", "timed_out"))


class CancelSafetyTests(BridgeTestCase):
    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def _victim(self) -> subprocess.Popen:
        """Start a process that must survive a refused cancel."""
        proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        self.addCleanup(self._reap, proc)
        return proc
    def _write_fake_task(
        self,
        backend: JcodeBackend,
        task_id: str,
        meta_nonce: str,
        runner_nonce: str,
        wrapper_pid: int,
        wrapper_pgid: int,
        child_pid: int,
        child_pgid: int,
    ) -> Path:
        task_dir = backend.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "meta.json").write_text(
            json.dumps(
                {
                    "id": task_id,
                    "nonce": meta_nonce,
                    "pid": wrapper_pid,
                    "wrapper_pgid": wrapper_pgid,
                    "prompt": "sleep 30",
                    "argv": ["/bin/sleep", "30"],
                    "cwd": None,
                    "timeout_secs": 60,
                    "started_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        (task_dir / "runner.json").write_text(
            json.dumps(
                {
                    "nonce": runner_nonce,
                    "wrapper_pid": wrapper_pid,
                    "wrapper_pgid": wrapper_pgid,
                    "child_pid": child_pid,
                    "child_pgid": child_pgid,
                    "started_at": time.time(),
                    "argv": ["/bin/sleep", "30"],
                }
            ),
            encoding="utf-8",
        )
        return task_dir

    def test_cancel_refuses_when_the_nonce_does_not_match(self):
        backend = self.backend()
        victim = self._victim()
        victim_pgid = os.getpgid(victim.pid)
        self._write_fake_task(
            backend,
            "t-nonce",
            meta_nonce="nonce-a",
            runner_nonce="nonce-b",
            wrapper_pid=victim.pid,
            wrapper_pgid=victim_pgid,
            child_pid=victim.pid,
            child_pgid=victim_pgid,
        )
        result = backend.cancel("t-nonce")
        self.assertFalse(result.ok, "cancel accepted a runner with a foreign nonce")
        self.assertIsNone(victim.poll(), "cancel killed a process it must not trust")

    def test_cancel_refuses_when_the_wrapper_pid_does_not_match(self):
        backend = self.backend()
        victim = self._victim()
        decoy = self._victim()
        victim_pgid = os.getpgid(victim.pid)
        decoy_pgid = os.getpgid(decoy.pid)
        self._write_fake_task(
            backend,
            "t-pid",
            meta_nonce="nonce-a",
            runner_nonce="nonce-a",
            wrapper_pid=decoy.pid,
            wrapper_pgid=decoy_pgid,
            child_pid=victim.pid,
            child_pgid=victim_pgid,
        )
        result = backend.cancel("t-pid")
        self.assertFalse(result.ok, "cancel accepted a runner with a foreign wrapper pid")
        self.assertIsNone(victim.poll(), "cancel killed a process it must not trust")
        self.assertIsNone(decoy.poll(), "cancel killed a process it must not trust")

    def test_cancel_stops_a_task_it_owns(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "exec sleep 30")
        backend = self.backend(fake)
        result = backend.start("long", timeout=60.0)
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        runner = _wait_for_json(task_dir / "runner.json", timeout=20.0)
        cancelled = backend.cancel(result.data["task_id"], grace=5.0)
        S.assert_result_ok(cancelled)
        self.assertTrue(
            _wait_until_gone(int(runner["child_pid"]), timeout=10.0),
            "the cancelled child survived",
        )
        self.assertTrue((task_dir / "cancelled.json").exists())


class ArgumentPassthroughTests(BridgeTestCase):
    def test_the_prompt_reaches_the_child_verbatim(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '%s\\n' \"$@\"")
        backend = self.backend(fake)
        result = backend.start("alpha -- beta")
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        _wait_for_json(task_dir / "result.json")
        lines = (task_dir / "stdout.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["run", "--json", "alpha -- beta"])
        runner = _wait_for_json(task_dir / "runner.json")
        self.assertEqual(
            runner.get("argv"), [fake, "run", "--json", "alpha -- beta"],
            "the runner did not record the child argv verbatim",
        )

    def test_a_prompt_of_two_dashes_is_not_dropped(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '%s\\n' \"$@\"")
        backend = self.backend(fake)
        result = backend.start("--")
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        _wait_for_json(task_dir / "result.json")
        lines = (task_dir / "stdout.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["run", "--json", "--"])
        runner = _wait_for_json(task_dir / "runner.json")
        self.assertEqual(
            runner.get("argv"), [fake, "run", "--json", "--"],
            "a prompt of exactly two dashes was not preserved",
        )


class ChildEnvironmentTests(BridgeTestCase):
    def test_the_child_environment_drops_a_foreign_pythonpath(self):
        marker = self.tmp / "pythonpath.txt"
        body = "printf '%s\\n' \"$PYTHONPATH\" > '{}'".format(marker)
        fake = S.fake_bin(self.bin_dir, "jcode", body)
        backend = self.backend(fake)
        with S.env_vars(PYTHONPATH="/foreign/one:/foreign/two"):
            result = backend.start("hello")
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        _wait_for_json(task_dir / "result.json")
        recorded = marker.read_text(encoding="utf-8").strip()
        self.assertEqual(
            os.path.realpath(recorded),
            os.path.realpath(str(PACKAGE_ROOT)),
            "the child environment kept a foreign PYTHONPATH: {!r}".format(recorded),
        )

    def test_the_child_environment_drops_pythonhome(self):
        marker = self.tmp / "pythonhome.txt"
        body = "printf '%s\\n' \"$PYTHONHOME\" > '{}'".format(marker)
        fake = S.fake_bin(self.bin_dir, "jcode", body)
        backend = self.backend(fake)
        with S.env_vars(PYTHONHOME="/foreign/pythonhome"):
            result = backend.start("hello")
        S.assert_result_ok(result)
        self.assertTrue(
            _wait_for_file(marker, timeout=20.0),
            "the task never completed, PYTHONHOME probably leaked into the child",
        )
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "")


class TaskRunnerCliTests(BridgeTestCase):
    def _run_runner(self, task_dir: Path, command: List[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PACKAGE_ROOT)
        env.pop("PYTHONHOME", None)
        base = [
            sys.executable,
            "-m",
            "jcode_bridge.taskrunner",
            "--dir",
            str(task_dir),
            "--timeout",
            "0",
        ]
        last: Optional[subprocess.CompletedProcess] = None
        for extra in (["--nonce", "nonce-test"], []):
            argv = base + extra + ["--"] + command
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)
            last = proc
            if not any(marker in proc.stderr for marker in _ARGV_ERRORS):
                return proc
        return last  # type: ignore[return-value]

    def test_a_missing_task_directory_is_a_usage_error(self):
        missing = self.tmp / "no-such-dir"
        proc = self._run_runner(missing, ["/bin/echo", "hi"])
        self.assertNotEqual(proc.returncode, 0)
        for marker in _ARGV_ERRORS:
            self.assertNotIn(marker, proc.stderr, "the runner rejected the argv shape: {}".format(proc.stderr))

    def test_the_runner_reports_the_child_exit_code(self):
        task_dir = self.tmp / "runner-exit"
        task_dir.mkdir()
        proc = self._run_runner(task_dir, ["/bin/sh", "-c", "exit 7"])
        self.assertNotEqual(proc.returncode, 0)
        payload = _wait_for_json(task_dir / "result.json", timeout=20.0)
        self.assertEqual(payload.get("exit_code"), 7)


class RedactionTests(BridgeTestCase):
    def test_task_output_is_redacted(self):
        secret = "sk-ABCDEFGHIJKLMNOPQRST"
        body = "printf '{{\"text\": \"{}\"}}\\n'".format(secret)
        fake = S.fake_bin(self.bin_dir, "jcode", body)
        backend = self.backend(fake)
        result = backend.start("hello")
        S.assert_result_ok(result)
        task_dir = self.task_dir(backend, result)
        _wait_for_json(task_dir / "result.json")
        out = backend.output(result.data["task_id"])
        S.assert_result_ok(out)
        self.assertNotIn(secret, out.stdout or "")
        self.assertNotIn(secret, (out.data or {}).get("answer") or "")
        self.assertNotIn(secret, json.dumps(out.data or {}))

    def test_the_task_list_is_redacted(self):
        secret = "sk-ZYXWVUTSRQPONMLKJIHG"
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        result = backend.start(secret)
        S.assert_result_ok(result)
        listing = backend.list_tasks()
        S.assert_result_ok(listing)
        self.assertNotIn(secret, json.dumps(listing.data or {}))


class ErrorSurfaceTests(BridgeTestCase):
    def test_a_missing_working_directory_is_an_error_not_a_missing_binary(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        result = backend.run("hello", cwd="/definitely/not/a/real/directory")
        S.assert_result_failed(result, S.ERROR)

    def test_an_empty_prompt_is_rejected(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        backend = self.backend(fake)
        self.assertFalse(backend.run("   ").ok)
        self.assertFalse(backend.start("   ").ok)


class McpSurfaceTests(BridgeTestCase):
    def test_mcp_rejects_unusable_timeouts(self):
        fake = S.fake_bin(self.bin_dir, "jcode", "printf '{\"text\": \"ok\"}\\n'")
        with S.env_vars(JCODE_BRIDGE_BIN=fake, JCODE_BRIDGE_HOME=str(self.home)):
            mcp_server.reset_backend()
            try:
                cases = (
                    ("jcode_run", {"prompt": "x", "timeout_secs": -1}),
                    ("jcode_run", {"prompt": "x", "timeout_secs": 0}),
                    ("jcode_task_start", {"prompt": "x", "timeout_secs": -1}),
                    ("jcode_task_start", {"prompt": "x", "timeout_secs": 0}),
                )
                for tool, args in cases:
                    with self.subTest(tool=tool, args=args):
                        result = mcp_server.call_tool(tool, args)
                        self.assertFalse(result.ok, "{} accepted {}".format(tool, args))
            finally:
                mcp_server.reset_backend()
        root = Path(self.home) / "tasks"
        if root.exists():
            self.assertEqual(list(root.iterdir()), [], "a rejected start left a task behind")


if __name__ == "__main__":
    unittest.main()
