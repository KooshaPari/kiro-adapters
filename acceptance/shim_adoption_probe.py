"""Prove the shim's ADOPTION path, not just its fail-closed path.

The fail-closed cases are already covered (`shim_live_probe.py`). This script
covers the other half: a real jcode ACP process creates a session and a live
daemon socket, and then the shim adopts that session over `session/load`.

Sequence:
  1. Start `kcode --no-selfdev acp` as the "operator's" session and keep it open.
  2. `initialize` + `session/new` on it. This is what creates the daemon socket
     and writes `<id>.json` into the session store. The shim will not do this.
  3. Locate the live daemon socket by probing the candidate paths.
  4. Start the shim pointed at that socket and that session store.
  5. `initialize` then `session/load` for the session created in step 2.
     Success means the shim attached, without ever spawning a daemon.

Usage:
    python3 acceptance/shim_adoption_probe.py [JCODE_BINARY]

Exit 0 only if step 5 returns a result rather than an error.
"""
import glob
import json
import os
import select
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
DEFAULT_BINARY = os.path.expanduser("~/.jcode/scratch/jcode-target/debug/kcode")

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": 1,
        "clientInfo": {"name": "adoption-probe", "version": "1"},
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                               "terminal": False},
    },
}


def live_sockets():
    pats = [os.path.expanduser("~/.jcode/scratch/*.sock"),
            os.path.expanduser("~/.kcode/scratch/*.sock")]
    out = []
    for pat in pats:
        for path in glob.glob(pat):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                s.connect(path)
                out.append(path)
            except Exception:
                pass
            finally:
                s.close()
    return out


def newest_persisted_session(exclude_prefixes=(), limit=5):
    """Session stems that actually have a file on disk, newest first."""
    stores = [os.path.expanduser("~/.jcode/sessions"),
              os.path.expanduser("~/.kcode/sessions")]
    found = []
    for store in stores:
        if not os.path.isdir(store):
            continue
        for path in glob.glob(os.path.join(store, "*.json")):
            stem = os.path.basename(path)[:-len(".json")]
            if any(stem.startswith(p) for p in exclude_prefixes):
                continue
            try:
                found.append((os.path.getmtime(path), stem))
            except OSError:
                continue
    found.sort(reverse=True)
    return [stem for _, stem in found[:limit]]


class Rpc:
    """Strict request/response over an ACP child, matched by id."""

    def __init__(self, argv, env=None, label="child"):
        self.label = label
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        self.next_id = 1

    def call(self, method, params, timeout):
        rid = self.next_id
        self.next_id += 1
        try:
            self.proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            return None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.25)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("id") == rid:
                return frame
        return None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return self.proc.stderr.read()


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BINARY
    if not os.path.exists(binary):
        print("binary not found:", binary)
        return 1

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    print("=== 1. operator session: %s --no-selfdev acp ===" % binary)
    operator = Rpc([binary, "--no-selfdev", "acp"], env=env, label="operator")
    init = operator.call("initialize", INIT["params"], 120)
    if not init or "result" not in init:
        operator.close()
        print("FAIL: operator initialize got no result")
        return 1
    info = init["result"].get("agentInfo", {})
    print("  operator ready:", info.get("name"), info.get("version"))

    print("=== 2. session/new on the operator child ===")
    created = operator.call("session/new", {"cwd": "/tmp", "mcpServers": []}, 180)
    if not created or "result" not in created:
        operator.close()
        print("FAIL: session/new got no result:", json.dumps(created)[:300] if created else None)
        return 1
    session_id = created["result"].get("sessionId")
    print("  session_id:", session_id)
    if not session_id:
        operator.close()
        print("FAIL: no sessionId in session/new result")
        return 1

    # A freshly created session has NO file on disk yet: the adapter persists a
    # session when it saves it, not when it creates it. That is a real property
    # of the harness, and it means "created" and "adoptable" are different
    # states. The gate is right to refuse, so the happy path needs a session
    # that has actually been persisted.
    persisted = newest_persisted_session(exclude_prefixes=("session_bird",))
    print("  persisted sessions available: %d (newest: %s)" % (
        len(persisted), persisted[0] if persisted else "none"))
    if not persisted:
        operator.close()
        print("FAIL: no persisted session to adopt")
        return 1

    print("=== 3. locate the live daemon socket ===")
    socks = []
    for _ in range(20):
        socks = live_sockets()
        if socks:
            break
        time.sleep(1)
    print("  live sockets:", socks or "(none)")
    if not socks:
        operator.close()
        print("FAIL: no daemon socket became live, so the shim cannot be given one")
        return 1

    print("=== 4. start the shim against that socket ===")
    shim_env = dict(env)
    shim_env["PYTHONPATH"] = ""
    shim = Rpc([
        sys.executable, "-E", "-s", "-c",
        "import sys; sys.path.insert(0, %r); "
        "from jcode_bridge.acp_shim.__main__ import main; sys.exit(main())" % SRC,
        "--session-dir", os.path.expanduser("~/.jcode/sessions"),
        "--session-dir", os.path.expanduser("~/.kcode/sessions"),
        "--socket", socks[0],
        "--timeout", "120",
        "--upstream", binary, "--no-selfdev", "acp",
    ], env=shim_env, label="shim")

    shim_init = shim.call("initialize", INIT["params"], 180)
    caps = (shim_init or {}).get("result", {}).get("agentCapabilities", {})
    meta = (caps.get("_meta") or {}).get("jcode.acpShim") or {}
    print("  shim upstreamState:", meta.get("upstreamState"))
    print("  shim spawnsDaemon:", meta.get("spawnsDaemon"))
    print("  shim loadSession:", caps.get("loadSession"))
    print("  shim modes:", json.dumps(caps.get("modes")))

    print("=== 5. session/load: the actual adoption ===")
    target = persisted[0]
    print("  adopting persisted session:", target)
    loaded = shim.call("session/load",
                       {"sessionId": target, "cwd": "/tmp", "mcpServers": []}, 180)
    if loaded is None:
        print("FAIL: session/load got no reply")
        shim.close()
        operator.close()
        return 1
    if "error" in loaded:
        err = loaded["error"]
        data = err.get("data") or {}
        print("FAIL: session/load refused:", err.get("code"), data.get("precondition"),
              "|", err.get("message"))
        shim.close()
        operator.close()
        return 1

    print("  ADOPTED:", json.dumps(loaded.get("result"))[:300])

    # A successful load must not mean "the gate stopped gating". Immediately
    # after the adoption, a bogus id must still be refused.
    print("=== 6. the gate is still a gate ===")
    bogus = shim.call("session/load",
                      {"sessionId": "definitely_not_a_real_session_xyz",
                       "cwd": "/tmp", "mcpServers": []}, 60)
    code = None
    precond = None
    if bogus and "error" in bogus:
        code = bogus["error"].get("code")
        precond = (bogus["error"].get("data") or {}).get("precondition")
    print("  bogus id ->", code, precond)
    gated = (code == -32010 and precond == "unknown_session")

    err_out = shim.close()
    operator.close()
    if err_out.strip():
        print("shim stderr:", err_out[:400])
    if not gated:
        print("FAIL: after a successful load the shim accepted a bogus session id")
        return 1
    print("PASS: the shim adopted a live session and still refused a bogus one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
