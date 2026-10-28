"""Shared helpers for the kiro-adapters test suite.

Standard library only. Nothing here touches the real ``~/.kiro`` tree or a real
``kirocrew`` / ``kirocode`` install: every test that needs a binary gets a
fake POSIX shell script written into a temporary directory and pointed at
explicitly (``binary=`` constructor argument or an env override).

Import convention inside the test modules::

    import os
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from _support import fake_bin, env_vars
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

# Result codes mirrored here so tests do not have to import the package for
# pure-assertion helpers. Keep in sync with kiro_adapters.result.
OK = "OK"
NOT_INSTALLED = "NOT_INSTALLED"
BLOCKED = "BLOCKED"
TIMEOUT = "TIMEOUT"
ERROR = "ERROR"
UNSUPPORTED = "UNSUPPORTED"


# ---------------------------------------------------------------------------
# temporary directories
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def temp_dir(prefix: str = "kiro-adapters-test-") -> Iterator[Path]:
    """Yield a fresh temporary directory that is removed on exit."""
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(holder.name)
    finally:
        holder.cleanup()


# ---------------------------------------------------------------------------
# fake binaries
# ---------------------------------------------------------------------------
def fake_bin(directory: os.PathLike, name: str, body: str) -> str:
    """Write an executable fake CLI into ``directory`` and return its path.

    ``body`` is the script text. A ``#!/bin/sh`` shebang is prepended when the
    body does not already start with one. The file is chmod'ed 0o755 so it can
    actually be executed by :mod:`subprocess`.

    The script receives no arguments contract: write it so that it works for
    whatever argv the adapter passes. Use ``"$@"`` to echo arguments back.
    """
    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True)
    path = directory_path / name
    text = body if body.lstrip().startswith("#!") else "#!/bin/sh\n" + body
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def fake_bin_with_marker(
    directory: os.PathLike,
    name: str,
    marker: os.PathLike,
    body: str = "",
) -> str:
    """Like :func:`fake_bin`, but the script appends its argv to ``marker``.

    The marker file lets a test assert that a subcommand was *not* invoked
    (the file must not exist) or that it *was* invoked with specific
    arguments (read the file and compare).
    """
    marker_path = Path(marker)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    prelude = 'printf "%s\\n" "$*" >> {}\n'.format(_shell_quote(str(marker_path)))
    return fake_bin(directory, name, prelude + body)


def _shell_quote(value: str) -> str:
    """Single-quote a value for safe interpolation into a POSIX shell script."""
    return "'" + str(value).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# marker files
# ---------------------------------------------------------------------------
def marker_exists(marker: os.PathLike) -> bool:
    return Path(marker).exists()


def marker_lines(marker: os.PathLike) -> List[str]:
    """Return the recorded invocation lines of a marker file (empty if absent)."""
    path = Path(marker)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def marker_text(marker: os.PathLike) -> str:
    path = Path(marker)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def env_vars(
    overrides: Optional[Dict[str, Optional[str]]] = None,
    **kwargs: Optional[str],
) -> Iterator[None]:
    """Temporarily set environment variables and restore them afterwards.

    A value of ``None`` removes the variable for the duration of the block.
    Every other value is set to ``str(value)``.
    """
    values: Dict[str, Optional[str]] = dict(overrides or {})
    values.update(kwargs)  # type: ignore[arg-type]

    saved: Dict[str, Optional[str]] = {
        key: os.environ.get(key) for key in values
    }
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, original in saved.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


@contextlib.contextmanager
def isolated_home(home: os.PathLike) -> Iterator[Path]:
    """Point ``$HOME`` at ``home`` so ``~`` expansions never reach the real home."""
    home_path = Path(home)
    home_path.mkdir(parents=True, exist_ok=True)
    with env_vars(HOME=str(home_path)):
        yield home_path


def path_without(binary_dir: os.PathLike, names: Sequence[str] = ()) -> str:
    """Build a ``PATH`` that contains only ``binary_dir`` (plus /usr/bin:/bin).

    Keeps a fake binary discoverable through ``shutil.which`` without exposing
    any real ``kirocrew`` / ``kirocode`` binary.
    """
    parts = [str(Path(binary_dir).resolve())]
    for extra in ("/usr/bin", "/bin"):
        if extra not in parts:
            parts.append(extra)
    return os.pathsep.join(parts)


def which_in(directory: os.PathLike, name: str) -> Optional[str]:
    """``shutil.which`` scoped to a single directory."""
    found = shutil.which(name, path=str(directory))
    return found


def is_executable(path: os.PathLike) -> bool:
    mode = os.stat(str(path)).st_mode
    return bool(mode & stat.S_IXUSR)


# ---------------------------------------------------------------------------
# result assertions
# ---------------------------------------------------------------------------
def assert_result_ok(result: Any, code: str = OK) -> None:
    """Assert an :class:`AdapterResult` is a success with the expected code."""
    assert result.ok is True, "expected ok=True, got ok={!r} code={!r}".format(
        getattr(result, "ok", None), getattr(result, "code", None)
    )
    assert result.code == code, "expected code {!r}, got {!r}".format(
        code, getattr(result, "code", None)
    )


def assert_result_failed(result: Any, code: Optional[str] = None) -> None:
    """Assert an :class:`AdapterResult` is a failure, optionally with a code."""
    assert result.ok is False, "expected ok=False, got ok={!r}".format(
        getattr(result, "ok", None)
    )
    if code is not None:
        assert result.code == code, "expected code {!r}, got {!r}".format(
            code, getattr(result, "code", None)
        )


def assert_data(result: Any, key: str, expected: Any) -> None:
    """Assert ``result.data[key] == expected`` with a readable failure message."""
    data = getattr(result, "data", {}) or {}
    assert key in data, "missing data key {!r} in {!r}".format(key, sorted(data))
    actual = data[key]
    assert actual == expected, "data[{!r}] expected {!r}, got {!r}".format(
        key, expected, actual
    )
