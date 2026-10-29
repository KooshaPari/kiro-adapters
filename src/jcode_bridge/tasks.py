"""Task store for detached jcode runs.

A "task" is a directory::

    ~/.jcode-bridge/tasks/<task id>/
        meta.json       id, nonce, pid, prompt, argv, cwd, timeout, started_at
        stdout.log      captured stdout of `jcode run`
        stderr.log      captured stderr
        runner.json     written by the task runner: nonce, wrapper and child pids
        result.json     written by jcode_bridge.taskrunner when jcode exits
        cancelled.json  written here when a caller cancels

The task id is a caller supplied string, so it is validated before it is ever
joined onto the tasks root: an id such as ".." must not be able to walk out of
the directory that holds the tasks.

The same reasoning drives the rest of the module. A caller may only cancel a
task whose runner record proves it belongs to the process this bridge started,
so a stale or forged file cannot make the bridge signal an unrelated process
group, and the kill target is always read from the runner record rather than
looked up from a pid that may since have been recycled.
"""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kiro_adapters import result as R
from kiro_adapters.redact import redact, redact_obj

from .payload import (
    coerce_int,
    descends_from,
    ensure_private_dir,
    extract_answer,
    is_inside,
    option_problem,
    pid_alive,
    prompt_problem,
    read_json,
    tail,
    write_json_private,
)

HARNESS = "jcode"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RESERVED_TASK_IDS = frozenset({".", ".."})
MAX_TIMEOUT = 86400.0

# The detached wrapper outlives the call that started it. Python warns when a
# Popen object is collected while its process is still running, so finished
# wrappers are polled and dropped on the next start.
_PENDING: List[subprocess.Popen] = []


def _track(proc: "subprocess.Popen") -> None:
    """Keep a reference to a detached wrapper until it has finished."""
    _PENDING[:] = [item for item in _PENDING if item.poll() is None]
    _PENDING.append(proc)


def _task_dir(backend: Any, task_id: str) -> Tuple[Path, Optional[R.AdapterResult]]:
    """Return ``(task_dir, problem)`` for a caller supplied task id.

    The id is rejected unless it is a short name from a safe charset, and the
    literal names "." and ".." are rejected as well: both are legal paths and
    both point somewhere other than a task.
    """
    root = backend.tasks_dir
    if (
        not isinstance(task_id, str)
        or task_id in RESERVED_TASK_IDS
        or not TASK_ID_RE.match(task_id)
    ):
        return root, R.AdapterResult.failure(
            HARNESS, "task", R.ERROR, "invalid task id: {!r}".format(task_id)
        )
    candidate = root / task_id
    if not is_inside(root, candidate):
        return root, R.AdapterResult.failure(
            HARNESS, "task", R.ERROR, "invalid task id: {!r}".format(task_id)
        )
    return candidate, None


