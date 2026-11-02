# KiroCrew MCP reachability: where the 14 inventory tools actually live

Measured 2026-11-02 against the installed KiroCrew Nightly backend
(`/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64`,
Python 3.12 site-packages) and the live gateway on `http://localhost:5476`.

This closes the gap left by the earlier note that "the MCP servers are
quarantined", which was true for one server and misleading about the tools.

## Headline answers

| Question | Answer |
|---|---|
| Is the quarantine still enforced today? | **Yes, for the discovery probe.** It is **permanent until an explicit clear** (no TTL, no expiry). It does **not** unmount the server from the agent config. |
| Exact clear-endpoint semantics | `POST /api/mcp/quarantine/clear` with body `{"name": "<server>"}` clears **exactly one named server** (counter + crossed flag). Owner-authenticated. There is no "clear all" form. |
| Which server carries the 14 inventory tools | **`kiro-adapter-mcp`** (registered in `~/.jcode/mcp.json` as `kiro`). `jcode-bridge-mcp` does **not** carry them. |
| Are the 14 tools reachable through KiroCrew today | **No.** `kiro-adapter-mcp` is absent from `~/.kiro/crew/mcp.json`, which is where KiroCrew looks. |

## The quarantine

`~/.kiro/crew/mcp-quarantine.json` holds **6** servers, all crossed at the same
instant `2026-10-29 15:59:59Z` (`crossed_at = 1793314799.1781309`), all
`fails: 3`:

| Server | Last error |
|---|---|
| `kirocrew-cron` | `timeout` |
| `kirocrew-core` | `timeout` |
| `kirocrew-guide` | `timeout` |
| `kirocrew-mate` | `timeout` |
| `kirocrew-computer` | `timeout` |
| `jcode-bridge` | `mcp_probe_sandbox_unavailable: ... no OS-level sandbox backend is available on this host` |

Record shape (`mcp_quarantine.py:_sanitize`, `:398`): `fails`, `last_status`,
`last_error`, `last_failed_at`, `crossed_at`. `version: 1`, keyed by server name.

### The two lists are NOT the same set

`~/.kiro/crew/mcp.json` configures 10 servers. The quarantine names 6. The
overlap is **one** server:

```
in both:                jcode-bridge
only in quarantine:     kirocrew-computer, kirocrew-core, kirocrew-cron,
                        kirocrew-guide, kirocrew-mate
only in mcp.json:       chrome-devtools, cloudflare-bindings, cloudflare-builds,
                        cloudflare-docs, cloudflare-observability, octocode,
                        phenotype-portal, workos, workos-docs
```

The nine `mcp.json`-only servers are **not quarantined**. A prior note listing
`chrome-devtools`, `cloudflare-*`, `octocode`, `phenotype-portal`, `workos` and
`workos-docs` as quarantined is **not supported by the file as it stands today**.

### Where the `kirocrew-*` names come from

They are **built-in managed servers**, not user config. Two declarations in the
installed package:

`kiro_crew/agent.py:1351-1496` — the authoritative mount set:

```python
_MANAGED_MCP_SERVERS: dict[str, dict] = {
    "kirocrew-cron": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-cron")},
    "kirocrew-core": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-core")},
    "kirocrew-computer": {...},
    ...
```

`kiro_crew/mcp_discovery_managed.py:22-33` — the same set as CLI subcommands:

```python
_MANAGED_SERVER_SUBCOMMANDS = {
    "kirocrew-core": "mcp-core",
    "kirocrew-cron": "mcp-cron",
    "kirocrew-computer": "mcp-computer",
    "kirocrew-dashboard": "mcp-dashboard",
    "kirocrew-work": "mcp-work",
    "kirocrew-crew-log": "mcp-crew-log",
    "kirocrew-debug": "mcp-debug",
    "kirocrew-panel": "mcp-panel",
    "kirocrew-guide": "mcp-guide",
    "kirocrew-mate": "mcp-mate",
}
```

Ten managed servers exist. Five are `opt_in` (assignable, never auto-emitted:
`kirocrew-dashboard`, `kirocrew-work`, `kirocrew-crew-log`, `kirocrew-debug`,
`kirocrew-panel`), one carries a platform `spec_gate`
(`kirocrew-computer`, gated on a native driver plus the keystone primary
enable), and four are always-on (`kirocrew-cron`, `kirocrew-core`,
`kirocrew-guide`, `kirocrew-mate`). On this host the computer gate is open, so
**five** managed names materialize into the generated agent config — and those
five are exactly the five `kirocrew-*` names in the quarantine.

