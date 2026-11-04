"""Synthetic, isolated acceptance probe for the shim's ADOPTION path.

Safety contract
---------------
This probe is deliberately synthetic. It does NOT start a jcode/KCode process,
does NOT connect to any socket it did not create, and does NOT read or write a
real ``~/.jcode`` / ``~/.kcode`` session store. It requires:

  * an EXPLICIT session id on the command line (never "the newest session");
  * a fresh isolated root under the system temp directory, containing both the
    session store and the daemon socket;
  * that the exact id resolves inside that isolated store.

Everything the shim does is checked against an audit hook: every socket
connect must target the probe's own socket, every opened file must live under
the probe root, and no subprocess may be started. A probe that inherits
``JCODE_ACP_SESSION_DIR`` / ``JCODE_ACP_SOCKET`` pointing outside the isolated
root refuses to run.

What it proves
--------------
  1. ``session/load`` for the exact declared id is forwarded upstream exactly
     once, with the id byte-identical, and never as ``session/new``.
  2. A different, unrelated session in the same isolated store is never
     selected or touched.
  3. Negative cases fail closed without an upstream call:
       - unknown id        -> -32010 unknown_session
       - traversal id      -> -32010 invalid_session_id
       - no live socket    -> -32010 daemon_not_running
  4. Immediately after a successful adoption a bogus id is still refused, so
     success is not a blanket allow.
  5. The shim reports ``spawnsDaemon: false`` and never forwards close/new.

Usage::

    python3 acceptance/shim_adoption_probe.py --session-id probe-session-0001

Exit 0 only when every check passes. There is no default session id: an
omitted ``--session-id`` is an error, not a fallback scan.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from jcode_bridge.acp_shim import (  # noqa: E402
    MODE_READ_ONLY,
    SHIM_PRECONDITION_FAILED,
    AdoptionShim,
    InProcessUpstream,
    ShimConfig,
)

ENV_OVERRIDES = ("JCODE_ACP_SESSION_DIR", "JCODE_ACP_SOCKET")


def _short_root() -> str:
    """Temp root with a short path: AF_UNIX is capped near 104 bytes."""
    for candidate in ("/tmp", "/private/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


class Checker:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.passes: List[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passes.append(label)
            print("PASS  " + label + (("  [" + detail + "]") if detail else ""))
        else:
            self.failures.append(label)
            print("FAIL  " + label + (("  [" + detail + "]") if detail else ""))
        return ok


class Audit:
    """Record connect/open/spawn events so isolation can be asserted."""

    def __init__(self) -> None:
        self.connects: List[str] = []
        self.opens: List[str] = []
        self.spawns: List[str] = []

    def install(self) -> None:
        audit = self

        def hook(event: str, args: Tuple[Any, ...]) -> None:
            try:
                if event == "socket.connect":
                    address = args[1] if len(args) > 1 else None
                    if isinstance(address, (str, bytes)):
                        audit.connects.append(
                            address.decode() if isinstance(address, bytes) else address
                        )
                elif event == "open" and args:
                    path = args[0]
                    if isinstance(path, (str, bytes)):
                        audit.opens.append(
                            path.decode() if isinstance(path, bytes) else path
                        )
                elif event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
                    audit.spawns.append(event)
            except Exception:  # an audit hook must never break the probe
                pass

        sys.addaudithook(hook)


def _inside(path: str, root: str) -> bool:
    if not isinstance(path, str):
        return False
    try:
        resolved = os.path.realpath(path)
        real_root = os.path.realpath(root)
    except OSError:
        return False
    return resolved == real_root or resolved.startswith(real_root + os.sep)


def _write_session(directory: Path, session_id: str, title: str) -> None:
    payload = {
        "id": session_id,
        "working_dir": str(directory / "project"),
        "title": title,
        "updated_at": "2026-11-03T00:00:00Z",
    }
    (directory / (session_id + ".json")).write_text(
        json.dumps(payload), encoding="utf-8"
    )


class Listener:
    """A stand-in daemon socket: accepts a connect and immediately closes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _shim(session_dir: Path, socket_dir: Path, handler):
    upstream = InProcessUpstream(handler=handler)
    config = ShimConfig(
        session_dirs=(str(session_dir),),
        socket_paths=(str(socket_dir / "probe.sock"),),
    )
    shim = AdoptionShim(config=config, upstream_factory=lambda _cfg: upstream)
    return shim, upstream


