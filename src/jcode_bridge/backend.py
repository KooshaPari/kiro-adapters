"""Jcode backend: synchronous and asynchronous task dispatch to ``jcode``.

Both shapes share one argv builder, so a caller cannot drift between what the
sync path runs and what the detached path runs. Every result is the same
``AdapterResult`` envelope the Kiro adapters use.

Detached task mechanics live in :mod:`jcode_bridge.tasks`; payload parsing and
OS helpers live in :mod:`jcode_bridge.payload`.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from kiro_adapters import result as R
from kiro_adapters.redact import redact, redact_obj
from kiro_adapters.runner import CommandResult, resolve_binary, run as run_command

from . import tasks as task_store
from .payload import extract_answer, option_problem, prompt_problem

HARNESS = "jcode"
ENV_VAR = "JCODE_BRIDGE_BIN"
HOME_ENV = "JCODE_BRIDGE_HOME"
DEFAULT_HOME = "~/.jcode-bridge"
DEFAULT_TIMEOUT = 900.0
PROBE_TIMEOUT = 60.0
MAX_TIMEOUT = 86400.0


def _validate_timeout(
    value: Optional[float],
    default: float = DEFAULT_TIMEOUT,
) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(timeout, problem)`` after checking the shared timeout contract.

    A timeout has to be a finite number greater than 0 and at most 86400
    seconds. The runner maps every non positive timeout to "no limit", so an
    unchecked 0 or NaN would quietly turn a bounded request into an unbounded
    one. The contract is enforced here, at the public edge, so the promise the
    caller made survives the whole call.
    """
    if value is None:
        return default, None
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


def _validate_cwd(cwd: Optional[str]) -> Optional[str]:
    """Return a message when ``cwd`` is not an existing directory.

    A missing directory surfaces as a spawn error, which reads like a missing
    binary. Naming the directory keeps the caller on the right path instead of
    sending it to install something that is already installed.
    """
    if cwd is None:
        return None
    expanded = os.path.expanduser(cwd)
    if not os.path.isdir(expanded):
        return "working directory does not exist or is not a directory: {}".format(cwd)
    return None


# A spawn failure that names one of these exceptions means the binary itself
# could not be used: it is not installed, or it is installed but not
# executable. Nothing else is a statement about the installation.
UNUSABLE_BINARY_ERRORS = ("FileNotFoundError", "PermissionError")


def _spawn_error_code(spawn_error: str) -> str:
    """Return the error code that describes a spawn failure.

    Only a genuinely absent or unusable binary means "not installed". Every
    other spawn failure, an argument list that is too long for example, is an
    error of this call and not a missing installation. The runner records the
    failure as "<ExceptionTypeName>: <message>", so the type name at the start
    of the string decides, and an unrecognised name is treated as an error.
    """
    name = spawn_error.split(":", 1)[0].strip()
    if name in UNUSABLE_BINARY_ERRORS:
        return R.NOT_INSTALLED
    return R.ERROR


def _option_fault(
    provider: Optional[str],
    model: Optional[str],
    tools: Optional[str],
    disabled_tools: Optional[str],
) -> Optional[str]:
    """Return the first problem with an option value, if any.

    The options travel to the child as argv elements, so they are held to the
    same rules as the prompt: text only, and no NUL byte.
    """
    for name, value in (
        ("provider", provider),
        ("model", model),
        ("tools", tools),
        ("disabled_tools", disabled_tools),
    ):
        fault = option_problem(name, value)
        if fault is not None:
            return fault
    return None


def _redact_result(result: R.AdapterResult) -> R.AdapterResult:
    """Return ``result`` with every credential looking string redacted.

    The detached path keeps the prompt in the task directory, so a raw result
    can carry a secret that the synchronous path already hides. Redacting the
    whole envelope keeps the shape intact while the payload stops travelling
    in the clear.
    """
    return replace(
        result,
        detail=redact(result.detail),
        data=cast(Dict[str, Any], redact_obj(result.data)),
        stdout=redact(result.stdout),
        stderr=redact(result.stderr),
        hints=[redact(hint) for hint in result.hints],
    )