Materialization path: `agent.py:2516` calls
`managed_mcp.emit_managed_servers(...)`
(`kiro_crew/agent_materialization/managed_mcp.py:643`), which writes the
managed set into a freshly built `mcpServers` map. The result is visible in the
generated agent config `~/.kiro/agents/kirocrew.json`, whose 15 entries are the
10 from `mcp.json` plus the 5 always-on managed servers:

```
kirocrew-cron, kirocrew-core, kirocrew-guide, kirocrew-mate, kirocrew-computer,
jcode-bridge, chrome-devtools, cloudflare-bindings, cloudflare-builds,
cloudflare-docs, cloudflare-observability, octocode, phenotype-portal,
workos, workos-docs
```

So the probe loop sees a 15-server universe, and it is the 6 above that
quarantined.

## 1. Is the quarantine still enforced?

**Yes — for the discovery probe — and it is permanent until explicitly cleared.**

### The exclusion predicate

`kiro_crew/mcp_quarantine.py:424` decides what "failing" means:

```python
"failing": bool(limit > 0 and rec.get("crossed_at") and fails >= limit),
```

`limit` is `threshold()` (`mcp_quarantine.py:106-116`), read live (not cached)
from the agent config so the off switch really is an off switch:

```python
return max(0, int(KiroCrewConfig.load().agent.mcp_quarantine_after_failures))
```

On this host `~/.kiro/crew/config.json` sets `agent.mcp_quarantine_after_failures = 3`.
All 6 records have `fails: 3` and `crossed_at` set, so `failing` is `True` for
every one of them today.

### Where it stops the spawn

`kiro_crew/mcp_discovery.py:53-84` builds the excluded set:

```python
excluded = {name for name, st in snap.items() if st.get("failing")}
```

and `mcp_discovery.py:178-198` applies it — the server is **not spawned** and
its row is reported as stale rather than freshly failed:

```python
if s.name in excluded:
    s.status = "outdated"
    s.error = ""
    _drop_temp_refusals(s)
    return s
```

The module's own warning states the durability explicitly
(`mcp_discovery.py:74-81`):

> "MCP server %s failed %d consecutive probes, so discovery will no longer spawn
> it. Fix it, then clear it from the MCP panel (POST
> /api/mcp/quarantine/clear) — the exclusion outlives a gateway restart; or set
> agent.mcp_quarantine_after_failures to 0 to stop quarantining."

### Time-bounded or permanent? **Permanent.**

There is **no TTL and no expiry**. `crossed_at` is written
(`mcp_quarantine.py:401-402`) and then only ever read as a truthiness flag
(`:424`). No code compares it against the clock. The record persists until
`clear()` removes it, or until the operator sets
`agent.mcp_quarantine_after_failures = 0`, which makes the predicate
`limit > 0` false for every server.

### Scope: the quarantine gates the PROBE, not the MOUNT

This distinction decides whether a quarantined server is still usable.

The quarantine is consumed **only** in the discovery-probe path. A search of
the installed package for `mcp_quarantine` references returns
`mcp_quarantine.py`, `mcp_discovery.py`, the dashboard handlers/routes, the
config loader, and `mcp_dashboard_folder_tree.py` — and **not**
`agent_materialization/mcp_sources.py` or the managed-server emit path.

Independently confirmed against the generated agent config
`~/.kiro/agents/kirocrew.json`: all 6 quarantined names are present with **no**
`disabled` key:

```
jcode-bridge:      present=True disabled=None cmd=/Users/kooshapari/bin/jcode-bridge-mcp
kirocrew-cron:     present=True disabled=None cmd=.../bin/kirocrew
kirocrew-core:     present=True disabled=None cmd=.../bin/kirocrew
kirocrew-guide:    present=True disabled=None cmd=.../bin/kirocrew
kirocrew-mate:     present=True disabled=None cmd=.../bin/kirocrew
kirocrew-computer: present=True disabled=None cmd=.../bin/kirocrew
```

