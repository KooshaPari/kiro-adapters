"""Capability advertisement for the adoption shim.

The shim preserves everything the fork advertises (src/cli/acp.rs:1704-1748)
and changes exactly two things, both deliberate:

* ``loadSession`` stays true. Adoption is the whole point.
* ``sessionCapabilities.close`` is dropped. The shim will not forward
  ``session/close``, because upstream that cancels a running prompt and stops
  the event pump (src/cli/acp.rs:653-677). It must not advertise what it will
  not do.

The shim also adds ``modes`` with a single ``read-only`` entry and
``currentModeId``, which is what KiroCrew's codex slot gate checks.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .protocol import JSONRPC_VERSION

ACP_PROTOCOL_VERSION = 1
MODE_READ_ONLY = "read-only"
SHIM_NAME = "jcode-acp-adoption-shim"
SHIM_VERSION = "0.1.0"

SUPPORTED_MODES: List[Dict[str, str]] = [
    {
        "id": MODE_READ_ONLY,
        "name": "Read only",
        "description": "Adopt an existing session; never create, close or mutate it.",
    }
]

# Mirrors the fork's advertisement, minus close. Used when upstream initialize
# is unavailable, so KiroCrew can still connect and get a fail-closed load.
FALLBACK_CAPABILITIES: Dict[str, Any] = {
    "loadSession": True,
    "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
    "mcpCapabilities": {"http": False, "sse": False},
    "sessionCapabilities": {"list": {}, "resume": {}},
}


def _capabilities_from(upstream_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(upstream_result, dict):
        advertised = upstream_result.get("agentCapabilities")
        if isinstance(advertised, dict) and advertised:
            return copy.deepcopy(advertised)
    return copy.deepcopy(FALLBACK_CAPABILITIES)


def _apply_contract(
    capabilities: Dict[str, Any],
    mode: str,
    upstream_state: str,
) -> None:
    capabilities["loadSession"] = True

    session_caps = capabilities.get("sessionCapabilities")
    if not isinstance(session_caps, dict):
        session_caps = {}
    session_caps.pop("close", None)
    session_caps.setdefault("list", {})
    capabilities["sessionCapabilities"] = session_caps

    capabilities["modes"] = copy.deepcopy(SUPPORTED_MODES)
    capabilities["currentModeId"] = mode

    meta = capabilities.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    existing = meta.get("jcode.acpShim")
    shim_meta: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    shim_meta.update(
        {
            "name": SHIM_NAME,
            "version": SHIM_VERSION,
            "upstreamState": upstream_state,
            "readOnly": True,
            "spawnsDaemon": False,
            "forwardsSessionClose": False,
        }
    )
    meta["jcode.acpShim"] = shim_meta
    capabilities["_meta"] = meta


def build_initialize_result(
    upstream_result: Optional[Dict[str, Any]] = None,
    mode: str = MODE_READ_ONLY,
    upstream_state: str = "unknown",
) -> Dict[str, Any]:
    """Return the initialize result the shim sends to its client."""
    capabilities = _capabilities_from(upstream_result)
    _apply_contract(capabilities, mode, upstream_state)

    protocol_version = ACP_PROTOCOL_VERSION
    if isinstance(upstream_result, dict):
        advertised = upstream_result.get("protocolVersion")
        if isinstance(advertised, int) and advertised > 0:
            protocol_version = advertised

    auth_methods: List[Any] = []
    if isinstance(upstream_result, dict):
        upstream_auth = upstream_result.get("authMethods")
        if isinstance(upstream_auth, list):
            auth_methods = copy.deepcopy(upstream_auth)

    return {
        "jsonrpc": JSONRPC_VERSION,
        "protocolVersion": protocol_version,
        "agentCapabilities": capabilities,
        "agentInfo": {
            "name": SHIM_NAME,
            "title": "jcode ACP adoption shim",
            "version": SHIM_VERSION,
        },
        "authMethods": auth_methods,
    }


def mode_result(degraded: bool = False) -> Dict[str, Any]:
    """Result shape for a local ``session/set_mode``."""
    return {
        "currentModeId": MODE_READ_ONLY,
        "modes": copy.deepcopy(SUPPORTED_MODES),
        "_meta": {
            "jcode": {
                "acpShim": {
                    "modeAppliedLocally": True,
                    "upstreamSupportsMode": not degraded,
                    "degraded": degraded,
                }
            }
        },
    }
