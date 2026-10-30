"""The adoption shim: an ACP server that adopts, but never spawns, a session.

Contract, in order of importance:

1. Answer ACP on newline-delimited JSON (stdio by default).
2. ``session/load`` / ``session/resume`` only succeed for a session that
   already exists on disk AND a daemon socket that already accepts a connect.
   Otherwise fail closed with a clear error. Never call ``ensure_daemon``
   indirectly by forwarding a load without that evidence.
3. Never send ``session/close`` upstream, and never send ``session/new``.
4. Advertise ``loadSession: true`` and a ``read-only`` mode.
5. Never log or echo a transcript body or a credential.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TextIO, Tuple

from . import capabilities, gate
from .capabilities import MODE_READ_ONLY
from .protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    SHIM_PRECONDITION_FAILED,
    SHIM_UPSTREAM_UNAVAILABLE,
    BlockedUpstreamCall,
    RpcError,
    error_payload,
    is_notification,
    read_message,
    result_payload,
    write_message,
)
from .upstream import Upstream

MODE_CONFIG_IDS = ("mode", "session_mode", "sessionMode", "modes")
WRITE_CONFIG_IDS = ("writes", "allow_writes", "write", "approval", "auto_approve")
SHIM_CLOSE_REASON = "the shim never closes an operator session; drop the connection"


@dataclass
class ShimConfig:
    """Everything injectable, so nothing touches a real install in tests."""

    upstream_argv: Tuple[str, ...] = ("kcode", "acp")
    session_dirs: Tuple[str, ...] = ()
    socket_paths: Tuple[str, ...] = ()
    mode: str = MODE_READ_ONLY
    forward_mode: bool = False
    client_name: str = "kirocrew-acp-adoption-shim"
    upstream_timeout: float = 60.0


class AdoptionShim:
    """ACP request handler plus stdio loop."""

    def __init__(
        self,
        config: Optional[ShimConfig] = None,
        upstream_factory: Optional[Callable[[ShimConfig], Upstream]] = None,
        logger: Optional[Callable[[str], None]] = None,
        probe: Optional[Callable[[Any], gate.DaemonState]] = None,
    ) -> None:
        self.config = config or ShimConfig()
        self._factory = upstream_factory
        self._logger = logger
        self._probe = probe
        self._upstream: Optional[Upstream] = None
        self._upstream_state = "not_used"
        self._adopted: Optional[str] = None
        self._mode = self.config.mode

    # -- collaborators -----------------------------------------------------
    def _session_dirs(self) -> Tuple[str, ...]:
        if self.config.session_dirs:
            return tuple(str(item) for item in self.config.session_dirs)
        return gate.default_session_dirs()

    def _socket_paths(self) -> Tuple[str, ...]:
        if self.config.socket_paths:
            return tuple(str(item) for item in self.config.socket_paths)
        return gate.default_socket_paths()

    def _ensure_upstream(self) -> Upstream:
        if self._upstream is not None:
            return self._upstream
        if self._factory is None:
            self._upstream_state = "not_configured"
            raise RpcError(
                SHIM_UPSTREAM_UNAVAILABLE,
                "no upstream ACP process is configured for this shim",
            )
        self._upstream = self._factory(self.config)
        self._upstream_state = "connected"
        return self._upstream

    def _log(self, text: str, error: Optional[RpcError] = None) -> None:
        if self._logger is None:
            return
        suffix = " code={0}".format(error.code) if error is not None else ""
        self._logger(text + suffix)

    # -- dispatch ----------------------------------------------------------
    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a response payload, or ``None`` for a notification."""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if not isinstance(method, str) or not method:
            if is_notification(message):
                return None
            return error_payload(request_id, RpcError(INVALID_REQUEST, "Missing method"))
        if is_notification(message):
            # The shim's contract needs no client notifications. Ignore them
            # rather than forwarding anything upstream.
            return None
        handler = _HANDLERS.get(method)
        if handler is None:
            self._log("method " + method)
            return error_payload(
                request_id,
                RpcError(METHOD_NOT_FOUND, "Unsupported ACP method: {0}".format(method)),
            )
        try:
            result = handler(self, params)
        except RpcError as error:
            self._log("method " + method, error)
            return error_payload(request_id, error)
        except BlockedUpstreamCall as error:
            self._log("blocked " + error.method)
            return error_payload(
                request_id,
                RpcError(
                    SHIM_UPSTREAM_UNAVAILABLE,
                    "the shim refuses to send {0} upstream".format(error.method),
                    {"blockedMethod": error.method, "willSpawnDaemon": False},
                ),
            )
        except Exception:
            # A shim must not break the stream, and must not echo internals.
            self._log("internal " + method)
            return error_payload(
                request_id,
                RpcError(INTERNAL_ERROR, "internal shim error in " + method),
            )
        return result_payload(request_id, result)

    # -- handlers ----------------------------------------------------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        upstream_result: Optional[Dict[str, Any]] = None
        state = "unavailable"
        init_params = {
            "protocolVersion": params.get("protocolVersion"),
            "clientCapabilities": params.get("clientCapabilities") or {},
            "clientInfo": params.get("clientInfo") or {"name": self.config.client_name},
        }
        try:
            upstream = self._ensure_upstream()
            upstream_result = upstream.request("initialize", init_params)
            state = "connected"
        except (RpcError, OSError) as error:
            self._upstream_state = "unavailable"
            self._log("initialize upstream", error if isinstance(error, RpcError) else None)
        merged = capabilities.build_initialize_result(
            upstream_result,
            mode=self._mode,
            upstream_state=state,
        )
        merged.pop("jsonrpc", None)
        return merged

    def _session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sessions = gate.list_sessions(self._session_dirs())
        cwd = params.get("cwd")
        if isinstance(cwd, str) and cwd:
            sessions = [
                item
                for item in sessions
                if not item.get("cwd") or item.get("cwd") == cwd
            ]
        return {"sessions": sessions}

    def _adopt(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = require_session_id(params)
        plan = gate.assess(
            session_id,
            self._session_dirs(),
            self._socket_paths(),
            self._probe,
        )
        upstream = self._ensure_upstream()
        forward = dict(params)
        forward["sessionId"] = session_id
        if plan.cwd and not forward.get("cwd"):
            forward["cwd"] = plan.cwd
        forward.setdefault("mcpServers", [])
        result = upstream.request(method, forward)
        self._adopted = session_id
        return result if isinstance(result, dict) else {}

    def _session_load(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._adopt("session/load", params)

    def _session_resume(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._adopt("session/resume", params)

    def _require_adopted(self, params: Dict[str, Any]) -> str:
        session_id = require_session_id(params)
        if self._adopted != session_id:
            raise RpcError(
                SHIM_PRECONDITION_FAILED,
                "session is not adopted by this shim; call session/load first",
                {"precondition": "session_not_loaded", "sessionId": session_id},
            )
        return session_id

    def _session_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._require_adopted(params)
        upstream = self._ensure_upstream()
        return upstream.request("session/prompt", dict(params))

    def _session_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._adopted is None:
            return {}
        upstream = self._ensure_upstream()
        return upstream.request("session/cancel", dict(params))

    def _session_set_mode(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mode_id = params.get("modeId") or params.get("mode")
        if not isinstance(mode_id, str) or not mode_id.strip():
            raise RpcError(INVALID_PARAMS, "session/set_mode requires a non-empty modeId")
        if mode_id.strip() != MODE_READ_ONLY:
            raise RpcError(
                INVALID_PARAMS,
                "this shim supports only the read-only mode",
                {"supportedModes": [MODE_READ_ONLY]},
            )
        self._mode = MODE_READ_ONLY
        if not self.config.forward_mode:
            return capabilities.mode_result()
        try:
            upstream = self._ensure_upstream()
            upstream.request("session/set_mode", dict(params))
        except RpcError as error:
            if error.code != METHOD_NOT_FOUND:
                raise
            # The fork does not implement session/set_mode; it answers -32601.
            # Degrade to the local contract instead of failing the client.
            self._log("set_mode upstream unsupported", error)
            return capabilities.mode_result(degraded=True)
        return capabilities.mode_result()

    def _session_set_config_option(self, params: Dict[str, Any]) -> Dict[str, Any]:
        config_id = params.get("configId") or params.get("optionId") or params.get("id")
        value = params.get("value")
        if isinstance(config_id, str) and config_id in MODE_CONFIG_IDS:
            mode_id = value.get("modeId") if isinstance(value, dict) else value
            return self._session_set_mode({"modeId": mode_id})
        if isinstance(config_id, str) and config_id in WRITE_CONFIG_IDS:
            raise RpcError(
                INVALID_PARAMS,
                "the shim is read-only and refuses write-enabling options",
                {"configId": config_id},
            )
        upstream = self._ensure_upstream()
        return upstream.request("session/set_config_option", dict(params))

    def _session_set_compat(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        upstream = self._ensure_upstream()
        try:
            return upstream.request(method, dict(params))
        except RpcError as error:
            if error.code != METHOD_NOT_FOUND:
                raise
            return {"_meta": {"jcode": {"acpShim": {"upstreamSupported": False}}}}

    def _session_set_model(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._session_set_compat("session/set_model", params)

    def _session_set_reasoning_effort(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._session_set_compat("session/set_reasoning_effort", params)

    def _session_close(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Deliberate local no-op. Forwarding this cancels a running prompt and
        # stops the event pump (src/cli/acp.rs:653-677).
        self._log("session/close refused")
        return {"closed": False, "detached": True, "reason": SHIM_CLOSE_REASON}

    # -- lifecycle ---------------------------------------------------------
    def serve(
        self,
        reader: Optional[TextIO] = None,
        writer: Optional[TextIO] = None,
        max_messages: Optional[int] = None,
    ) -> int:
        """Run the stdio loop until EOF. Returns the number of messages read."""
        source = reader if reader is not None else sys.stdin
        sink = writer if writer is not None else sys.stdout
        served = 0
        while max_messages is None or served < max_messages:
            try:
                message = read_message(source)
            except RpcError as error:
                write_message(sink, error_payload(None, error))
                continue
            if message is None:
                break
            payload = self.handle_message(message)
            if payload is not None:
                write_message(sink, payload)
            served += 1
        self.shutdown()
        return served

    def shutdown(self) -> None:
        """Drop the upstream link. Never sends ``session/close``."""
        if self._upstream is not None:
            try:
                self._upstream.close()
            finally:
                self._upstream = None


def require_session_id(params: Dict[str, Any]) -> str:
    session_id = params.get("sessionId") or params.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise RpcError(INVALID_PARAMS, "sessionId is required")
    return session_id.strip()


_HANDLERS: Dict[str, Callable[[AdoptionShim, Dict[str, Any]], Any]] = {
    "initialize": AdoptionShim._initialize,
    "session/list": AdoptionShim._session_list,
    "session/load": AdoptionShim._session_load,
    "session/resume": AdoptionShim._session_resume,
    "session/prompt": AdoptionShim._session_prompt,
    "session/cancel": AdoptionShim._session_cancel,
    "session/close": AdoptionShim._session_close,
    "session/set_mode": AdoptionShim._session_set_mode,
    "session/set_config_option": AdoptionShim._session_set_config_option,
    "session/set_model": AdoptionShim._session_set_model,
    "session/set_reasoning_effort": AdoptionShim._session_set_reasoning_effort,
}