An absent `disabled` is not a mute — `mcp_cleanup.py:58`
(`mcp_entry_is_muted`) treats "anything but an absent `disabled` or a literal
`False`" as a mute, and is deliberately fail-closed. So these entries are not
muted, and KiroCrew does not write `disabled` on its own
(`mcp_quarantine.py:21-25`: "Nor does it ever write `disabled`").

**Conclusion**: a quarantined server is skipped by the *dashboard's* probe and
shows a stale badge with no tool count, but it is still mounted in the agent
config and still launchable by a chat session. The quarantine explains why the
dashboard says "0 tools"; it does not by itself make a server unreachable.

## 2. Is there an API that lists current MCP server status?

**There are status-shaped GET routes, but none is unauthenticated, and none is
a live per-server status feed.** All dashboard MCP routes are registered in
`kiro_crew/dashboard/routes/agent_config.py:75-88`:

| Path | Method | Handler | Auth |
|---|---|---|---|
| `/api/mcp` | GET | `api_mcp_servers` (`dashboard/handlers/mcp_probe.py:407`) | owner token required |
| `/api/mcp/scopes` | GET | `api_mcp_global_scopes` | owner token required |
| `/api/mcp/active` | GET | `api_mcp_active` (`dashboard/handlers/mcp_probe.py:564`) | owner token required |
| `/api/mcp/probe` | GET | `api_mcp_probe_cached` | owner token required |
| `/api/mcp/quarantine/clear` | POST | `api_mcp_quarantine_clear` (`dashboard/handlers/mcp_probe.py:842`) | owner token required |

Probed without credentials:

```
$ curl -sS -m 10 -i http://localhost:5476/api/mcp
HTTP/1.1 403 Forbidden
X-Auth-Required: true
{"error": ...}

$ curl -sS -m 10 -i http://localhost:5476/api/mcp/active
HTTP/1.1 403 Forbidden
X-Auth-Required: true
```

So **no** no-auth API reports the jcode-bridge row. Answering the question as
asked: the status is observable only with the dashboard owner token, and the
handler's own docstring (`mcp_probe.py:408-416`) warns that "It makes no claim
about which of these sources kiro-cli itself loads at session time".

`api_mcp_servers` merges `list_servers()` with the probe cache
(`mcp_probe.py:426-432`) and then arms a background re-probe if the cache is
stale (`:423-424`, `:834-838`). For a quarantined server the probe is skipped,
so the row is served from cache as `outdated` with no tool count — which is
exactly why the dashboard showed `jcode-bridge` as broken while the server was
fine.

## 3. Exact semantics of POST /api/mcp/quarantine/clear

Handler: `kiro_crew/dashboard/handlers/mcp_probe.py:842-905`.

**It clears ONE named server. There is no clear-all form.**

```python
async def api_mcp_quarantine_clear(request: web.Request) -> web.Response:
    """POST /api/mcp/quarantine/clear — release an auto-quarantined MCP server.

    Clears the consecutive-failure COUNTER as well as the quarantine flag.
    ...
    Deliberately does NOT touch ``disabled`` in any config file ...
    It does not mount or unmount anything either -- the
    server was never unmounted.
    """
```

Sequence, with citations:

1. **Auth** (`:859-863`): `require_owner_dashboard_request(request, "mcp_quarantine_clear")`.
   A non-owner gets a denial response; the call never reaches the store.
2. **Body** (`:864-872`): JSON body, `name` extracted by `_string_identifier`.
   An empty or missing name is a `400` with `{"error": "name is required",
   "code": "name_required"}`.
3. **Action** (`:874-875`): `mcp_quarantine.clear(name)`.
   `mcp_quarantine.py:439` — "Reset one server's count; returns the record it
   removed, or ``None`` if absent." It removes the counter **and** the crossed
   flag, so the server is one failure away from re-quarantining, not zero.
4. **Failure path** (`:876-889`): an `OSError`/`ValueError` (store unreadable
   or unwritable) returns `500` with `code: "quarantine_store_write_failed"`,
   rather than reporting a success that never reached disk.
5. **Cache cleanup** (`:899-904`): `probeFailing`/`probeFailures` are dropped
   from matching cached rows so the next poll does not re-render the badge.
6. **Response** (`:905`): `{"ok": True, "name": name, "released": released}`.
   `released` is `False` when there was no record to remove.

