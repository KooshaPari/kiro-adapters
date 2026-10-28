"""Read-only probes for Kiro Crew, split out to keep each module small.

Every probe here is one or two ``kirocrew`` subcommands plus local parsing. They
share a short-lived cache because a single kirocrew subcommand costs 12-100s on
a loaded host (it boots a Python app with an ML stack), and an MCP client will
happily ask for status, readiness and agents within the same second.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import result as R
from .redact import redact

PROBE_TTL_SECONDS = 45.0

_KAS_PINNED_RE = re.compile(r"pinned:\s*KAS\s+([0-9A-Za-z.\-]+)")
_KAS_INSTALLED_RE = re.compile(r"installed:\s*(.+)$", re.MULTILINE)
_PORT_RE = re.compile(r"https?://[^/:\s]+:(\d+)")


def cached(adapter: Any, key: str) -> Optional[R.AdapterResult]:
    """Return a still-fresh cached probe result, or None."""
    entry: Optional[Tuple[float, R.AdapterResult]] = getattr(
        adapter, "_probe_cache", {}
    ).get(key)
    if entry and (time.time() - entry[0]) < PROBE_TTL_SECONDS:
        return entry[1]
    return None


def store(adapter: Any, key: str, value: R.AdapterResult) -> R.AdapterResult:
    cache = getattr(adapter, "_probe_cache", None)
    if cache is None:
        cache = {}
        adapter._probe_cache = cache
    cache[key] = (time.time(), value)
    return value


def invalidate(adapter: Any) -> None:
    cache = getattr(adapter, "_probe_cache", None)
    if cache is not None:
        cache.clear()


def gateway_running(stdout: str) -> bool:
    return "is running" in stdout and "not running" not in stdout


def status(
    adapter: Any,
    include_kas: bool = False,
    timeout: float = 120.0,
    use_cache: bool = True,
) -> R.AdapterResult:
    """Gateway liveness and auth mode, plus agent count. KAS is opt-in."""
    key = "status:{}".format(include_kas)
    if use_cache:
        hit = cached(adapter, key)
        if hit is not None:
            return hit

    cmd = adapter._run(["status"], timeout)
    running = gateway_running(cmd.stdout)
    data: Dict[str, Any] = {
        "gateway_running": running,
        "token_auth": "token auth enabled" in cmd.stdout,
        "binary": adapter.binary,
        "resolve": adapter.resolve_info(),
    }

    kas_data: Dict[str, Any] = {}
    if include_kas:
        kas_data = kas_status(adapter, timeout=timeout).data
        data["kas"] = kas_data

    agents = list_agents(adapter)
    data["agents_available"] = len(agents.data.get("agents", []))
    if include_kas:
        data["ready"] = bool(running and kas_data.get("installed"))

    hints: List[str] = []
    if not running:
        hints.append("Start the gateway: kirocrew gateway")
    if include_kas and not kas_data.get("installed"):
        hints.append("Install the agent engine: kirocrew kas install")

    value = adapter._result_from_command(
        "status",
        cmd,
        detail="gateway {}".format("running" if running else "stopped"),
        data=data,
        hints=hints,
    )
    return store(adapter, key, value) if use_cache else value


def kas_status(adapter: Any, timeout: float = 120.0, use_cache: bool = True) -> R.AdapterResult:
    if use_cache:
        hit = cached(adapter, "kas")
        if hit is not None:
            return hit

    cmd = adapter._run(["kas", "status"], timeout)
    installed_line = ""
    match = _KAS_INSTALLED_RE.search(cmd.stdout)
    if match:
        installed_line = match.group(1).strip()
    # "KAS 0.66.33 is installed" vs "KAS 0.66.33 is not installed".
    installed = bool(installed_line) and "not installed" not in installed_line
    pinned_match = _KAS_PINNED_RE.search(cmd.stdout)
    data = {
        "installed": installed,
        "installed_line": installed_line,
        "pinned_version": pinned_match.group(1) if pinned_match else None,
        "engine_route": "kas_bundle" if "agent.kas_bundle" in cmd.stdout else None,
    }
    hints = [] if installed else ["Run: kirocrew kas install"]
    value = adapter._result_from_command(
        "kas_status",
        cmd,
        detail="KAS {}".format("installed" if installed else "not installed"),
        data=data,
        hints=hints,
    )
    return store(adapter, "kas", value) if use_cache else value


def list_agents(adapter: Any) -> R.AdapterResult:
    """Agent definitions from ~/.kiro/agents/*.json. No subprocess, no model call."""
    agents: List[Dict[str, Any]] = []
    errors: List[str] = []
    pattern = str(adapter.agents_dir / "*.json")
    for path in sorted(glob.glob(pattern)):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            errors.append("{}: {}".format(stem, exc))
            continue
        if not isinstance(payload, dict):
            errors.append("{}: top-level JSON is not an object".format(stem))
            continue
        mcp_servers = payload.get("mcpServers")
        agents.append(
            {
                "name": payload.get("name") or stem,
                "file": path,
                "model": payload.get("model"),
                "description": (payload.get("description") or "").strip(),
                "tools": payload.get("tools"),
                "mcp_servers": sorted(mcp_servers.keys())
                if isinstance(mcp_servers, dict)
                else mcp_servers,
            }
        )
    return R.AdapterResult.success(
        adapter.harness,
        "list_agents",
        "{} agent definition(s)".format(len(agents)),
        data={
            "agents_dir": str(adapter.agents_dir),
            "agents": agents,
            "errors": errors,
        },
    )


def dashboard_url(
    adapter: Any,
    ttl: Optional[str] = None,
    include_token: bool = False,
    timeout: float = 120.0,
) -> R.AdapterResult:
    """Dashboard URL. The token is a credential and is redacted by default."""
    args: List[str] = ["token"]
    if ttl:
        args.extend(["--ttl", ttl])
    cmd = adapter._run(args, timeout)
    raw_url = ""
    for line in cmd.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            raw_url = candidate
            break
    port_match = _PORT_RE.search(raw_url) if raw_url else None
    data = {
        "url": raw_url if include_token else redact(raw_url),
        "port": int(port_match.group(1)) if port_match else None,
        "token_included": bool(include_token),
    }
    return adapter._result_from_command(
        "dashboard_url",
        cmd,
        detail="dashboard URL {}".format("<redacted>" if raw_url else "unavailable"),
        data=data,
    )


def doctor(adapter: Any, timeout: float = 240.0) -> R.AdapterResult:
    cmd = adapter._run(["doctor"], timeout)
    failing = [
        line.strip() for line in cmd.stdout.splitlines() if line.strip().startswith("❌")
    ]
    return adapter._result_from_command(
        "doctor",
        cmd,
        detail="{} problem(s)".format(len(failing)) if failing else "healthy",
        data={"problems": failing},
    )


def spawn_list(adapter: Any, timeout: float = 120.0) -> R.AdapterResult:
    cmd = adapter._run(["spawn", "list"], timeout)
    return adapter._result_from_command(
        "spawn_list",
        cmd,
        detail="subagent list captured",
        data={"raw": cmd.stdout.strip()},
    )