def _call(shim: AdoptionShim, method: str, params: Optional[Dict[str, Any]] = None,
          request_id: int = 1) -> Dict[str, Any]:
    message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return shim.handle_message(message) or {}


def _precondition(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    error = payload.get("error") or {}
    return error.get("code"), (error.get("data") or {}).get("precondition")


def _adoption_calls(upstream) -> List[str]:
    """Upstream methods other than initialize: the adoption surface only."""
    return [method for method in upstream.calls if method != "initialize"]


def run(session_id: str, root: Path, audit: Audit) -> int:
    check = Checker()
    sessions = root / "sessions"
    socket_dir = root / "sock"
    sessions.mkdir()
    socket_dir.mkdir()
    session_file = sessions / (session_id + ".json")

    # A second, unrelated session proves the probe never picks "a" session.
    other_id = "probe-unrelated-neighbour"
    _write_session(sessions, session_id, "declared probe session")
    _write_session(sessions, other_id, "must never be selected")
    other_file = sessions / (other_id + ".json")
    before = {
        str(session_file): session_file.stat().st_mtime_ns,
        str(other_file): other_file.stat().st_mtime_ns,
    }

    forwards: List[Tuple[str, Dict[str, Any]]] = []

    def handler(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        forwards.append((method, dict(params)))
        if method == "session/load":
            return {"sessionId": params.get("sessionId"), "modes": []}
        return {}

    print("root:", root)
    print("declared session id:", session_id)

    # --- negative: no live socket -> daemon_not_running ---------------------
    shim, upstream = _shim(sessions, socket_dir, handler)
    init = _call(shim, "initialize", {"protocolVersion": 1})
    caps = (init.get("result") or {}).get("agentCapabilities") or {}
    meta = (caps.get("_meta") or {}).get("jcode.acpShim") or {}
    check.check(caps.get("loadSession") is True, "shim advertises loadSession")
    check.check(meta.get("spawnsDaemon") is False, "shim reports spawnsDaemon=false",
                json.dumps(meta))
    check.check(caps.get("currentModeId") == MODE_READ_ONLY,
                "shim advertises read-only mode", str(caps.get("currentModeId")))
    check.check("close" not in (caps.get("sessionCapabilities") or {}),
                "shim does not advertise session/close")

    payload = _call(shim, "session/load", {"sessionId": session_id, "cwd": "/tmp"})
    code, precond = _precondition(payload)
    check.check(code == SHIM_PRECONDITION_FAILED and precond == "daemon_not_running",
                "no socket -> daemon_not_running", "%s/%s" % (code, precond))
    check.check(_adoption_calls(upstream) == [],
                "no socket -> no adoption call reached upstream",
                str(upstream.calls))

    # --- negative: unknown id ----------------------------------------------
    shim_u, upstream_u = _shim(sessions, socket_dir, handler)
    payload = _call(shim_u, "session/load", {"sessionId": session_id + "-absent"})
    code, precond = _precondition(payload)
    check.check(code == SHIM_PRECONDITION_FAILED and precond == "unknown_session",
                "absent id -> unknown_session", "%s/%s" % (code, precond))
    check.check(upstream_u.calls == [], "absent id -> upstream untouched")

    # --- negative: traversal id --------------------------------------------
    shim_t, upstream_t = _shim(sessions, socket_dir, handler)
    payload = _call(shim_t, "session/load", {"sessionId": "../" + session_id})
    code, precond = _precondition(payload)
    check.check(code == SHIM_PRECONDITION_FAILED and precond == "invalid_session_id",
                "traversal id -> invalid_session_id", "%s/%s" % (code, precond))
    check.check(upstream_t.calls == [], "traversal id -> upstream untouched")

    # --- happy path: exact explicit id, isolated live socket ----------------
    listener = Listener(socket_dir / "probe.sock")
    try:
        shim_ok, upstream_ok = _shim(sessions, socket_dir, handler)
        payload = _call(shim_ok, "session/load",
                        {"sessionId": session_id, "cwd": "/tmp"})
        check.check("error" not in payload, "exact id adopted",
                    json.dumps(payload.get("result"))[:160])
        (result_id,) = [p.get("sessionId") for m, p in forwards
                        if m == "session/load"] or [None]
        check.check(result_id == session_id,
                    "session/load forwarded verbatim, exactly once",
                    "forwarded=%r" % (result_id,))
        check.check([m for m, _ in forwards] == ["session/load"],
                    "session/load is the only adoption call forwarded",
                    str([m for m, _ in forwards]))
        check.check(_adoption_calls(upstream_ok) == ["session/load"],
                    "session/new/close were never forwarded",
                    str(upstream_ok.calls))
        check.check(payload.get("result", {}).get("sessionId") == session_id,
                    "upstream result echoed the exact id")

        # --- the gate is still a gate after a success ----------------------
        payload = _call(shim_ok, "session/load",
                        {"sessionId": "definitely_not_a_real_session_xyz"})
        code, precond = _precondition(payload)
        check.check(code == SHIM_PRECONDITION_FAILED and precond == "unknown_session",
                    "bogus id after adoption still refused", "%s/%s" % (code, precond))

        close_payload = _call(shim_ok, "session/close", {"sessionId": session_id})
        check.check(close_payload.get("result", {}).get("closed") is False,
                    "session/close is local, not forwarded")
        check.check(_adoption_calls(upstream_ok) == ["session/load"],
                    "session/close never reached upstream", str(upstream_ok.calls))
    finally:
        listener.close()

    # --- isolation evidence ------------------------------------------------
    after = {
        str(session_file): session_file.stat().st_mtime_ns,
        str(other_file): other_file.stat().st_mtime_ns,
    }
    check.check(before[str(session_file)] == after[str(session_file)],
                "declared session file was not modified")
    check.check(before[str(other_file)] == after[str(other_file)],
                "unrelated session file was not modified")

    outside_connects = [p for p in audit.connects if not _inside(p, str(root))]
    check.check(audit.connects and all(_inside(p, str(root)) for p in audit.connects),
                "every socket connect targeted the isolated root",
                str(sorted(set(audit.connects))))
    check.check(not outside_connects, "no connect to a real daemon socket",
                str(outside_connects))

    json_opens = [p for p in audit.opens
                  if p.endswith(".json") and os.path.isabs(p)]
    outside_opens = [p for p in json_opens if not _inside(p, str(root))]
    check.check(not outside_opens, "no session json read outside the probe root",
                str(outside_opens[:5]))
    check.check(audit.spawns == [], "no subprocess was spawned", str(audit.spawns))

    print()
    print("checks passed: %d, failed: %d" % (len(check.passes), len(check.failures)))
    if check.failures:
        print("FAILED:", ", ".join(check.failures))
        return 1
    print("PASS: synthetic isolated adoption probe satisfied every check")
    return 0


def _validate(argv: argparse.Namespace) -> Optional[str]:
    if not argv.session_id:
        return "--session-id is required; this probe never picks a session for you"
    sid = argv.session_id
    if not sid or sid != sid.strip():
        return "--session-id must be non-empty and must not be padded"
    if sid.startswith(".") or "/" in sid or "\\" in sid or "\x00" in sid:
        return "--session-id must be a plain filename stem"
    if len(sid) > 200:
        return "--session-id is too long"
    for name in ENV_OVERRIDES:
        value = os.environ.get(name)
        if value:
            return ("%s is set (%r). Unset it: this probe must not inherit a "
                    "real session store or socket" % (name, value))
    if not hasattr(socket, "AF_UNIX"):
        return "unsupported platform: no AF_UNIX sockets (Windows)"
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolated synthetic acceptance probe for the ACP adoption shim.",
    )
    parser.add_argument("--session-id", required=False, default=None,
                        help="Exact session id to adopt. Required. No fallback.")
    args = parser.parse_args(argv)

    problem = _validate(args)
    if problem:
        print("REFUSED:", problem)
        return 2

    audit = Audit()
    audit.install()

    root = Path(tempfile.mkdtemp(prefix="acp-adopt-probe-", dir=_short_root()))
    try:
        return run(args.session_id, root, audit)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
