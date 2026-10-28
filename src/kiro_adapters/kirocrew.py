"""Kiro Crew adapter — drive the ``kirocrew`` personal agent from jcode.

Surfaces verified against ``kirocrew 0.11.0-nightly.20261021t201456``:

    kirocrew status | doctor | token | logs
    kirocrew chat  -m MSG [--agent A] [--model M]
    kirocrew run   SPEC [--name N] [--no-test] [--timeout S] [--fresh]
    kirocrew spawn run PROMPT [--async] [--crew C] [--agent A]
    kirocrew spawn list
    kirocrew kas status
    kirocrew agent list

Readiness matters. ``chat``/``spawn``/``run`` need the KAS engine bundle
(``kirocrew kas install``) and, for ``spawn``, a running gateway. The adapter
checks before it spends a model call and returns ``BLOCKED`` with the exact
remediation instead of a confusing stack trace.

Read-only probes live in :mod:`kiro_adapters.kirocrew_probes`; this module owns
process resolution, readiness and the actions that cost a model call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import kirocrew_probes as probes
from . import result as R
from .redact import redact
from .runner import CommandResult, resolve_binary, run

HARNESS = "kirocrew"
BINARY_CANDIDATES = ("kirocrew",)
ENV_VAR = "KIROCREW_BIN"

DEFAULT_ASK_TIMEOUT = 900.0
DEFAULT_SPAWN_TIMEOUT = 1800.0
DEFAULT_SPEC_TIMEOUT = 3600.0
# kirocrew boot is slow (it imports an ML stack): a single subcommand measured
# 12-100s on a loaded host. Short probes get a generous floor so a busy host
# reports a real answer instead of a false TIMEOUT.
DEFAULT_SHORT_TIMEOUT = 120.0
DEFAULT_DOCTOR_TIMEOUT = 240.0


class KiroCrewAdapter:
    """Thin, structured wrapper over the ``kirocrew`` CLI."""

    harness = HARNESS

    def __init__(
        self,
        binary: Optional[str] = None,
        agents_dir: Optional[str] = None,
        env_var: str = ENV_VAR,
    ) -> None:
        self._binary = binary
        self.agents_dir = Path(agents_dir or os.path.expanduser("~/.kiro/agents"))
        self.env_var = env_var
        self._resolved: Optional[str] = None
        self._tried: List[str] = []
        self._probe_cache: Dict[str, Any] = {}

    # -- resolution -------------------------------------------------------
    @property
    def binary(self) -> Optional[str]:
        if self._binary:
            return self._binary
        if self._resolved is None:
            self._resolved, self._tried = resolve_binary(BINARY_CANDIDATES, self.env_var)
        return self._resolved

    def resolve_info(self) -> Dict[str, Any]:
        return {
            "binary": self.binary,
            "found": self.binary is not None,
            "tried": self._tried if not self._binary else ["explicit:{}".format(self._binary)],
        }

    def _missing(self, action: str) -> R.AdapterResult:
        return R.AdapterResult.failure(
            HARNESS,
            action,
            R.NOT_INSTALLED,
            "kirocrew CLI not found",
            data=self.resolve_info(),
            hints=[
                "Install Kiro Crew (the macOS app bundle ships the launcher)",
                "Ensure `kirocrew --version` works in this shell",
                "Or pin the launcher: export {}=/path/to/kirocrew".format(self.env_var),
            ],
        )

    # -- plumbing ---------------------------------------------------------
    def _run(
        self,
        args: Sequence[str],
        timeout: float = DEFAULT_SHORT_TIMEOUT,
        cwd: Optional[str] = None,
    ) -> CommandResult:
        binary = self.binary
        if binary is None:
            raise FileNotFoundError("kirocrew binary not resolved")
        return run([binary] + [str(a) for a in args], timeout=timeout, cwd=cwd)

    @staticmethod
    def _result_from_command(
        action: str,
        cmd: CommandResult,
        detail: str = "",
        data: Optional[Dict[str, Any]] = None,
        hints: Optional[List[str]] = None,
    ) -> R.AdapterResult:
        payload = dict(data or {})
        payload.setdefault("argv", cmd.argv)
        payload.setdefault("exit_code", cmd.code)
        stdout = redact(cmd.stdout)
        stderr = redact(cmd.stderr)
        if cmd.spawn_error:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                R.NOT_INSTALLED,
                "failed to launch {}: {}".format(cmd.argv[0], cmd.spawn_error),
                data=payload,
                stdout=stdout,
                stderr=stderr,
                duration_ms=cmd.duration_ms,
                hints=hints,
            )
        if cmd.timed_out:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                R.TIMEOUT,
                detail or "timed out after {}s".format(int(cmd.duration_ms / 1000)),
                data=payload,
                stdout=stdout,
                stderr=stderr,
                duration_ms=cmd.duration_ms,
                hints=hints,
            )
        if cmd.code != 0:
            return R.AdapterResult.failure(
                HARNESS,
                action,
                R.ERROR,
                detail or "kirocrew exited {}".format(cmd.code),
                data=payload,
                stdout=stdout,
                stderr=stderr,
                duration_ms=cmd.duration_ms,
                hints=hints,
            )
        return R.AdapterResult.success(
            HARNESS,
            action,
            detail,
            data=payload,
            stdout=stdout,
            stderr=stderr,
            duration_ms=cmd.duration_ms,
            hints=hints,
        )

    # -- read-only probes (delegated, cached) -----------------------------
    def status(
        self,
        timeout: float = DEFAULT_SHORT_TIMEOUT,
        include_kas: bool = False,
        use_cache: bool = True,
    ) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("status")
        return probes.status(
            self, include_kas=include_kas, timeout=timeout, use_cache=use_cache
        )

    def kas_status(
        self, timeout: float = DEFAULT_SHORT_TIMEOUT, use_cache: bool = True
    ) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("kas_status")
        return probes.kas_status(self, timeout=timeout, use_cache=use_cache)

    def list_agents(self) -> R.AdapterResult:
        return probes.list_agents(self)

    agents = list_agents

    def dashboard_url(
        self,
        ttl: Optional[str] = None,
        include_token: bool = False,
        timeout: float = DEFAULT_SHORT_TIMEOUT,
    ) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("dashboard_url")
        return probes.dashboard_url(
            self, ttl=ttl, include_token=include_token, timeout=timeout
        )

    def doctor(self, timeout: float = DEFAULT_DOCTOR_TIMEOUT) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("doctor")
        return probes.doctor(self, timeout=timeout)

    def spawn_list(self, timeout: float = DEFAULT_SHORT_TIMEOUT) -> R.AdapterResult:
        if self.binary is None:
            return self._missing("spawn_list")
        return probes.spawn_list(self, timeout=timeout)

    def refresh(self) -> None:
        """Drop cached probe results (call after kas install / gateway restart)."""
        probes.invalidate(self)

    # -- readiness gate ---------------------------------------------------
    def readiness(
        self, need_gateway: bool = True
    ) -> "tuple[bool, List[str], Dict[str, Any]]":
        hints: List[str] = []
        state: Dict[str, Any] = {}
        if self.binary is None:
            return False, ["kirocrew not installed (see `kirocrew --version`)"], state

        kas = self.kas_status()
        state["kas"] = kas.data
        if not kas.data.get("installed"):
            hints.append("Agent engine missing: run `kirocrew kas install`")

        if need_gateway:
            status = self.status()
            running = bool(status.data.get("gateway_running"))
            state["gateway_running"] = running
            if not running:
                hints.append("Gateway stopped: run `kirocrew gateway`")

        return (not hints), hints, state

    # -- actions that cost a model call ------------------------------------
    def ask(
        self,
        message: str,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_ASK_TIMEOUT,
        cwd: Optional[str] = None,
        require_ready: bool = True,
    ) -> R.AdapterResult:
        """One-shot chat turn: ``kirocrew chat -m MSG``."""
        if self.binary is None:
            return self._missing("ask")
        if not (message or "").strip():
            return R.AdapterResult.failure(
                HARNESS, "ask", R.ERROR, "message must not be empty"
            )
        state: Dict[str, Any] = {}
        if require_ready:
            ready, hints, state = self.readiness(need_gateway=False)
            if not ready:
                return R.AdapterResult.failure(
                    HARNESS,
                    "ask",
                    R.BLOCKED,
                    "kirocrew is not ready to run the agent",
                    data={"readiness": state},
                    hints=hints,
                )

        args: List[str] = ["chat"]
        if agent:
            args.extend(["--agent", agent])
        if model:
            args.extend(["--model", model])
        args.extend(["-m", message])

        cmd = self._run(args, timeout, cwd=cwd)
        answer = _answer_text(cmd.stdout)
        return self._result_from_command(
            "ask",
            cmd,
            detail="{} chars".format(len(answer)) if answer else "no text returned",
            data={"answer": answer, "agent": agent, "model": model, "readiness": state},
        )

    def spawn(
        self,
        prompt: str,
        agent: Optional[str] = None,
        crew: Optional[str] = None,
        background: bool = False,
        timeout: float = DEFAULT_SPAWN_TIMEOUT,
        cwd: Optional[str] = None,
        require_ready: bool = True,
    ) -> R.AdapterResult:
        """Dispatch a subagent: ``kirocrew spawn run PROMPT``."""
        if self.binary is None:
            return self._missing("spawn")
        if not (prompt or "").strip():
            return R.AdapterResult.failure(
                HARNESS, "spawn", R.ERROR, "prompt must not be empty"
            )
        state: Dict[str, Any] = {}
        if require_ready:
            ready, hints, state = self.readiness(need_gateway=True)
            if not ready:
                return R.AdapterResult.failure(
                    HARNESS,
                    "spawn",
                    R.BLOCKED,
                    "kirocrew cannot accept a subagent right now",
                    data={"readiness": state},
                    hints=hints,
                )

        args: List[str] = ["spawn", "run"]
        if crew:
            args.extend(["--crew", crew])
        if agent:
            args.extend(["--agent", agent])
        if background:
            args.append("--async")
        args.append(prompt)

        cmd = self._run(args, timeout, cwd=cwd)
        return self._result_from_command(
            "spawn",
            cmd,
            detail="dispatched" + (" (async)" if background else ""),
            data={
                "prompt": prompt,
                "agent": agent,
                "crew": crew,
                "background": background,
            },
        )

    def run_spec(
        self,
        spec_path: str,
        name: Optional[str] = None,
        timeout: float = DEFAULT_SPEC_TIMEOUT,
        no_test: bool = False,
        fresh: bool = False,
        cwd: Optional[str] = None,
        require_ready: bool = True,
    ) -> R.AdapterResult:
        """Run an autonomous task from a spec file: ``kirocrew run SPEC``."""
        if self.binary is None:
            return self._missing("run_spec")
        spec = os.path.abspath(os.path.expanduser(spec_path))
        if not os.path.isfile(spec):
            return R.AdapterResult.failure(
                HARNESS,
                "run_spec",
                R.ERROR,
                "spec file not found: {}".format(spec),
                hints=["Pass an existing markdown/text spec path"],
            )
        state: Dict[str, Any] = {}
        if require_ready:
            ready, hints, state = self.readiness(need_gateway=False)
            if not ready:
                return R.AdapterResult.failure(
                    HARNESS,
                    "run_spec",
                    R.BLOCKED,
                    "kirocrew is not ready to run a spec",
                    data={"readiness": state},
                    hints=hints,
                )

        args: List[str] = ["run", spec]
        if name:
            args.extend(["--name", name])
        if no_test:
            args.append("--no-test")
        if fresh:
            args.append("--fresh")
        args.extend(["--timeout", str(int(timeout))])

        cmd = self._run(args, timeout + 30.0, cwd=cwd or os.path.dirname(spec))
        return self._result_from_command(
            "run_spec",
            cmd,
            detail="spec run finished",
            data={
                "spec": spec,
                "name": name or os.path.splitext(os.path.basename(spec))[0],
            },
        )


def _answer_text(stdout: str) -> str:
    """Strip leading blank lines from chat output, keeping the assistant text."""
    lines = list((stdout or "").splitlines())
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()
