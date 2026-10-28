"""Parsing and small OS helpers shared by the jcode backend.

``extract_answer`` is deliberately tolerant: the ``jcode run --json`` schema was
not pinned when this bridge was written, so an unrecognised payload degrades to
the raw text rather than to an empty answer.

This module also owns the file mode contract. A task directory holds a prompt
and a captured transcript, so it belongs to the owner alone: directories are
0700 and files are 0600. Files are written through a temporary file in the
same directory and then renamed, so a reader never sees a half written file
and never sees a briefly world readable one.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

TAIL_CHARS = 20000
FILE_MODE = 0o600
DIR_MODE = 0o700
_ANSWER_KEYS = ("text", "response", "answer", "output", "content", "result", "message")


def extract_answer(stdout: str) -> "tuple[str, Optional[Any]]":
    """Return ``(answer, parsed_or_None)`` from a JSON payload or raw text."""
    raw = (stdout or "").strip()
    if not raw:
        return "", None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw, None
    if isinstance(parsed, dict):
        for key in _ANSWER_KEYS:
            value = parsed.get(key)
            if isinstance(value, str):
                return value, parsed
        messages = parsed.get("messages")
        if isinstance(messages, list):
            parts = [
                item.get("content")
                for item in messages
                if isinstance(item, dict) and isinstance(item.get("content"), str)
            ]
            if parts:
                return "\n".join(parts), parsed
    if isinstance(parsed, list):
        parts = [item for item in parsed if isinstance(item, str)]
        if parts:
            return "\n".join(parts), parsed
    return raw, parsed


def prompt_problem(prompt: Any) -> Optional[str]:
    """Return a message when ``prompt`` cannot be handed to a child process.

    A prompt reaches the operating system as one argv element, and an argv
    element cannot carry a NUL byte. Passing one raises ``ValueError`` inside
    ``subprocess``, which would escape the bridge as a raw exception instead of
    coming back as a result the caller can read. The same goes for a prompt
    that is not a string at all.
    """
    if not isinstance(prompt, str):
        return "prompt must be a string, got {}".format(type(prompt).__name__)
    if not prompt.strip():
        return "prompt must not be empty"
    if "\x00" in prompt:
        return "prompt must not contain a NUL byte"
    return None


def pid_alive(pid: Optional[int]) -> bool:
    """True when ``pid`` exists and we are allowed to signal it."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def tail(path: Path, limit: int = TAIL_CHARS) -> str:
    """Read the last ``limit`` characters of a file, noting what was dropped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return "...[truncated {} chars]\n".format(len(text) - limit) + text[-limit:]


def read_json(path: Path) -> dict:
    """Read a JSON object; return ``{}`` for a missing or malformed file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parent_pid(pid: int) -> Optional[int]:
    """Return the parent pid of ``pid``, or ``None`` when it cannot be read.

    Linux answers from procfs, macOS has no procfs so the lookup falls back
    to ``ps``. A process that is already gone has no answer, which the
    callers treat as "unknown" rather than as "no such parent".
    """
    try:
        with open("/proc/{}/status".format(pid), "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    for program in ("/bin/ps", "ps"):
        try:
            completed = subprocess.run(
                [program, "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = (completed.stdout or "").strip()
        if not text:
            return None
        try:
            return int(text.splitlines()[0].strip())
        except (ValueError, IndexError):
            return None
    return None


def coerce_int(value: Any) -> Optional[int]:
    """Return ``value`` as an int, or ``None`` when it is not a usable number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def is_inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` really lives under ``root``."""
    root_real = os.path.realpath(str(root))
    child_real = os.path.realpath(str(candidate))
    return child_real == root_real or child_real.startswith(root_real + os.sep)


def descends_from(pid: int, ancestor: int, depth: int = 8) -> Optional[bool]:
    """True when ``pid`` is a descendant of ``ancestor``.

    ``None`` means the relation could not be established, for example because
    the process is already gone. An unknown relation is not a rejection: the
    caller has no evidence either way.
    """
    current = pid
    for _ in range(depth):
        parent = parent_pid(current)
        if parent is None:
            return None
        if parent == ancestor:
            return True
        if parent <= 1:
            return False
        current = parent
    return False


def ensure_private_dir(path: Path, exist_ok: bool = True) -> Path:
    """Create ``path`` with its parents and force the owner only mode.

    ``mkdir`` honours the process umask, so the mode is set explicitly
    afterwards: a task directory must be 0700 whatever the umask happens to
    be on the machine that runs the bridge. With ``exist_ok`` false an
    existing directory is an error, which is what a fresh task directory
    needs.
    """
    path.mkdir(parents=True, exist_ok=exist_ok)
    os.chmod(str(path), DIR_MODE)
    return path


def write_json_private(path: Path, payload: Dict[str, Any]) -> None:
    """Write ``payload`` as JSON with mode 0600, atomically.

    The temporary file lives in the same directory so the rename stays inside
    one filesystem, and it is created with mode 0600 before any content is
    written. The final mode is set explicitly because ``os.open`` applies the
    process umask to the mode it is given.
    """
    tmp = path.parent / ".{}.{}.tmp".format(path.name, uuid.uuid4().hex)
    handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=1, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(tmp), FILE_MODE)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
