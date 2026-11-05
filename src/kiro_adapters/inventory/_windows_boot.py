"""A read-only Windows boot epoch, from CIM, cached for this process run.

Windows has no ``/proc`` and no ``kern.boottime``. The honest per-boot marker
the platform does offer is ``Win32_OperatingSystem.LastBootUpTime``: it is
constant for the whole boot and changes when the host reboots. It is read
through the same bounded :func:`_run_capture` path as the rest of the Windows
probes, so a wedged or chatty PowerShell cannot exhaust memory, and every
failure - missing shell, non-zero exit, stderr, truncation, timeout, stalled
reader, malformed text - is reported as ``None`` (unknown), never as a value.

The result is cached with :func:`functools.lru_cache`, so at most one
PowerShell child runs per process run. A failed read is cached as well: this
module would rather keep refusing identity for the rest of the run than
silently change its answer mid-run.

This module is read-only and never writes.
"""

from __future__ import annotations

import datetime
import functools
import re
from typing import Optional

from ._windows_table import (
    _POWERSHELL_PREAMBLE,
    _powershell_path,
    _run_capture,
)

# ``DateTime.ToString('o')`` renders UTC as
# ``2026-11-03T00:00:00.0000000Z``. That round-trip format always emits
# exactly seven fractional digits; requiring that shape rejects an arbitrary
# string that merely looks timestamp-ish.
_BOOT_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$"
)

_TIMEOUT_SECONDS = 15


def read_windows_boot_epoch() -> Optional[str]:
    """One bounded CIM read of ``LastBootUpTime``, or ``None``.

    ``None`` means the probe could not produce a trustworthy value. Callers
    place a process identity on a boot timeline only when this returns a
    value; a failed read is not evidence that any two processes share a boot.
    """
    shell = _powershell_path()
    if shell is None:
        return None
    # NOTE: `_POWERSHELL_PREAMBLE` is written with DOUBLED braces because it is
    # designed to go through ``str.format``. Every literal brace in this script
    # must be doubled for the same reason, and ``.format()`` MUST be called.
    #
    # Measured consequence of not calling it, on a real Windows host: PowerShell
    # does not error. It treats ``{{ ... }}`` as a nested script BLOCK and emits
    # that block's text to stdout, so the output becomes two lines - the block
    # text, then the epoch. This function's own single-line guard
    # (``"\n" in text``) then rejects the value and the boot epoch reads as
    # unknown. Silent, with exit code 0 and empty stderr, which is why every
    # mocked test passed while the path was dead on the host: the mock returned a
    # value and never executed the script.
    script = (
        _POWERSHELL_PREAMBLE
        + "$os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop; "
        "if ($null -eq $os -or $null -eq $os.LastBootUpTime) {{ exit 1 }}; "
        "$os.LastBootUpTime.ToUniversalTime().ToString('o')"
    ).format()
    # Cheap guard against the same mistake returning: a rendered script must not
    # still contain the escape sequences.
    if "{{" in script or "}}" in script:  # pragma: no cover - defensive
        raise AssertionError("unrendered PowerShell braces")
    out = _run_capture(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=_TIMEOUT_SECONDS,
    )
    if not out.ok:
        return None
    # PowerShell can print a non-terminating error and still exit 0.
    if out.stderr.strip():
        return None
    text = out.stdout.strip().lstrip("\ufeff").strip()
    if not text or "\n" in text:
        return None
    if not _BOOT_TIME_RE.match(text):
        return None
    try:
        # The regexp constrains the shape, not the calendar; reject impossible
        # dates so a misbehaving probe cannot become a stable-looking epoch.
        datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return text


@functools.lru_cache(maxsize=1)
def windows_boot_epoch() -> Optional[str]:
    """The boot epoch, read once per process run.

    :func:`functools.lru_cache` also caches ``None``, so a transient failure
    is not retried mid-run; identity simply stays unknown until the next run.
    """
    return read_windows_boot_epoch()
