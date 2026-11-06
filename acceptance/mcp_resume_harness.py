#!/usr/bin/env python3
"""Prove whether a RESUMED session registers MCP tools.

Why this exists
---------------
`jcode`'s `handle_subscribe` only registers MCP servers when its
`register_mcp_tools` flag is true, and the takeover/resume branch used to pass a hardcoded
`false`. A session reached via `session/load` therefore ran with no `mcp__*` tools, with no
error anywhere.

Nothing client-side could distinguish that from "the server is misconfigured", so this
harness tests the behaviour directly: start a daemon, SUBSCRIBE with a
`target_session_id` that exists only on disk (the resume path, with no live owner), and
watch whether a configured MCP server is actually spawned.

The observable is the inventory server's own attestation: when MCP registers, the `kiro`
server in ~/.jcode/mcp.json is launched, and it appends a `start` record to
`KIRO_INVENTORY_AUDIT_LOG`.

Safety: private socket per run inside a scratch dir, HOME is NOT redirected (the real
session store is only read, which the resume path needs), and only the daemon this script
started is ever signalled.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

SCRATCH = pathlib.Path.home() / ".jcode" / "scratch" / "verify-mcp-resume"
AUDIT = SCRATCH / "audit.jsonl"
# An old session id: guaranteed to exist on disk and to have no live owner.
SESSION_ID = "session_llama_1792100519215_660106b81db1bccc"


def wait_for_socket(path: pathlib.Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1.0)
                    probe.connect(str(path))
                return True
            except OSError:
                pass
        time.sleep(0.25)
    return False


def run_case(label: str, binary: pathlib.Path) -> dict:
    if not binary.is_file():
        return {"label": label, "error": "binary missing: {}".format(binary)}
    sock = SCRATCH / "{}.sock".format(label)
    for stale in (sock, AUDIT):
        stale.unlink(missing_ok=True)

    env = dict(os.environ)
    env["KIRO_INVENTORY_AUDIT_LOG"] = str(AUDIT)
    env["JCODE_NO_TELEMETRY"] = "1"

    proc = subprocess.Popen(
        [str(binary), "serve", "--socket", str(sock)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    result: dict = {"label": label, "binary": str(binary)}
    try:
        if not wait_for_socket(sock):
            result["error"] = "daemon socket never appeared"
            return result

        # The resume path: target_session_id names a session that exists only on disk.
        request = {
            "type": "subscribe",
            "id": 1,
            "working_dir": str(pathlib.Path.home()),
            "target_session_id": SESSION_ID,
            "client_instance_id": "mcp-resume-harness",
            "client_has_local_history": False,
            "allow_session_takeover": False,
            "crash_on_disconnect": False,
            "continue_on_disconnect": False,
            "terminal_env": [],
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10.0)
            client.connect(str(sock))
            client.sendall((json.dumps(request) + "\n").encode())

        # Registration is asynchronous; give the MCP child time to be launched.
        deadline = time.monotonic() + 12.0
        records: list = []
        while time.monotonic() < deadline:
            if AUDIT.exists():
                records = [
                    json.loads(line)
                    for line in AUDIT.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if records:
                    break
            time.sleep(0.5)

        result["start_records"] = records
        result["mcp_spawned"] = any(r.get("event") == "start" for r in records)
        return result
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        sock.unlink(missing_ok=True)
        try:
            err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        except Exception:  # noqa: BLE001
            err = ""
        result["stderr_tail"] = err.strip().splitlines()[-3:] if err.strip() else []


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pinned = pathlib.Path.home() / ".jcode" / "builds" / "versions" / "0.99.1-kcode-mode" / "jcode"
    patched = pathlib.Path(
        "/Users/kooshapari/CodeProjects/Phenotype/repos/jcode-acp-native/target/debug/jcode"
    )

    cases = []
    for label, binary in (("pinned", pinned), ("patched", patched)):
        cases.append(run_case(label, binary))

    print(json.dumps({"session": SESSION_ID, "cases": cases}, indent=2))

    by = {c["label"]: c for c in cases}
    old = by.get("pinned", {}).get("mcp_spawned")
    new = by.get("patched", {}).get("mcp_spawned")
    print("\n=== verdict ===")
    print("pinned  MCP spawned on resume:", old)
    print("patched MCP spawned on resume:", new)
    if new is True and old is False:
        print("FIX CONFIRMED: resume registers MCP only with the patch")
        return 0
    if new is True and old is True:
        print("INCONCLUSIVE: both spawned, so the resume path was not the differentiator here")
        return 1
    print("NOT CONFIRMED: patched build did not spawn MCP on resume")
    return 2


if __name__ == "__main__":
    sys.exit(main())
