"""Store layout and confined reads for the Orca agent-hook adapter.

Orca writes two cooperating artifacts beside its Electron profile:

``agent-hooks/endpoint.env``
    A small ``KEY=value`` file that states how to reach the local agent-hook
    endpoint: ``ORCA_AGENT_HOOK_PORT``, ``ORCA_AGENT_HOOK_TRANSPORT``,
    ``ORCA_AGENT_HOOK_ENV``, ``ORCA_AGENT_HOOK_VERSION``,
    ``ORCA_AGENT_HOOK_OPENCODE_TUI`` **and** ``ORCA_AGENT_HOOK_TOKEN``.

``agent-hooks/last-status.json``
    Orca's own last-known status document. Its observed top-level keys are
    ``version`` (int), ``entries`` (mapping) and ``authorityCommitments``
    (mapping). An ``entries`` value is a per-agent status object; on the host
    this adapter was written against the mapping is empty, so the *entry*
    field names are not proven and the envelope builder reads only an
    allowlist of plausible keys (see :mod:`orca_fs`).

Two rules make this adapter safe to point at a real profile:

1. **The token is never read as a value.** The env file is parsed for *key
   names*, and values are taken only for an allowlist of non-secret keys.
   ``ORCA_AGENT_HOOK_TOKEN`` is recognised so it can be reported as present,
   and its value is discarded unexamined.
2. **Both artifacts are path-confined and size-bounded.** A symlink that
   leaves the ``agent-hooks`` directory, or a file over the cap, is treated as
   absent rather than read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._path_safety import contained_path, exact_child, read_text_capped

# Cap on either artifact. Orca's real files are 212 B and 52 B; anything past
# these caps is not a status document this adapter will trust.
MAX_ENDPOINT_ENV_BYTES = 16 * 1024
MAX_LAST_STATUS_BYTES = 256 * 1024

# Env keys whose *values* are non-secret and therefore safe to project.
_SAFE_ENV_VALUES = (
    "ORCA_AGENT_HOOK_PORT",
    "ORCA_AGENT_HOOK_TRANSPORT",
    "ORCA_AGENT_HOOK_ENV",
    "ORCA_AGENT_HOOK_VERSION",
    "ORCA_AGENT_HOOK_OPENCODE_TUI",
)

# Recognised but never valued. Named so ``present`` can be reported honestly.
TOKEN_ENV_KEYS = ("ORCA_AGENT_HOOK_TOKEN",)

ENDPOINT_ENV_NAME = "endpoint.env"
LAST_STATUS_NAME = "last-status.json"


class OrcaStoreLayoutMixin:
    """The Orca profile's directory, and its confined artifacts."""

    orca_dir: Path

    # -- layout ------------------------------------------------------------
    @property
    def agent_hooks_dir(self) -> Path:
        return self.orca_dir / "agent-hooks"

    @property
    def endpoint_env_path(self) -> Path:
        return self.agent_hooks_dir / ENDPOINT_ENV_NAME

    @property
    def last_status_path(self) -> Path:
        return self.agent_hooks_dir / LAST_STATUS_NAME

    def exists(self) -> bool:
        """True when the hooks directory is really there.

        Directory existence only. Nothing is read, so this stays cheap enough
        for ``available_sources`` to call on every discovery request.
        """
        try:
            return self.agent_hooks_dir.is_dir()
        except (OSError, ValueError):
            return False

    # -- confinement -------------------------------------------------------
    def _confined_hook_file(self, name: str) -> Optional[Path]:
        """``agent-hooks/<name>`` resolved, or None when it escapes.

        ``exact_child`` compares realpaths, so a plain file is accepted and a
        symlink whose target lives anywhere else is refused after resolution.
        """
        hooks = contained_path(self.orca_dir, self.agent_hooks_dir)
        if hooks is None or not hooks.is_dir():
            return None
        return exact_child(hooks, name)

    # -- endpoint.env (names and non-secret values only) -------------------
    def endpoint_env(self) -> Dict[str, Any]:
        """Parse ``endpoint.env`` without ever exposing the token.

        Returns ``{"ok": bool, "keys": [...], "values": {safe key: value},
        "token_present": bool}``. A key that is not on the safe list is only
        reported as present in ``keys``; its value is never parsed into the
        result.
        """
        result: Dict[str, Any] = {
            "ok": False,
            "keys": [],
            "values": {},
            "token_present": False,
        }
        path = self._confined_hook_file(ENDPOINT_ENV_NAME)
        if path is None:
            return result
        body = read_text_capped(path, MAX_ENDPOINT_ENV_BYTES)
        if not body:
            return result
        keys: List[str] = []
        values: Dict[str, str] = {}
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            keys.append(key)
            if key in TOKEN_ENV_KEYS:
                # Deliberately not stored. The value is not inspected, not
                # trimmed, not logged.
                result["token_present"] = True
                continue
            if key in _SAFE_ENV_VALUES:
                values[key] = value.strip().strip('"')
        result["ok"] = True
        result["keys"] = keys
        result["values"] = values
        return result

    # -- last-status.json --------------------------------------------------
    def read_last_status(self) -> Optional[Dict[str, Any]]:
        """The parsed status document, or None when absent/refused/unusable."""
        path = self._confined_hook_file(LAST_STATUS_NAME)
        if path is None:
            return None
        body = read_text_capped(path, MAX_LAST_STATUS_BYTES)
        if not body:
            return None
        try:
            document = json.loads(body)
        except (ValueError, TypeError):
            return None
        if not isinstance(document, dict):
            return None
        return document


def default_orca_dir() -> Path:
    """The Orca profile directory for the current user.

    macOS stores Electron app data under ``~/Library/Application Support``.
    The path is returned unresolved; confinement resolves it when it matters.
    """
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "orca"
