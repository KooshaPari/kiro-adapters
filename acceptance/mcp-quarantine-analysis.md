# KiroCrew MCP reachability: where the 14 inventory tools actually live

Measured 2026-11-02. This closes the gap left by the earlier note that "the MCP
servers are quarantined", which turned out to be true for one server and
misleading about the tools.

## The quarantine

`~/.kiro/crew/mcp-quarantine.json` holds 6 servers, all crossed at the same
instant `2026-10-29 15:59:59Z`, all `fails: 3`:

| Server | Last error |
|---|---|
| `kirocrew-cron` | `timeout` |
| `kirocrew-core` | `timeout` |
| `kirocrew-guide` | `timeout` |
| `kirocrew-mate` | `timeout` |
| `kirocrew-computer` | `timeout` |
| `jcode-bridge` | `mcp_probe_sandbox_unavailable: ... no OS-level sandbox backend is available on this host` |

## Two distinct problems, not one

### 1. `jcode-bridge` was never broken

The quarantine error is a **probe** failure, not a server failure. The message
says so itself: "Kiro Crew could not probe this server because no OS-level
sandbox backend is available on this host. The server itself may be fine".

Measured directly:

```
$ ~/bin/jcode-bridge-mcp --help
jcode-bridge-mcp: MCP stdio server exposing jcode as a task backend.
Tools: jcode_health, jcode_run, jcode_task_start, ... (10 tools)
EXIT=0

$ ~/bin/kiro-adapter-mcp --selftest
EXIT=0   (14 tools listed)
```

Both servers start fine. macOS has no cgroup sandbox backend, so KiroCrew's
**discovery probe** can never succeed here. This is the same host limitation
that made `agent.sandbox_allow_unsandboxed_exec=false` mean "cannot start an
agent" on Windows.

### 2. The 14 inventory tools are in a server KiroCrew never configured

The tools are carried by **`kiro-adapter-mcp`**, registered in
`~/.jcode/mcp.json` under the server name `kiro`:

```json
"kiro": {
  "command": "/Users/kooshapari/bin/kiro-adapter-mcp",
  "args": [],
  "description": "Kiro adapters: kirocrew + kirocode. 10 tools; ...",
  "timeout_secs": 240,
  "shared": true
}
```

Its 14 tools are `kirocrew_status`, `kirocrew_agents`, `kirocrew_ask`,
`kirocrew_spawn`, `kirocrew_spawn_list`, `kirocrew_run_spec`,
`kirocrew_dashboard_url`, `kirocode_status`, `kirocode_ask`, `kiro_dispatch`,
`agent_sessions_list`, `agent_session_get`, `agent_session_history`,
`agent_sources`.

KiroCrew's own `~/.kiro/crew/mcp.json` configures a **different** set:
`jcode-bridge`, `chrome-devtools`, `cloudflare-bindings`, `cloudflare-builds`,
`cloudflare-docs`, `cloudflare-observability`, `octocode`, `phenotype-portal`,
`workos`, `workos-docs`. **`kiro-adapter-mcp` is absent from it.**

So the quarantine was never the reason the inventory tools were unreachable.
They were simply not registered where KiroCrew looks.

## Live proof the tools work today

Called through the `kiro` MCP server, unmodified:

```json
{"ok": true, "harness": "inventory", "action": "agent_sessions_list",
 "code": "OK", "detail": "5 session(s) detected",
 "data": {"count": 5, "include_stale": true, "sessions": [...]}}
```

Each session carries honest per-capability flags rather than a blanket claim:

```json
"capabilities": {
  "discover_session": {"supported": true, "probe": "jcode sessions/*.json", "observed_at": "2026-11-02T01:48:14Z"},
  "discover_process": {"supported": false, "reason": "no pid is recorded for this session, so no process was probed"},
  "read_native_history": {"supported": true, "probe": "jcode sessions/<native_id>.json messages[]"},
  "enroll_existing": {"supported": false, "reason": "enrollment requires the harness's own session path; ..."},
  "join_same_live_session": {"supported": false, "reason": "joining a live session requires the harness protocol (ACP); ..."}
}
```

## The exact remaining change

To make the tools reachable from inside KiroCrew, add the `kiro` server to
`~/.kiro/crew/mcp.json`:

```json
"kiro": {
  "command": "/Users/kooshapari/bin/kiro-adapter-mcp",
  "args": [],
  "type": "stdio"
}
```

`type: stdio` matches the shape of every other entry in that file (for example
`jcode-bridge`). **Not done in this pass** — it changes which servers a live
agent may spawn, and it deserves the operator's explicit go-ahead.

Two caveats that must travel with the change:

1. **KiroCrew's discovery probe will quarantine it too**, for the same reason it
   quarantined `jcode-bridge`: no OS sandbox backend on macOS. The quarantine
   error is about probing, not about the server. The probe must run
   unsandboxed, or the exclusion must be cleared by name after the fact.
2. The quarantine outlives a gateway restart by design (`mcp_discovery.py:45-49`:
   "the quarantine is re-read process memory while the quarantine is durable").
   `POST /api/mcp/quarantine/clear` requires a `name` in the body and clears one
   server's counter and flag
   (`dashboard/handlers/mcp_probe.py:842-872`). It deliberately does not touch a
   `disabled` flag in any config file, and it does not mount or unmount anything.

## What is NOT claimed

- Not claimed: the `kirocrew-*` servers were fixed. Their errors were `timeout`,
  which is a different fault from the sandbox-probe error, and they are not in
  KiroCrew's `mcp.json` under those names either. Where they are configured from
  is not established.
- Not claimed: adding the server to `mcp.json` is safe by inspection. It changes
  what a live agent can spawn.
