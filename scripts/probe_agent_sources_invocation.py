#!/usr/bin/env python3
"""Bounded, read-only probe: does a real Kiro Crew agent turn INVOKE the
read-only inventory MCP tool ``agent_sources``?

What it does
------------
1. Reads the local dashboard secret from ``~/.kiro/crew/.local_secret`` and
   talks to the already-running gateway on 127.0.0.1:5476 (it never starts,
   stops, or reconfigures anything).
2. Records the set of session transcripts (``~/.kiro/crew/sessions/*.jsonl``)
   and their sizes BEFORE the turn.
3. Submits ONE chat turn whose instructions make an ``agent_sources`` call the
   only way to answer.
4. Streams the SSE reply to a scratch file (bounded by --timeout).
5. Re-scans the session store for NEW or GROWN transcripts and reports any
   structured ``tool_call``/tool-name records it finds. It prints tool names and
   counts only: never tokens, never message text.

Exit codes
----------
0  an ``agent_sources`` tool record was found in a transcript
2  turn completed but no ``agent_sources`` record observed
3  the chat request itself failed (HTTP error / refusal / timeout)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5476"
CREW = pathlib.Path.home() / ".kiro" / "crew"
SESSIONS = CREW / "sessions"
SECRET_PATH = CREW / ".local_secret"

PROMPT = (
    "Use only the MCP tool agent_sources from the kiro-inventory server. "
    "Call agent_sources once with no filters and reply with the number of "
    "sources it returned and the tool name you called. Do not guess; if the "
    "tool is unavailable, say TOOL_UNAVAILABLE."
)


def secret() -> str:
    try:
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - environment dependent
        sys.exit(f"cannot read {SECRET_PATH}: {exc}")


def request(path: str, *, body: dict | None = None, timeout: float = 30.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method="GET" if body is None else "POST",
        headers={
            "X-Internal-Secret": secret(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def snapshot() -> dict[str, int]:
    out: dict[str, int] = {}
    if not SESSIONS.is_dir():
        return out
    for path in SESSIONS.glob("*.jsonl"):
        try:
            out[str(path)] = path.stat().st_size
        except OSError:
            continue
    return out


def scan(before: dict[str, int]) -> list[dict]:
    """Return structured tool-call facts from transcripts that changed."""
    hits: list[dict] = []
    after = snapshot()
    for path, size in after.items():
        if before.get(path, -1) == size:
            continue
        try:
            lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = 0
        if path in before:
            # Re-read the whole file: rows are line-oriented and a grown file's
            # earlier lines are unchanged, so scanning all is safe and simpler.
            start = 0
        for line in lines[start:]:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            blob = json.dumps(row)
            if "tool" not in blob.lower():
                continue
            name = row.get("tool_name") or row.get("name") or ""
            kind = row.get("kind") or row.get("type") or row.get("role") or ""
            server = row.get("mcp_server_name") or row.get("server") or ""
            hits.append(
                {
                    "file": os.path.basename(path),
                    "kind": kind,
                    "tool_name": name,
                    "server": server,
                    "mentions_agent_sources": "agent_sources" in blob,
                    "mentions_kiro_inventory": "kiro-inventory" in blob,
                }
            )
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--slot", default="")
    args = ap.parse_args()

    try:
        with request("/api/chat/slots", timeout=15) as resp:
            slots = json.loads(resp.read().decode() or "{}")
        rows = slots.get("slots") if isinstance(slots, dict) else slots
        names = []
        for row in rows or []:
            if isinstance(row, dict):
                names.append(row.get("name") or row.get("key") or row.get("slot"))
            else:
                names.append(str(row)[:16])
        print(f"preflight: /api/chat/slots -> {resp.status}, slot_count={len(names)}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        print(f"preflight failed: {exc!r}")
        return 3

    before = snapshot()
    started = time.time()
    body = {"message": PROMPT}
    if args.slot:
        body["slot"] = args.slot

    raw = pathlib.Path(os.environ.get("JCODE_SCRATCH_DIR", "/tmp")) / "kiro-chat-probe.sse"
    try:
        with request("/api/chat", body=body, timeout=args.timeout) as resp:
            status = resp.status
            with raw.open("wb") as fh:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    fh.write(chunk)
                    fh.flush()
        print(f"chat: HTTP {status}, streamed bytes={raw.stat().st_size}, elapsed={time.time()-started:.1f}s")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        print(f"chat: HTTP {exc.code} {exc.reason} body={detail}")
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"chat: transport failure {exc!r} after {time.time()-started:.1f}s")
        print(f"partial stream at {raw}")
        hits = scan(before)
        print(f"tool records in changed transcripts: {len(hits)}")
        return 3

    # Let persistence settle briefly; transcripts are flushed per row.
    time.sleep(1.0)
    hits = scan(before)
    print(f"tool records in changed transcripts: {len(hits)}")
    for hit in hits[:20]:
        print("  " + json.dumps(hit, sort_keys=True))

    invoked = any(h["mentions_agent_sources"] and h["mentions_kiro_inventory"] for h in hits) or any(
        "agent_sources" in (h["tool_name"] or "") for h in hits
    )
    if invoked:
        print("RESULT: AGENT_SOURCES_INVOKED_OBSERVED")
        return 0
    print("RESULT: AGENT_SOURCES_NOT_OBSERVED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