**Not called in this pass.** To release all 6, six separate calls with six
distinct names are required.

## 4. Which server carries the 14 inventory tools?

**`kiro-adapter-mcp`.** It is registered in `~/.jcode/mcp.json` under the
server name `kiro`:

```json
"kiro": {
  "command": "/Users/kooshapari/bin/kiro-adapter-mcp",
  "args": [],
  "description": "Kiro adapters: kirocrew (Kiro Crew personal agent) + kirocode (Kiro coding CLI). 10 tools; results carry code OK/NOT_INSTALLED/BLOCKED/TIMEOUT/ERROR/UNSUPPORTED.",
  "timeout_secs": 240,
  "shared": true
}
```

(The `description` says "10 tools" — that string is stale; the server now
serves 14. The tool list, not the description, is authoritative.)

Its 14 tools, measured from `~/bin/kiro-adapter-mcp --selftest` and matching
the `_TOOLS` table at `src/kiro_adapters/mcp_server.py:75-107`:

```
kirocrew_status, kirocrew_agents, kirocrew_ask, kirocrew_spawn,
kirocrew_spawn_list, kirocrew_run_spec, kirocrew_dashboard_url,
kirocode_status, kirocode_ask, kiro_dispatch,
agent_sessions_list, agent_session_get, agent_session_history, agent_sources
```

The last four are the inventory tools: `agent_sessions_list`,
`agent_session_get`, `agent_session_history`, `agent_sources`.

**`jcode-bridge-mcp` does NOT carry them.** Measured:

```
$ ~/bin/jcode-bridge-mcp --help
jcode-bridge-mcp: MCP stdio server exposing jcode as a task backend.
Tools: jcode_health, jcode_run, jcode_task_start, jcode_task_status,
       jcode_task_output, jcode_task_cancel, jcode_task_list,
       jcode_api_health, jcode_api_ask, jcode_api_sessions
EXIT=0
```

Ten `jcode_*` tools, no `agent_*` inventory tools. `jcode-bridge` is a
task-backend bridge; `kiro-adapter-mcp` is the inventory carrier.

### The 10 servers in `~/.kiro/crew/mcp.json` and their commands

| Server | Command | Args |
|---|---|---|
| `jcode-bridge` | `/Users/kooshapari/bin/jcode-bridge-mcp` | — |
| `chrome-devtools` | `npx` | `-y chrome-devtools-mcp@latest` |
| `cloudflare-bindings` | (remote, `url` + `disabled`) | — |
| `cloudflare-builds` | (remote, `url` + `disabled`) | — |
| `cloudflare-docs` | (remote, `url` + `disabled`) | — |
| `cloudflare-observability` | (remote, `url` + `disabled`) | — |
| `octocode` | `npx` | `-y octocode-mcp` |
| `phenotype-portal` | (remote, `url` + `disabled`) | — |
| `workos` | (remote, `url` + `disabled`) | — |
| `workos-docs` | `npx` | `-y @workos/mcp-docs-server` |

**`kiro-adapter-mcp` is absent from this file.** That is the whole reason the
inventory tools are not reachable through KiroCrew.

## 5. Are the 14 inventory tools reachable through KiroCrew today?

**No.** Two independent facts, and the quarantine is only the lesser one:

1. **Not registered.** KiroCrew reads `~/.kiro/crew/mcp.json` (and the agent
   config materialized from it). `kiro-adapter-mcp` appears in neither. The
   tools are simply not offered to a KiroCrew session.
2. **Not probed.** Even after registration, KiroCrew's discovery probe cannot
   succeed on this host: macOS has no OS-level sandbox backend, so the probe
   fails with `mcp_probe_sandbox_unavailable` and the server lands in the
   quarantine with `fails: 3`. That is the fault that produced the
   `jcode-bridge` record.

Note the ordering of the two: because the quarantine gates only the probe and
not the mount, a registered server would still be launchable in a chat
session even while its dashboard row reads `outdated` with 0 tools. So the
quarantine is **not** the blocker; the missing registration is.

### The exact change required

Add one entry to `~/.kiro/crew/mcp.json`:

```json
"kiro": {
  "command": "/Users/kooshapari/bin/kiro-adapter-mcp",
  "args": [],
  "type": "stdio"
}
```

