"""ACP adoption shim: let another client adopt an operator's jcode session.

What it is
----------
An ACP server on stdio. KiroCrew (or any ACP client) connects, calls
``initialize``, then ``session/load`` with a session id the operator already
has. The shim forwards the load over its own ACP link to the operator's build
only after two read-only preconditions hold.

What it refuses to do
---------------------
* Start a daemon. ``session/load`` upstream calls ``connect_daemon`` which
  calls ``ensure_daemon`` and spawns one when none is running
  (src/cli/acp.rs:851-867). If no daemon socket is already live, the shim
  fails closed with ``SHIM_PRECONDITION_FAILED`` and
  ``data.willSpawnDaemon = false``.
* Create a session. ``session/new`` is blocked at the transport.
* Close a session. Upstream ``session/close`` cancels a running prompt and
  stops the event pump (src/cli/acp.rs:653-677). Detach is dropping the
  connection.

Public API
----------
``ShimConfig``, ``AdoptionShim``, ``run``
    Construction, dispatch and the stdio loop.
``gate.assess``, ``gate.probe_daemon``, ``gate.list_sessions``
    The fail-closed adoption gate and its read-only probes.
``capabilities.build_initialize_result``, ``capabilities.SUPPORTED_MODES``
    Capability advertisement (``loadSession: true`` plus ``read-only``).
``InProcessUpstream``, ``StdioUpstream``, ``Upstream``
    Transport seam. Both enforce the forbidden-method list.
``RpcError``, ``BlockedUpstreamCall``, error codes
    Error surface, safe to send to a client.
"""

from __future__ import annotations

from .capabilities import (
    ACP_PROTOCOL_VERSION,
    FALLBACK_CAPABILITIES,
    MODE_READ_ONLY,
    SHIM_NAME,
    SHIM_VERSION,
    SUPPORTED_MODES,
    build_initialize_result,
    mode_result,
)
from .gate import (
    AdoptionPlan,
    DaemonState,
    assess,
    find_session_file,
    list_sessions,
    probe_daemon,
)
from .protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SHIM_PRECONDITION_FAILED,
    SHIM_UPSTREAM_UNAVAILABLE,
    BlockedUpstreamCall,
    RpcError,
    read_message,
    write_message,
)
from .server import (
    AdoptionShim,
    ShimConfig,
    require_session_id,
)
from .runtime import build, in_process_upstream, run, stdio_upstream_factory
from .upstream import FORBIDDEN_METHODS, InProcessUpstream, StdioUpstream, Upstream

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "AdoptionPlan",
    "AdoptionShim",
    "BlockedUpstreamCall",
    "DaemonState",
    "FALLBACK_CAPABILITIES",
    "FORBIDDEN_METHODS",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "InProcessUpstream",
    "METHOD_NOT_FOUND",
    "MODE_READ_ONLY",
    "PARSE_ERROR",
    "RpcError",
    "SHIM_NAME",
    "SHIM_PRECONDITION_FAILED",
    "SHIM_UPSTREAM_UNAVAILABLE",
    "SHIM_VERSION",
    "SUPPORTED_MODES",
    "ShimConfig",
    "StdioUpstream",
    "Upstream",
    "assess",
    "build_initialize_result",
    "build",
    "find_session_file",
    "in_process_upstream",
    "list_sessions",
    "mode_result",
    "probe_daemon",
    "read_message",
    "require_session_id",
    "run",
    "stdio_upstream_factory",
    "write_message",
]