def _validate_timeout(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(timeout, problem)`` after checking the timeout contract.

    A timeout has to be a finite number greater than 0 and at most 86400
    seconds. The runner maps every non positive timeout to "no limit", so an
    unchecked 0 or NaN would quietly turn a bounded request into an unbounded
    one. The task store checks it as well as the backend, because the task
    store is what writes the value into the task directory.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "timeout must be a number, got {}".format(type(value).__name__)
    number = float(value)
    if not math.isfinite(number):
        return None, "timeout must be a finite number, got {}".format(number)
    if number <= 0:
        return None, "timeout must be greater than 0 seconds, got {}".format(number)
    if number > MAX_TIMEOUT:
        return None, "timeout must be at most {} seconds, got {}".format(
            int(MAX_TIMEOUT), number
        )
    return number, None


def start(
    backend: Any,
    prompt: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    tools: Optional[str] = None,
    disabled_tools: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: float = 900.0,
) -> R.AdapterResult:
    fault = prompt_problem(prompt)
    if fault is None:
        for name, value in (
            ("provider", provider),
            ("model", model),
            ("tools", tools),
            ("disabled_tools", disabled_tools),
        ):
            fault = option_problem(name, value)
            if fault is not None:
                break
    if fault is not None:
        return R.AdapterResult.failure(HARNESS, "start", R.ERROR, fault)
    checked, problem = _validate_timeout(timeout)
    if problem is not None or checked is None:
        return R.AdapterResult.failure(HARNESS, "start", R.ERROR, problem or "bad timeout")

    try:
        ensure_private_dir(backend.tasks_dir)
    except OSError as exc:
        return R.AdapterResult.failure(
            HARNESS, "start", R.ERROR, "could not create tasks dir: {}".format(exc)
        )
    task_id = "t-{}-{}".format(int(time.time()), uuid.uuid4().hex[:8])
    task_dir = backend.tasks_dir / task_id
    try:
        ensure_private_dir(task_dir, exist_ok=False)
    except OSError as exc:
        return R.AdapterResult.failure(
            HARNESS, "start", R.ERROR, "could not create task dir: {}".format(exc)
        )

    argv = backend.build_argv(prompt, provider, model, tools, disabled_tools)
    nonce = uuid.uuid4().hex
    meta: Dict[str, Any] = {
        "id": task_id,
        "nonce": nonce,
        "prompt": prompt,
        "argv": argv,
        "provider": provider,
        "model": model,
        "tools": tools,
        "cwd": cwd,
        "timeout_secs": checked,
        "started_at": time.time(),
        "pid": None,
        "wrapper_pgid": None,
    }
    try:
        write_json_private(task_dir / "meta.json", meta)
    except OSError as exc:
        return R.AdapterResult.failure(
            HARNESS, "start", R.ERROR, "could not write meta.json: {}".format(exc)
        )

    wrapper = [
        sys.executable,
        "-m",
        "jcode_bridge.taskrunner",
        "--dir",
        str(task_dir),
        "--timeout",
        str(float(checked)),
        "--nonce",
        nonce,
        "--",
    ] + argv
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env.pop("PYTHONHOME", None)

    try:
        proc = subprocess.Popen(
            wrapper,
            cwd=cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return R.AdapterResult.failure(
            HARNESS, "start", R.ERROR, "could not spawn task: {}".format(exc)
        )
    _track(proc)

    # The wrapper starts a new session, so it leads its own process group and
    # the group id is its pid. The nonce never changes: the runner echoes it
    # back, and cancel() only trusts a runner record that carries the same one.
    meta["pid"] = proc.pid
    meta["wrapper_pgid"] = proc.pid
    try:
        write_json_private(task_dir / "meta.json", meta)
    except OSError as exc:
        return R.AdapterResult.failure(
            HARNESS, "start", R.ERROR, "could not update meta.json: {}".format(exc)
        )
    return R.AdapterResult.success(
        HARNESS,
        "start",
        "task {} started".format(task_id),
        data={
            "task_id": task_id,
            "pid": proc.pid,
            "task_dir": str(task_dir),
            "timeout_secs": checked,
        },
    )


def _state(meta: Dict[str, Any], result: Dict[str, Any], cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if result:
        ok = (
            result.get("exit_code") == 0
            and not result.get("timed_out")
            and not result.get("error")
        )
        return "completed" if ok else "failed"
    if pid_alive(meta.get("pid")):
        return "running"
    return "unknown"


def status(backend: Any, task_id: str) -> R.AdapterResult:
    task_dir, problem = _task_dir(backend, task_id)
    if problem:
        return problem
    meta = read_json(task_dir / "meta.json")
    result = read_json(task_dir / "result.json")
    cancelled = (task_dir / "cancelled.json").exists()
    state = _state(meta, result, cancelled)

    data = {
        "task_id": task_id,
        "state": state,
        "alive": pid_alive(meta.get("pid")),
        "started_at": meta.get("started_at"),
        "pid": meta.get("pid"),
        "result": redact_obj(result),
        "cancelled": cancelled,
        "task_dir": str(task_dir),
        "has_output": (task_dir / "stdout.log").exists(),
    }
    if state in ("running", "completed"):
        return R.AdapterResult.success(
            HARNESS, "status", "task {} {}".format(task_id, state), data=data
        )
    if state in ("cancelled", "failed"):
        return R.AdapterResult.failure(
            HARNESS, "status", R.ERROR, "task {}".format(state), data=data
        )
    return R.AdapterResult.failure(
        HARNESS, "status", R.ERROR, "task {} state unknown".format(task_id), data=data
    )


def output(backend: Any, task_id: str, tail_chars: int = 20000) -> R.AdapterResult:
    task_dir, problem = _task_dir(backend, task_id)
    if problem:
        return problem
    if not task_dir.is_dir():
        return R.AdapterResult.failure(
            HARNESS, "output", R.ERROR, "unknown task {}".format(task_id)
        )
    stdout = tail(task_dir / "stdout.log", tail_chars)
    stderr = tail(task_dir / "stderr.log", tail_chars)
    answer, _parsed = extract_answer(stdout)
    result = redact_obj(read_json(task_dir / "result.json"))
    return R.AdapterResult.success(
        HARNESS,
        "output",
        "{} chars captured".format(len(stdout)),
        data={
            "task_id": task_id,
            "answer": redact(answer),
            "result": result,
            "task_dir": str(task_dir),
        },
        stdout=redact(stdout),
        stderr=redact(stderr),
    )


def _trusted_runner(meta: Dict[str, Any], runner: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    """Return ``(problem, wrapper_pid)`` when the runner record can be trusted.

    The runner record is only believable when it carries the same nonce the
    task was started with and when it names the same wrapper process the task
    recorded. Anything else is a stale or foreign file, and the caller must
    not act on it.
    """
    if not runner:
        return "task has no runner record to trust", None
    meta_nonce = meta.get("nonce")
    runner_nonce = runner.get("nonce")
    if not meta_nonce or not runner_nonce or meta_nonce != runner_nonce:
        return "runner record does not carry the task nonce", None
    meta_pid = coerce_int(meta.get("pid"))
    wrapper_pid = coerce_int(runner.get("wrapper_pid"))
    if meta_pid is None or wrapper_pid is None or meta_pid != wrapper_pid:
        return "runner record does not name the recorded wrapper", None
    return None, wrapper_pid


def _signal_groups(groups: List[int], pids: List[int], grace: float) -> None:
    """Terminate ``groups`` politely, then forcefully."""
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline and any(pid_alive(pid) for pid in pids):
        time.sleep(0.05)
    if not any(pid_alive(pid) for pid in pids):
        return
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def cancel(backend: Any, task_id: str, grace: float = 2.0) -> R.AdapterResult:
    task_dir, problem = _task_dir(backend, task_id)
    if problem:
        return problem
    if not task_dir.is_dir():
        return R.AdapterResult.failure(
            HARNESS, "cancel", R.ERROR, "unknown task {}".format(task_id)
        )
    meta = read_json(task_dir / "meta.json")
    runner = read_json(task_dir / "runner.json")
    trust_problem, wrapper_pid = _trusted_runner(meta, runner)
    if trust_problem is not None or wrapper_pid is None:
        return R.AdapterResult.failure(
            HARNESS, "cancel", R.ERROR, "task {} {}".format(task_id, trust_problem or "is not trusted")
        )

    child_pid = coerce_int(runner.get("child_pid"))
    if child_pid is not None and descends_from(child_pid, wrapper_pid) is False:
        return R.AdapterResult.failure(
            HARNESS,
            "cancel",
            R.ERROR,
            "task {} runner does not describe the recorded wrapper".format(task_id),
        )
    tracked = [pid for pid in (wrapper_pid, child_pid) if pid is not None]
    if not any(pid_alive(pid) for pid in tracked):
        return R.AdapterResult.failure(
            HARNESS, "cancel", R.ERROR, "task {} is not running".format(task_id)
        )

    # The kill target comes from the runner record and never from a lookup of
    # the recorded pid: a pid may have been recycled by now, and the group it
    # belongs to now has nothing to do with this task.
    own_group = os.getpgrp()
    groups: List[int] = []
    for value in (runner.get("child_pgid"), runner.get("wrapper_pgid")):
        pgid = coerce_int(value)
        if pgid is None or pgid <= 1 or pgid == own_group:
            continue
        if pgid not in groups:
            groups.append(pgid)
    if not groups:
        return R.AdapterResult.failure(
            HARNESS,
            "cancel",
            R.ERROR,
            "task {} has no process group this bridge may signal".format(task_id),
        )

    try:
        grace_seconds = max(0.0, float(grace))
    except (TypeError, ValueError):
        grace_seconds = 0.0
    _signal_groups(groups, tracked, grace_seconds)

    try:
        os.waitpid(wrapper_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    still_alive = any(pid_alive(pid) for pid in tracked)
    try:
        write_json_private(
            task_dir / "cancelled.json", {"at": time.time(), "pid": wrapper_pid}
        )
    except OSError as exc:
        return R.AdapterResult.failure(
            HARNESS, "cancel", R.ERROR, "could not write cancelled.json: {}".format(exc)
        )
    return R.AdapterResult.success(
        HARNESS,
        "cancel",
        "task {} cancelled".format(task_id),
        data={"task_id": task_id, "pid": wrapper_pid, "still_alive": still_alive},
    )


def list_tasks(backend: Any, limit: int = 20) -> R.AdapterResult:
    tasks: List[Dict[str, Any]] = []
    tasks_root = backend.tasks_dir
    if tasks_root.is_dir():
        dirs = sorted(
            (path for path in tasks_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for task_dir in dirs[: max(1, int(limit))]:
            meta = read_json(task_dir / "meta.json")
            result = read_json(task_dir / "result.json")
            cancelled = (task_dir / "cancelled.json").exists()
            prompt = meta.get("prompt")
            tasks.append(
                {
                    "task_id": task_dir.name,
                    "state": _state(meta, result, cancelled),
                    "started_at": meta.get("started_at"),
                    "prompt": redact(prompt) if isinstance(prompt, str) else prompt,
                }
            )
    return R.AdapterResult.success(
        HARNESS, "list", "{} task(s)".format(len(tasks)), data={"tasks": tasks}
    )