class JcodeBackend:
    """Drive ``jcode run`` as a service, synchronously or detached."""

    harness = HARNESS

    def __init__(
        self,
        binary: Optional[str] = None,
        home: Optional[str] = None,
        env_var: str = ENV_VAR,
    ) -> None:
        self._binary = binary
        self.env_var = env_var
        resolved_home = home or os.environ.get(HOME_ENV) or DEFAULT_HOME
        self.home = Path(os.path.expanduser(resolved_home))
        self._resolved: Optional[str] = None
        self._tried: List[str] = []

    # -- resolution -------------------------------------------------------
    @property
    def binary(self) -> Optional[str]:
        if self._binary:
            return self._binary
        if self._resolved is None:
            self._resolved, self._tried = resolve_binary(("jcode",), self.env_var)
        return self._resolved

    @property
    def tasks_dir(self) -> Path:
        return self.home / "tasks"

    def resolve_info(self) -> Dict[str, Any]:
        return {
            "binary": self.binary,
            "found": self.binary is not None,
            "tried": self._tried if not self._binary else ["explicit:{}".format(self._binary)],
            "home": str(self.home),
        }

    def _missing(self, action: str) -> R.AdapterResult:
        return R.AdapterResult.failure(
            HARNESS,
            action,
            R.NOT_INSTALLED,
            "jcode binary not found",
            data=self.resolve_info(),
            hints=[
                "Ensure `jcode --version` works in this shell",
                "Or pin the binary: export {}=/path/to/jcode".format(self.env_var),
            ],
        )

    # -- argv -------------------------------------------------------------
    def build_argv(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[str] = None,
        disabled_tools: Optional[str] = None,
    ) -> List[str]:
        argv = [self.binary or "jcode", "run", "--json"]
        if provider:
            argv.extend(["-p", provider])
        if model:
            argv.extend(["-m", model])
        if tools:
            argv.extend(["--tools", tools])
        if disabled_tools:
            argv.extend(["--disabled-tools", disabled_tools])
        argv.append(prompt)
        return argv

    # -- health -----------------------------------------------------------
    def health(self, timeout: float = PROBE_TIMEOUT) -> R.AdapterResult:
        checked, problem = _validate_timeout(timeout, default=PROBE_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "health", R.ERROR, problem)
        effective_timeout: float = checked if checked is not None else PROBE_TIMEOUT
        if self.binary is None:
            return self._missing("health")
        cmd = run_command([self.binary, "version", "--json"], timeout=effective_timeout)
        version: Optional[str] = None
        if cmd.ok:
            try:
                payload = json.loads(cmd.stdout)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                candidate = payload.get("version") or payload.get("git_hash")
                version = str(candidate) if candidate else None
        if version is None:
            fallback = run_command([self.binary, "--version"], timeout=effective_timeout)
            lines = (fallback.stdout or fallback.stderr or "").strip().splitlines()
            version = lines[0] if lines else None
        data = {
            "binary": self.binary,
            "version": version,
            "home": str(self.home),
            "tasks_dir": str(self.tasks_dir),
            "resolve": self.resolve_info(),
        }
        if version is None:
            return _redact_result(
                R.AdapterResult.failure(
                    HARNESS,
                    "health",
                    R.ERROR,
                    "jcode did not report a version",
                    data=data,
                    stdout=redact(cmd.stdout),
                    stderr=redact(cmd.stderr),
                    duration_ms=cmd.duration_ms,
                )
            )
        return _redact_result(
            R.AdapterResult.success(
                HARNESS,
                "health",
                version,
                data=data,
                duration_ms=cmd.duration_ms,
            )
        )

    # -- synchronous ------------------------------------------------------
    def run(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[str] = None,
        disabled_tools: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> R.AdapterResult:
        fault = prompt_problem(prompt) or _option_fault(
            provider, model, tools, disabled_tools
        )
        if fault is not None:
            return R.AdapterResult.failure(HARNESS, "run", R.ERROR, fault)
        cwd_problem = _validate_cwd(cwd)
        if cwd_problem is not None:
            return R.AdapterResult.failure(HARNESS, "run", R.ERROR, cwd_problem)
        checked, problem = _validate_timeout(timeout, default=DEFAULT_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "run", R.ERROR, problem)
        if self.binary is None:
            return self._missing("run")
        effective_timeout: float = checked if checked is not None else DEFAULT_TIMEOUT
        argv = self.build_argv(prompt, provider, model, tools, disabled_tools)
        cmd = run_command(argv, timeout=effective_timeout, cwd=cwd)
        answer, parsed = extract_answer(cmd.stdout)
        data: Dict[str, Any] = {
            "answer": redact(answer),
            "provider": provider,
            "model": model,
            "tools": tools,
            "parsed": parsed,
            "argv": cmd.argv,
            "exit_code": cmd.code,
        }
        return _redact_result(
            self._result_from_command("run", cmd, data=data, answer_chars=len(answer))
        )

    # -- detached ---------------------------------------------------------
    def start(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[str] = None,
        disabled_tools: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> R.AdapterResult:
        fault = prompt_problem(prompt) or _option_fault(
            provider, model, tools, disabled_tools
        )
        if fault is not None:
            return R.AdapterResult.failure(HARNESS, "start", R.ERROR, fault)
        cwd_problem = _validate_cwd(cwd)
        if cwd_problem is not None:
            return R.AdapterResult.failure(HARNESS, "start", R.ERROR, cwd_problem)
        checked, problem = _validate_timeout(timeout, default=DEFAULT_TIMEOUT)
        if problem is not None:
            return R.AdapterResult.failure(HARNESS, "start", R.ERROR, problem)
        if self.binary is None:
            return self._missing("start")
        effective_timeout: float = checked if checked is not None else DEFAULT_TIMEOUT
        return _redact_result(
            task_store.start(
                self,
                prompt,
                provider=provider,
                model=model,
                tools=tools,
                disabled_tools=disabled_tools,
                cwd=cwd,
                timeout=effective_timeout,
            )
        )

    def status(self, task_id: str) -> R.AdapterResult:
        return _redact_result(task_store.status(self, task_id))

    def output(self, task_id: str, tail_chars: int = 20000) -> R.AdapterResult:
        return _redact_result(task_store.output(self, task_id, tail_chars))

    def cancel(self, task_id: str, grace: float = 2.0) -> R.AdapterResult:
        return task_store.cancel(self, task_id, grace)

    def list_tasks(self, limit: int = 20) -> R.AdapterResult:
        return _redact_result(task_store.list_tasks(self, limit))

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _result_from_command(
        action: str,
        cmd: CommandResult,
        data: Dict[str, Any],
        answer_chars: int = 0,
    ) -> R.AdapterResult:
        if cmd.spawn_error:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                _spawn_error_code(cmd.spawn_error),
                "failed to launch jcode: {}".format(cmd.spawn_error),
                data=data,
                duration_ms=cmd.duration_ms,
            )
        if cmd.timed_out:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                R.TIMEOUT,
                "jcode run timed out",
                data=data,
                stdout=redact(cmd.stdout),
                stderr=redact(cmd.stderr),
                duration_ms=cmd.duration_ms,
            )
        if cmd.code != 0:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                R.ERROR,
                "jcode run exited {}".format(cmd.code),
                data=data,
                stdout=redact(cmd.stdout),
                stderr=redact(cmd.stderr),
                duration_ms=cmd.duration_ms,
            )
        return R.AdapterResult.success(
            HARNESS,
            action,
            "{} chars".format(answer_chars),
            data=data,
            stdout=redact(cmd.stdout),
            stderr=redact(cmd.stderr),
            duration_ms=cmd.duration_ms,
        )
