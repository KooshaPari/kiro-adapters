"""Detached task wrapper for asynchronous jcode runs.

Started as::

    python -m jcode_bridge.taskrunner --dir DIR [--timeout SECS] [--nonce NONCE] -- jcode run ...

It owns four things the caller cannot own for itself:

1. It is the parent of the jcode process, so it can record the real exit code
   even after the MCP server that started it has exited.
2. It writes stdout/stderr to files, so output is readable after the fact
   without a pipe that dies with the launcher.
3. It runs the child in its own session, so a timeout can kill the child's
   process group without killing the wrapper that still has to write the
   result.
4. It writes runner.json, which carries the nonce the task store handed it
   together with the pids of the wrapper and of the child. The task store only
   trusts that record when the nonce matches, so a stale or forged file cannot
   make the bridge signal an unrelated process group.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .payload import write_json_private

FILE_MODE = 0o600


def _open_log(path: str) -> int:
    """Open a log file for appending, readable by the owner alone."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.fchmod(handle, FILE_MODE)
    except OSError:
        pass
    return handle


def _child_argv(remainder: Sequence[str]) -> List[str]:
    """Return the argv to run, dropping only the leading separator.

    The separator is the one token that tells argparse where the options end.
    Every other ``--`` belongs to the child, so a prompt of exactly ``--`` or
    an argument such as ``run -- --flag`` has to survive untouched.
    """
    argv = list(remainder)
    if argv and argv[0] == "--":
        argv = argv[1:]
    return argv


def _signal_group(pgid: int, sig: int) -> bool:
    """Send ``sig`` to a process group, refusing to signal our own group."""
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _terminate_child(proc: "subprocess.Popen[bytes]", pgid: int, grace: float) -> None:
    """Stop the child politely and then forcefully.

    The child was started in its own session, so ``pgid`` is its own group and
    killing that group cannot reach the wrapper that has to write the result.
    """
    if not _signal_group(pgid, signal.SIGTERM):
        return
    try:
        proc.wait(timeout=max(0.0, grace))
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _install_forwarding(child_pgid: int) -> None:
    """Forward SIGTERM and SIGINT to the child's process group."""

    def _forward(signum: int, _frame: Any) -> None:
        _signal_group(child_pgid, signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _forward)
        except (OSError, ValueError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="jcode_bridge.taskrunner")
    parser.add_argument("--dir", required=True, help="task directory to write into")
    parser.add_argument("--timeout", type=float, default=0.0, help="seconds; 0 = no limit")
    parser.add_argument(
        "--nonce",
        default=None,
        help="task nonce, echoed into runner.json so the task store can trust it",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the argv to run")
    args = parser.parse_args(list(argv) if argv is not None else None)

    command = _child_argv(args.command)
    if not command:
        sys.stderr.write("taskrunner: no command after --\n")
        return 2

    task_dir = Path(os.path.abspath(args.dir))
    if not task_dir.is_dir():
        sys.stderr.write(
            "taskrunner: task directory does not exist: {}\n".format(task_dir)
        )
        return 2

    nonce = args.nonce or uuid.uuid4().hex
    stdout_path = task_dir / "stdout.log"
    stderr_path = task_dir / "stderr.log"
    result_path = task_dir / "result.json"
    runner_path = task_dir / "runner.json"

    started = time.time()
    code: Optional[int] = None
    timed_out = False
    error: Optional[str] = None

    out_fd = _open_log(str(stdout_path))
    err_fd = _open_log(str(stderr_path))
    try:
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out_fd,
                stderr=err_fd,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
        else:
            # The child leads its own session, so its process group id is its
            # pid. The group id is recorded once here and never looked up
            # again later, because a pid may be recycled by then.
            child_pgid = proc.pid
            _install_forwarding(child_pgid)
            write_json_private(
                runner_path,
                {
                    "nonce": nonce,
                    "wrapper_pid": os.getpid(),
                    "wrapper_pgid": os.getpgrp(),
                    "child_pid": proc.pid,
                    "child_pgid": child_pgid,
                    "started_at": started,
                    "argv": command,
                },
            )
            limit = args.timeout if args.timeout and args.timeout > 0 else None
            try:
                code = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_child(proc, child_pgid, grace=2.0)
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = proc.returncode
    finally:
        for handle in (out_fd, err_fd):
            try:
                os.close(handle)
            except OSError:
                pass

    write_json_private(
        result_path,
        {
            "exit_code": code,
            "timed_out": timed_out,
            "error": error,
            "duration_ms": int((time.time() - started) * 1000),
            "finished_at": time.time(),
            "nonce": nonce,
            "argv": command,
        },
    )
    return 0 if code == 0 and not timed_out and error is None else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
