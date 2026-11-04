# Why a Kiro Crew agent session connects to 0 MCP servers

Date: 2026-11-04. Diagnosis only; no config was changed.
Host: macOS aarch64, Kiro Crew Nightly (`backend-dist/kirocrew-backend-arm64`).

## Verdict

**Cause A is the decisive one.** `~/.kiro/crew/mcp.json` is not an input to the
agent session at all. On this host the session backend is **codex**
(`~/.kiro/crew/config.json` → `agent.acp_backend = "codex"`, `provider = "acp"`,
`default_agent = "default"`, session template `kirocrew`). codex-acp reads no
`~/.kiro/agents/*.json`; Crew must mirror the agent spec onto the
`session/new` `mcpServers` array itself. That mirror reads the **agent spec**,
not Crew's own server list.

Source of truth, quoted from the installed backend:

- `lib/python3.12/site-packages/kiro_crew/acp/session_mcp.py:1-9`
  > "For a harness in `ACP_BACKENDS_SESSION_MCP_ARRAY`, the `session/new` /
  > `session/load` `mcpServers` parameter is where Kiro Crew's MCP servers come
  > from **and the only place**: neither claude-agent-acp nor codex-acp reads
  > `~/.kiro/agents/<name>.json`."
- `acp/session_mcp.py:368` — the only global registry read is
  `~/.kiro/settings/mcp.json` ("The user's global `~/.kiro/settings/mcp.json`,
  `{}` when absent or bad").
- `acp/session_mcp.py:1472` — the project registry is
  `<work_dir>/.kiro/settings/mcp.json`.
- `acp/session_mcp.py` header — "The agent spec stays the single source of truth;
  there is no second, claude-shaped registry to keep in sync."

`~/.kiro/crew/mcp.json` is a **host/dashboard projection only** (it is what
`GET /api/mcp/active` renders). That is exactly the divergence the earlier worker
observed.

### Confirmed by the config files

| File | Contains `kiro-inventory`? |
|---|---|
| `~/.kiro/crew/mcp.json` (host projection, `/api/mcp/active`) | **yes** |
| `~/.kiro/agents/kirocrew.json` (spec for `selection_name: kirocrew`) | **no** — its 15 `mcpServers` are `kirocrew-cron/core/guide/mate/computer/work`, `chrome-devtools`, 5×`cloudflare-*`, `octocode`, `phenotype-portal`, `workos`, `workos-docs`, `jcode-bridge` |
| `~/.kiro/agents/*.json` (all 17 specs) | **no** — zero specs declare it |
| `~/.kiro/settings/mcp.json` | file does not exist |
| `<work_dir>/.kiro/settings/mcp.json` | does not exist |

So the tool call in
`~/.kiro/crew/sessions/dashboard_chat-2-1793781242.jsonl` (line 3,
`meta.input.server = "kiro-inventory"`) names a server that was never in the
session's array — hence `Error: MCP server 'kiro-inventory' not connected`.

### Secondary, by design (a C-shaped filter, but intentional)

codex mirror, `kiro_crew/providers/mirrors/codex.py:188` (`codex_withheld_servers`)
and `:379`, logs at session spawn. Matching lines in
`~/.kiro/crew/gateway.log` for the same PID/time as the failing session
(00:34:32, PID 81136; the jsonl turn is 08:34Z):

```
codex session MCP: withholding server 'kirocrew-computer' -- this transport cannot
deliver what makes it correct (a per-tool restriction it has no deny channel for,
or the session identity a Crew server binds to)
   ... same for 'kirocrew-guide', 'kirocrew-mate'
acp.mcp_ref_guard: agent-spec tool refs name no MCP server in Crew's projection for
this session: backend='codex' agent='kirocrew' unresolved=@kirocrew-computer,
@kirocrew-guide, @kirocrew-mate mcp_gateway=off.
```

`agent.mcp_gateway.enabled = false` in `config.json`, so no pooled broker stubs
substitute for them.

### Ruled out

- **B (binary fails to start):** `/Users/kooshapari/bin/kiro-adapter-mcp
  --inventory-only` run as a plain subprocess returns exit 0, `initialize` →
  `serverInfo {"name":"kiro-adapters","version":"0.1.0"}`, `tools/list` → **4
  tools**: `agent_sessions_list`, `agent_session_get`, `agent_session_history`,
  `agent_sources`. The binary is healthy; it is simply never told to run.
- **D (quarantine excludes it):** `~/.kiro/crew/mcp-quarantine.json` quarantines
  `kirocrew-cron/core/guide/mate/computer` and `jcode-bridge` for **discovery
  probes** only. `config/sections_agent.py:205-215` states "The server stays
  mounted, but while its streak is at or over this threshold MCP discovery stops
  spawning it for probes." Quarantine never removes a spec-declared server from
  `session/new`. `kiro-inventory` is not in quarantine at all.
- **C (agent-level allowlist):** `mcp_registry_mode = false` (registry filter
  off). The `tools`/`allowedTools` lists gate per-tool access, not whole-server
  mounting; `kirocrew.json` does reference `@kirocrew-*` (only
  computer/guide/mate are unresolved, per the log).
- `agent.sandbox = "auto"` with no OS sandbox backend is what makes *probing*
  fail (`mcp_discovery_probe.py:840`, `mcp_probe_sandbox_unavailable`), but the
  probe path is explicitly documented as not the launch path for chat sessions
  ("kiro-cli launches it from the agent config without this probe").

## Minimal change that would fix it (described, NOT applied)

Control-plane route, no gateway restart needed (specs are re-read per spawn,
`session_mcp.py` header: "It is read per spawn, so installing or toggling an MCP
server takes effect on the NEXT session with no gateway restart"):

1. Add `kiro-inventory` to `~/.kiro/agents/kirocrew.json` →
   `mcpServers["kiro-inventory"] = {"command": "/Users/kooshapari/bin/kiro-adapter-mcp", "args": ["--inventory-only"], "env": []}`
   (mirror the existing `jcode-bridge` entry's shape; stdio entries need `env`).
2. Add `"@kiro-inventory"` to that spec's `tools` (and `allowedTools` if the
   loader requires both) so the projection's allowlist grants it.

That is the whole fix for the reported symptom. Optionally, to also make
`kirocrew-computer/guide/mate` reachable, `agent.mcp_gateway.enabled = true`
(pooled stubs are the supported path for identity-bound servers on the codex
backend) — a larger change, not required for `kiro-inventory`.

## What would still be unproven after that change

- The turn `dashboard_chat-3` reported **"Connected MCP servers: 0 (0 tools)"**
  while `kirocrew-core` and `kirocrew-cron` were neither withheld nor
  quarantine-blocked from mounting. Adding `kiro-inventory` does not explain
  that; it only adds a seventh server to an array that is allegedly already
  non-empty. Something after projection (spawn failure of every stdio child
  under the codex session env, or a `drop_unadvertised_transports` narrowing at
  `mirrors/codex.py:98`) is still unverified — it needs `session/new` params
  capture plus a per-child spawn log for one session.
- Whether the `agent-im*` server named in "Configured but not connected:" is the
  same class of missing-speck entry (it is also absent from `kirocrew.json`).
- Whether `sandbox = "auto"` with no available backend also blocks the *session*
  spawn path (as opposed to the probe path). Only a controlled run with
  `agent.sandbox_allow_unsandboxed_exec = true` would separate the two.
