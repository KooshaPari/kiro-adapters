#!/usr/bin/env python3
"""Prove that a jcode build can serve as Kiro Crew's ACP backend.

Kiro Crew's `codex` backend slot only accepts a harness that satisfies its
`SESSION_CONFIG` routing: the agent must advertise a `configOptions` entry
whose id is `mode` and whose values include `read-only`, and writing
`mode=read-only` must succeed. If either half fails, Kiro Crew refuses the
session rather than run an agent it cannot gate.

This script performs exactly those two checks against a jcode binary, so the
gate can be verified without running Kiro Crew.

Usage:
    python3 scripts/acceptance/kiro_backend_gate.py /path/to/jcode

Exit status is 0 when the gate passes and 1 when it does not.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": 1,
        "clientInfo": {"name": "kiro-crew-acceptance", "version": "1"},
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
    },
}

#: How long to wait for the daemon to come up and answer. The adapter starts
#: its own daemon when none is running, which is not instant.
STARTUP_TIMEOUT = 90.0
REPLY_TIMEOUT = 20.0


class Probe:
    """One ACP conversation with a jcode child process."""

    def __init__(self, binary: str) -> None:
        self.proc = subprocess.Popen(
            [binary, "--no-selfdev", "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.frames: List[Dict[str, Any]] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.frames.append(json.loads(line))
            except ValueError:
                continue

    def send(self, request: Dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()

    def reply_for(self, request_id: int, timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in list(self.frames):
                if frame.get("id") == request_id:
                    return frame
            time.sleep(0.2)
        return None

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        self.proc.terminate()


def check(binary: str) -> int:
    if not binary or not __import__("os").access(binary, __import__("os").X_OK):
        print("FAIL: {} is not an executable file".format(binary), file=sys.stderr)
        return 1

    probe = Probe(binary)
    try:
        probe.send(INITIALIZE)
        init = probe.reply_for(1, REPLY_TIMEOUT)
        if init is None or "result" not in init:
            print("FAIL: no initialize reply", file=sys.stderr)
            return 1

        agent = init["result"].get("agentInfo", {})
        print("agent reports: {} {}".format(agent.get("name"), agent.get("version")))

        probe.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/tmp", "mcpServers": []},
            }
        )
        created = probe.reply_for(2, STARTUP_TIMEOUT)
        if created is None or "result" not in created:
            print("FAIL: no session/new reply", file=sys.stderr)
            return 1

        result = created["result"]
        session_id = result.get("sessionId")
        if not session_id:
            print("FAIL: session/new returned no sessionId", file=sys.stderr)
            return 1

        options = result.get("configOptions")
        if not isinstance(options, list):
            print("FAIL: session/new advertised no configOptions", file=sys.stderr)
            return 1

        mode = next(
            (item for item in options if isinstance(item, dict) and item.get("id") == "mode"),
            None,
        )
        if mode is None:
            print("FAIL: no config option with id 'mode'", file=sys.stderr)
            return 1

        values = [
            entry.get("value")
            for entry in mode.get("options", [])
            if isinstance(entry, dict)
        ]
        if "read-only" not in values:
            print(
                "FAIL: the mode option does not advertise 'read-only': {}".format(values),
                file=sys.stderr,
            )
            return 1
        print("gate half 1: mode advertises read-only -- PASS")

        probe.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": session_id,
                    "configId": "mode",
                    "value": "read-only",
                },
            }
        )
        applied = probe.reply_for(3, REPLY_TIMEOUT)
        if applied is None:
            print("FAIL: no reply to session/set_config_option", file=sys.stderr)
            return 1
        if "error" in applied:
            print(
                "FAIL: the agent rejected mode=read-only: {}".format(applied["error"]),
                file=sys.stderr,
            )
            return 1
        print("gate half 2: mode=read-only was accepted -- PASS")
        print("\nKiro Crew's SESSION_CONFIG gate would accept this agent.")
        return 0
    finally:
        probe.close()


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
