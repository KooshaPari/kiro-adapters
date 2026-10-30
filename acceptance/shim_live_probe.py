"""Live probe for the ACP adoption shim.

The shim's stdio loop is SERIAL: it reads one message, handles it fully, then
reads the next. So this probe is strictly request/response -- send, wait for the
matching id, then send the next one. Pipelining would make the later replies
arrive long after their probe window closed.

Usage:
    python3 acceptance/shim_live_probe.py [stub|<path-to-jcode-binary>]

With no argument it uses the stub upstream, which never answers `initialize`.
That exercises the fail-closed path: the shim must still answer, and must
answer with the fallback capabilities.
"""
import json
import os
import select
import subprocess
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acp_stub.py")


def shim_argv():
    return [
        sys.executable, "-E", "-s", "-c",
        "import sys; sys.path.insert(0, %r); "
        "from jcode_bridge.acp_shim.__main__ import main; sys.exit(main())" % SRC,
    ]


class Client:
    def __init__(self, upstream, extra_args=()):
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        self.proc = subprocess.Popen(
            shim_argv() + ["--upstream"] + list(upstream) + list(extra_args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self.next_id = 1

    def call(self, method, params, timeout):
        rid = self.next_id
        self.next_id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
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


REAL_SESSION = "session_ant_1791700258468_d56c3f871c03e946"


def report(client, label, timeout):
    print("=== %s ===" % label)
    init = client.call("initialize", {
        "protocolVersion": 1,
        "clientInfo": {"name": "live-probe", "version": "1"},
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                               "terminal": False},
    }, timeout)
    if init is None:
        print("  initialize -> NO REPLY")
    else:
        res = init.get("result", {})
        caps = res.get("agentCapabilities") or {}
        session_caps = caps.get("sessionCapabilities") or {}
        print("  initialize.protocolVersion:", res.get("protocolVersion"))
        print("  initialize.loadSession:", caps.get("loadSession"))
        print("  initialize.modes:", json.dumps(caps.get("modes")))
        print("  initialize.currentModeId:", caps.get("currentModeId"))
        print("  initialize.sessionCapabilities:", json.dumps(session_caps))
        print("  initialize.closeAdvertised:", "close" in session_caps)
        print("  initialize.agentInfo:", json.dumps(res.get("agentInfo")))
        meta = (caps.get("_meta") or {}).get("jcode.acpShim") or {}
        print("  initialize.shimMeta:", json.dumps(meta))
    print()

    cases = [
        ("load: existing session, no daemon",
         "session/load", {"sessionId": REAL_SESSION, "cwd": "/tmp", "mcpServers": []}),
        ("load: unknown session",
         "session/load", {"sessionId": "definitely_not_a_real_session_xyz", "cwd": "/tmp",
                          "mcpServers": []}),
        ("load: path traversal",
         "session/load", {"sessionId": "../../../../etc/passwd", "cwd": "/tmp",
                          "mcpServers": []}),
        ("load: dot-prefixed id",
         "session/load", {"sessionId": ".hidden", "cwd": "/tmp", "mcpServers": []}),
        ("new: session/new is blocked",
         "session/new", {"cwd": "/tmp", "mcpServers": []}),
        ("close: never forwarded",
         "session/close", {"sessionId": REAL_SESSION}),
        ("mode: unrestricted refused",
         "session/set_mode", {"modeId": "unrestricted"}),
        ("mode: read-only accepted locally",
         "session/set_mode", {"modeId": "read-only"}),
        ("config: mode via set_config_option",
         "session/set_config_option", {"configId": "mode", "value": "read-only"}),
        ("config: write option refused",
         "session/set_config_option", {"configId": "allow_writes", "value": "true"}),
        ("unknown method",
         "session/definitely_not_a_method", {}),
    ]
    for label, method, params in cases:
        frame = client.call(method, params, 30)
        if frame is None:
            print("  %-34s -> NO REPLY" % label)
        elif "error" in frame:
            err = frame["error"]
            data = err.get("data") or {}
            print("  %-34s -> error %s [%s] %s" % (
                label, err.get("code"), data.get("precondition"), err.get("message")[:80]))
        else:
            print("  %-34s -> RESULT %s" % (label, json.dumps(frame.get("result"))[:130]))
    print()


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "stub"
    if which == "stub":
        client = Client([sys.executable, STUB])
        report(client, "STUB upstream (never answers initialize)", 90)
    else:
        client = Client([which, "--no-selfdev", "acp"])
        report(client, "REAL upstream %s" % which, 120)
    err = client.close()
    if err.strip():
        print("stderr:", err[:800])


if __name__ == "__main__":
    main()
