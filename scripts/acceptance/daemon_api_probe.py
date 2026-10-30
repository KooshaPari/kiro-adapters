#!/usr/bin/env python3
"""Probe the jcode daemon harness API (``jcode api --stdio``).

This is a runnable script, not a test. It drives the published protocol end to
end: hello, ping, session creation, one turn, and detach, over newline-delimited
JSON. It is intentionally model-agnostic on the probe prompt and reports the
exact failure it hits rather than retrying or papering over it.

Usage:
    python3 scripts/acceptance/daemon_api_probe.py [JCODE_BINARY] [PROMPT]

Defaults:
    JCODE_BINARY  ~/.local/bin/jcode
    PROMPT        "Reply with exactly: DAEMON_PROBE_OK"

Exit codes:
    0  turn completed (turn_done observed)
    1  protocol, spawn, or turn failure (details on stderr/stdout)
    2  bad arguments

The probe never starts a daemon by side effect beyond what ``jcode api --stdio``
itself does; it only speaks the protocol. It does not send credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

DEFAULT_BINARY = os.path.expanduser("~/.local/bin/jcode")
DEFAULT_PROMPT = "Reply with exactly: DAEMON_PROBE_OK"
DEFAULT_PROVIDER = os.environ.get("JCODE_PROBE_PROVIDER", "")
CONNECT_TIMEOUT = 30.0
TURN_TIMEOUT = 120.0
MAX_FRAME_BYTES = 16 * 1024 * 1024


def _fail(stage: str, detail: str, raw: str = "") -> int:
    print("STAGE: {}".format(stage), file=sys.stderr)
    print("DETAIL: {}".format(detail), file=sys.stderr)
    if raw:
        print("RAW: {}".format(raw[:500]), file=sys.stderr)
    return 1


def main(argv):
    binary = DEFAULT_BINARY
    prompt = DEFAULT_PROMPT
    if len(argv) > 1:
        binary = argv[1]
    if len(argv) > 2:
        prompt = " ".join(argv[2:])
    if len(argv) > 3:
        print("usage: daemon_api_probe.py [JCODE_BINARY] [PROMPT...]", file=sys.stderr)
        return 2

    if not os.path.exists(binary):
        return _fail("resolve", "binary not found: {}".format(binary))

    # Spawn the API bridge. NDJSON over stdin/stdout, one connection.
    argv = [binary, "api", "--stdio"]
    provider = os.environ.get("JCODE_PROBE_PROVIDER", DEFAULT_PROVIDER)
    if provider:
        argv += ["-p", provider]
    model = os.environ.get("JCODE_PROBE_MODEL", "")
    if model:
        argv += ["-m", model]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return _fail("spawn", str(exc))

    def send(obj):
        proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def read_one(timeout):
        import select
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([proc.stdout], [], [], 0.25)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                return None
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
                return ("__too_big__", line[:200])
            try:
                return json.loads(line)
            except ValueError:
                return ("__bad_json__", line[:200])
        return ("__timeout__", "")

    # 1. hello
    send({"v": 1, "id": 1, "req": "hello", "min_version": 1,
          "max_version": 1, "client": "daemon-api-probe/0.1.0"})
    frame = read_one(CONNECT_TIMEOUT)
    if not isinstance(frame, dict):
        proc.kill()
        return _fail("hello", "no reply", repr(frame))
    if frame.get("ev") != "hello_ok":
        proc.kill()
        return _fail("hello", "expected hello_ok, got {}".format(frame.get("ev")), repr(frame))
    server = frame.get("server")
    version = frame.get("version")
    caps = frame.get("capabilities") or []
    print("HELLO_OK server={} version={} caps={}".format(server, version, len(caps)))

    # 2. ping
    send({"v": 1, "id": 2, "req": "ping"})
    frame = read_one(CONNECT_TIMEOUT)
    if not isinstance(frame, dict) or frame.get("ev") != "pong":
        proc.kill()
        return _fail("ping", "expected pong, got {}".format((frame or {}).get("ev")), repr(frame))
    print("PING_OK")

    # 3. create_session
    send({"v": 1, "id": 3, "req": "create_session"})
    frame = read_one(CONNECT_TIMEOUT)
    if not isinstance(frame, dict) or frame.get("ev") not in ("attached", "session_forked", "session_status"):
        proc.kill()
        return _fail("create_session", "expected attached, got {}".format((frame or {}).get("ev")), repr(frame))
    ev = frame.get("ev")
    if ev == "session_status":
        session_id = frame.get("session_id")
        if not session_id:
            proc.kill()
            return _fail("create_session", "session_status without session_id", repr(frame))
        print("SESSION_CREATED id={} (via session_status)".format(session_id))
    else:
        session = frame.get("session") or {}
        session_id = session.get("session_id")
        if not session_id:
            proc.kill()
            return _fail("create_session", "no session_id in reply", repr(frame))
        print("SESSION_CREATED id={}".format(session_id))

    # 4. send_message (notify) + collect until turn_done
    send({"v": 1, "id": 4, "req": "send_message", "session_id": session_id,
          "content": prompt})
    # send_message is a notify: no reply frame. Read streaming events.
    text_parts = []
    events = []
    accepted = False
    done = False
    tool_calls = []
    token_usage = []
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline and not done:
        frame = read_one(1.0)
        if frame is None:
            continue
        if isinstance(frame, tuple):
            # timeout on this read; keep waiting
            continue
        if not isinstance(frame, dict):
            continue
        ev = frame.get("ev")
        owner = frame.get("session_id")
        if owner is not None and owner != session_id:
            continue
        if ev:
            events.append(ev)
        if ev == "message_accepted":
            accepted = True
        elif ev == "text_delta":
            t = frame.get("text")
            if isinstance(t, str):
                text_parts.append(t)
        elif ev == "tool_start":
            tool_calls.append({"name": frame.get("name"), "call_id": frame.get("call_id")})
        elif ev == "token_usage":
            token_usage.append({"input": frame.get("input"), "output": frame.get("output")})
        elif ev == "turn_done":
            done = True
        elif ev == "error":
            proc.kill()
            return _fail("turn", frame.get("message") or "remote error", repr(frame))

    answer = "".join(text_parts)
    print("TURN accepted={} done={} events={} text_chars={} tools={} tokens={}".format(
        accepted, done, len(events), len(answer), len(tool_calls), len(token_usage)))
    print("ANSWER: {}".format(answer))

    # 5. detach (best effort)
    try:
        send({"v": 1, "id": 5, "req": "detach_session", "session_id": session_id})
        read_one(5.0)
    except Exception:
        pass

    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    stderr = ""
    try:
        stderr = proc.stderr.read() or ""
    except Exception:
        pass
    if stderr.strip():
        print("STDERR: {}".format(stderr.strip()[:500]), file=sys.stderr)

    if not done:
        return _fail("turn", "turn ended without turn_done", "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