`type: stdio` matches the shape of every other local entry in that file (for
example `jcode-bridge`).

**Not done in this pass.** This changes which servers a live agent may spawn,
which deserves the operator's explicit go-ahead. `~/.kiro` was not modified.

Two caveats that travel with the change:

1. **The dashboard badge will stay stale** until the probe can run unsandboxed
   (`agent.sandbox_allow_unsandboxed_exec=true`, which is currently unset) or
   until the new server's record is cleared by name. This is cosmetic for
   reachability, but it will make the panel look broken.
2. **The quarantine outlives a gateway restart by design**
   (`mcp_discovery.py:45-49`: "the quarantine is re-read process memory while
   the quarantine is durable"). Clearing requires the owner-authenticated
   `POST /api/mcp/quarantine/clear` with that server's `name` — one server per
   call.

## Live proof the tools work today

Called through the `kiro` MCP server (the JCode-side registration), unmodified:

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

## What is NOT claimed

- Not claimed: the `kirocrew-*` servers were fixed. Their errors were `timeout`,
  which is a different fault from the sandbox-probe error, and it is not
  established that the timeout is caused by the quarantine rather than by the
  managed server itself.
- Not claimed: adding the server to `mcp.json` is safe by inspection. It changes
  what a live agent can spawn.
- Not claimed: the quarantined servers are broken. For `jcode-bridge` that is
  affirmatively false — the probe error says the server may be fine, and
  `--help` exits 0.
- Not claimed: any authenticated dashboard response was inspected. The status
  API was probed **without** credentials only; no token was read or used.

## Evidence index

| Source | Location | What it establishes |
|---|---|---|
| Installed package | `.../backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew/` | all `file:line` citations above |
| `mcp_quarantine.py` | `:398`, `:401-402`, `:424`, `:439`, `:463-472` | record shape, crossed-at write, failing predicate, `clear()`, `snapshot()` |
| `mcp_quarantine.py` | `:106-116` | `threshold()` reads `agent.mcp_quarantine_after_failures` live |
| `mcp_discovery.py` | `:53-84`, `:178-198` | `_spawn_excluded()` and the skip-the-spawn consumer |
| `mcp_discovery.py` | `:45-49`, `:74-81` | durability note and the operator-facing warning |
| `mcp_cleanup.py` | `:58` | `mcp_entry_is_muted` — absent `disabled` is not a mute |
| `dashboard/routes/agent_config.py` | `:75-88` | MCP route table and methods |
| `dashboard/handlers/mcp_probe.py` | `:407-432`, `:564-593` | `api_mcp_servers`, `api_mcp_active` |
| `dashboard/handlers/mcp_probe.py` | `:842-905` | `api_mcp_quarantine_clear` semantics |
| `agent.py` | `:1351-1496`, `:2516` | `_MANAGED_MCP_SERVERS`, `emit_managed_servers` call |
| `agent_materialization/managed_mcp.py` | `:272-297`, `:643-661` | emission eligibility (opt_in / spec_gate) and managed-server emission into `mcpServers` |
| `mcp_discovery_managed.py` | `:22-34` | `_MANAGED_SERVER_SUBCOMMANDS`, `_MANAGED_SERVER_NAMES` |
| `~/.kiro/crew/config.json` | `agent.mcp_quarantine_after_failures = 3` | the threshold in force on this host |
| `~/.kiro/crew/mcp-quarantine.json` | all | 6 records, `fails: 3`, `crossed_at = 1793314799.1781309` |
| `~/.kiro/crew/mcp.json` | all | the 10 configured servers |
| `~/.kiro/agents/kirocrew.json` | all | 15 materialized entries, no `disabled` on quarantined names |
| `~/.jcode/mcp.json` | `servers.kiro` | the `kiro-adapter-mcp` registration |
| `~/bin/kiro-adapter-mcp --selftest` | exit 0 | the 14 tool descriptors |
| `~/bin/jcode-bridge-mcp --help` | exit 0 | 10 `jcode_*` tools, no `agent_*` tools |
| `curl -i localhost:5476/api/mcp` | `403`, `X-Auth-Required: true` | no unauthenticated status API |
| `curl -i localhost:5476/api/mcp/active` | `403`, `X-Auth-Required: true` | no unauthenticated status API |
