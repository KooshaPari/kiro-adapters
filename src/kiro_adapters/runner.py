"""Subprocess execution with timeouts, ANSI stripping and binary resolution.

Deliberately dependency-free and shell-free: every adapter call is an argv
list, so a prompt containing ``; rm -rf /`` is a prompt, not a command.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Inherited variables that make a bundled interpreter import foreign code.
# kirocrew's own launcher scrubs these for the same reason; the adapter scrubs
# them for the child processes it spawns.
_SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME")


def strip_ansi(text: Optional[str]) -> str:
    if not text:
        return ""
    return ANSI_RE.sub("", text)


@dataclass
class CommandResult:
    """Raw outcome of one subprocess invocation."""

    argv: List[str]
    code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: Optional[str] = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return (
            self.spawn_error is None
            and not self.timed_out
            and self.code == 0
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "argv": self.argv,
            "code": self.code,
            "timed_out": self.timed_out,
            "spawn_error": self.spawn_error,
            "duration_ms": self.duration_ms,
        }


def resolve_binary(
    candidates: Sequence[str],
    env_var: Optional[str] = None,
) -> "tuple[Optional[str], List[str]]":
    """Resolve the first usable binary path.

    Order: explicit env override, then PATH lookup for each candidate.
    Returns ``(path_or_None, tried)`` where ``tried`` is diagnostic.
    """
    tried: List[str] = []
    if env_var:
        override = os.environ.get(env_var)
        if override:
            tried.append("{}={}".format(env_var, override))
            if os.path.sep in override:
                if os.path.isfile(override) and os.access(override, os.X_OK):
                    return override, tried
            else:
                found = shutil.which(override)
                if found:
                    return found, tried
    for name in candidates:
        tried.append("which {}".format(name))
        found = shutil.which(name)
        if found:
            return found, tried
    return None, tried


def build_env(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
    if overrides:
        env.update({key: str(value) for key, value in overrides.items()})
    return env


def run(
    argv: Sequence[str],
    timeout: float = 60.0,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    stdin_text: Optional[str] = None,
    scrub_python_env: bool = True,
) -> CommandResult:
    """Run ``argv`` and capture output.

    Never raises for the ordinary failure modes: a missing binary, a non-zero
    exit and a timeout all come back as a populated :class:`CommandResult`.
    """
    argv_list = [str(part) for part in argv]
    child_env = (
        build_env(env)
        if scrub_python_env
        else {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    )

    started = time.time()
    try:
        completed = subprocess.run(
            argv_list,
            cwd=cwd,
            env=child_env,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout if timeout and timeout > 0 else None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(
            argv=argv_list,
            spawn_error="{}: {}".format(type(exc).__name__, exc),
            duration_ms=int((time.time() - started) * 1000),
        )
    except PermissionError as exc:
        return CommandResult(
            argv=argv_list,
            spawn_error="{}: {}".format(type(exc).__name__, exc),
            duration_ms=int((time.time() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=argv_list,
            code=None,
            stdout=strip_ansi(_as_text(exc.stdout)),
            stderr=strip_ansi(_as_text(exc.stderr)),
            timed_out=True,
            duration_ms=int((time.time() - started) * 1000),
        )
    except OSError as exc:
        return CommandResult(
            argv=argv_list,
            spawn_error="{}: {}".format(type(exc).__name__, exc),
            duration_ms=int((time.time() - started) * 1000),
        )

    return CommandResult(
        argv=argv_list,
        code=completed.returncode,
        stdout=strip_ansi(completed.stdout or ""),
        stderr=strip_ansi(completed.stderr or ""),
        duration_ms=int((time.time() - started) * 1000),
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
