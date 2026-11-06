# Consumer Capability Matrix — "mapped" → "measured"

Date: 2026-11-05 (local). Scope: read-only discovery. No builds, no config changes, no services started.

> **No integration code exists yet for any of the nine products.** Nothing in this document is an
> integration claim. Each row reports only what a bounded local probe observed: whether an install or
> implementation is present, what attach surface it advertises, and the exact evidence line. Where a
> probe was bounded, absence is recorded as *unproven*, never as "does not exist".

## Search envelope (what "bounded" means here)

Searched only: `~/CodeProjects/Phenotype/repos` (listing), `~/CodeProjects` (top level listing),
`/Applications` (name filter), `~/.config/*` (listing), `~/Library/Application Support` (name filter),
and `command -v` over the nine names plus obvious aliases. No recursive walk of `$HOME`, no disk-wide
`find`, no server started, no log tailed beyond ~6 lines, no socket connected.

## Matrix

| product | installed? (path) | surface type | evidence (file:line or `--help` line) | class | smallest useful next step |
|---|---|---|---|---|---|
| **HERDR** | yes — `/Users/kooshapari/.local/bin/herdr`; runtime `~/.config/herdr/` | Unix socket + structured CLI over it (JSON snapshot) | `herdr --help` → `herdr api <subcommand> ...`; `herdr api --help` → `snapshot  Print the live session snapshot`; socket present at `~/.config/herdr/herdr.sock` (`srw-------`, owns `herdr-client.sock` too) | **ATTACHABLE** (`herdr api snapshot`) | Parse `herdr api snapshot` JSON into the broker's session-inventory shape; treat enrollment as herdr-managed-only unless a probe proves otherwise. |
| **Tracera** | yes — `/Users/kooshapari/.local/bin/tracera`; app `/Applications/Tracera.app`; src `~/CodeProjects/Tracera` | Container-stack CLI (up/down/status/logs/doctor) + HTTP health endpoints + local frontend | `tracera --help` → `status  Show container status`, `logs  Tail logs from one or all services`, `doctor  Health probe — exits 0 if /health and /ready both return ok`; `Tracera/api/honest-fallback.ts:7` (`/health`, `/healthz`), `:11` (`/ready`, `/readyz`, `/api/v1/health`) | **PARTIAL** (health + container status + logs; a bounded `rg -l -i "agent.?session" Tracera/src` returned zero files, so no agent-session record was found — unproven, not disproven) | Read `tracera status` + `/api/v1/health` as *product* liveness only; do not present Tracera as an agent-session reader until a session record is found. |
| **AgilePlus** | yes — `/Users/kooshapari/.local/bin/agileplus`; state `~/Library/Application Support/AgilePlus/state/core.db` (+`-wal`, `-shm`) | SQLite state DB + read-only CLI verbs (list/dashboard/cockpit/dag) | `agileplus --help` → `dashboard  Render an in-flight DAG / status dashboard from SQLite`, `cockpit  Publish or read local cockpit scorecards`, `dag  Manage the DAG of work packages (pick, claim, release, dedup, scan)`; DB on disk at `.../state/core.db` | **PARTIAL** (durable work-package/DAG state, readable; it is delivery state, not live agent-session state) | `agileplus dashboard` / `cockpit` read as *acceptance* signals; keep it out of the session-inventory lane. |
| **ShareCLI** | yes — `/Users/kooshapari/.cargo/bin/sharecli`; config `~/.config/sharecli/session-shell.zsh`; repo `~/CodeProjects/sharecli` (only `audit-sessions/`, no source tree) | Durable session ledger + CLI with explicit `session list/inspect/observe/watch` + JSON status | `sharecli --help` → `session  Inspect durable agent sessions`; `sharecli session --help` → `list  List persisted sessions`, `inspect  Inspect one persisted session`, `observations  List append-only observations`, `watch  Continuously snapshot Ghostty surfaces into the durable ledger`; `sharecli status --help` → `--json  Emit machine-readable JSON (includes detected agent inventory)` | **ATTACHABLE** (`sharecli session list` / `sharecli status --json`) | Consume `sharecli status --json` (agent inventory) and `sharecli session list`; confirm the ledger path and record shapes with one `--json` call. |
| **Khostty** | repo yes — `~/CodeProjects/Phenotype/repos/khostty` (+`khostty-wtrees`); binary no (`command -v khostty` → NONE); terminal present `/Applications/Ghostty.app` 1.3.1 | Terminal surface + file-write actions; no session CLI | `ghostty +list-actions` (filtered) → `write_scrollback_file`, `write_screen_file`, `write_selection_file`; no `session`/`status`/`list` action matched the same filter | **PARTIAL** (display/scrollback output only; per standing policy pane write is display output, not agent input) | Treat Khostty as a *display* sink (scrollback file), never as a control surface; if a real surface is needed, build it into the adapter, not the pane. |
| **Paperclip** | yes — `~/.paperclip/cli/current` (+`install.json`, `installs/`), instance `~/.paperclip/instances/default`, embedded PostgreSQL | HTTP API (auth-gated) + embedded DB; currently stopped | `~/.paperclip/run-restart.log` (tail) → `WARN: GET /api/health 403`, then `INFO graceful heartbeat run drain complete {"signal":"SIGTERM"...}`, `INFO Stopping embedded PostgreSQL`; no `*.sock` under `~/.paperclip` (maxdepth 3) | **PARTIAL** (an HTTP API exists, but it is 403-gated and the process is currently down; root cause of the stop is UNKNOWN — do not restart it as a diagnostic) | Capture the next startup's error text plus the postgres log **before** any restart; treat cause as unknown, not as a stale lock. |
| **Orca** | app present — `~/Library/Application Support/orca` (agent-hooks, blob_storage, `agent-session-authority.key`, `codex-*` trust files); binary not on PATH | Local HTTP agent-hook endpoint with explicit port + token + transport | `.../orca/agent-hooks/endpoint.env:1` `ORCA_AGENT_HOOK_PORT=62042`, `:3` `ORCA_AGENT_HOOK_TRANSPORT=raw-json-v1`, `:5` `ORCA_AGENT_HOOK_ENV=production`; sibling `last-status.json` | **ATTACHABLE** (`POST http://127.0.0.1:62042`, `raw-json-v1`) | Read `last-status.json` first (free), then a single read-only hook call to learn the envelope; never print the token value. |
| **OmniRoute** | yes — `/opt/homebrew/bin/omniroute`; `/Applications/OmniRoute.app`; state `~/.config/omniroute*` (`storage.sqlite`, `server.env`, `logs/`) | Router HTTP server + CLI with `sessions`, `status`, `usage`, `telemetry` | `omniroute --help` → `sessions  Inspect and manage active sessions`, `status  Show OmniRoute status dashboard`, `usage  Usage analytics, budgets, quotas, and logs`, `serve  Start the OmniRoute server`; state `~/.config/omniroute*/storage.sqlite` | **PARTIAL** (sessions here are routing/LLM request sessions, not harness agent sessions; it is routing/usage authority, not inventory) | Use `omniroute status` + `usage` as *routing/usage* telemetry only; do not count its "sessions" as agent sessions. |
| **Discord** | desktop app data — `~/Library/Application Support/discord`; bridge present `~/.config/discord-bridge/` (`bridge.json`, `bridge.sock`, `bridge.lock`, `inbox/`=2, `processed/`, `wake-sessions.json`, `watchdog-state.json`) | Local bridge daemon: Unix socket + socket-ish CLI/daemon surface; multi-bot routing config | `bridge.json` keys: `guildId, defaultAgent, allowedUserIds, routes, server, agents, watchedChannels, noMentionDefault, aliases, feedback, postChannels, wake`; `agents` = 4 bot identities each with `tokenEnv` (e.g. `{'id':'Instinct','tokenEnv':'DISCORD_BOT_TOKEN_INSTINCT'}`); `watchedChannels` = 2 channel IDs; socket `bridge.sock` (`srw-------`, mtime 2026-11-04 16:13) | **ATTACHABLE** as *notification sink + inbound command surface* — **NOT** an agent runtime | Read `wake-sessions.json` / `watchdog-state.json` to learn the bridge's own session model, then post one read-only status message. |

## Discord: notification sink vs agent runtime

The two must not be conflated. What exists here is **"Discord as a notification sink" plus an inbound
command surface that can wake work** — not "Discord as an agent runtime".

Evidence for the sink reading: `postChannels` and `feedback` keys (outbound targets), `watchedChannels`
(2 IDs the bridge listens on), `noMentionDefault` and `aliases` (message-routing granularity), and a
4-bot `agents` list whose entries only carry `{id, tokenEnv}` — a token envelope, not a process handle.

Evidence for the wake reading: a `wake` config key and a `wake-sessions.json` state file, plus
`watchdog-state.json` and a `bridge.sock`, consistent with a daemon that reacts to messages by
waking sessions it already knows about.

Evidence *against* the runtime reading: `routes` is empty (`[]`), no per-agent process/PTY/pid record
appears in the config, and the bridge owns no session store of its own beyond `wake-sessions.json`.
Plausible classification from what exists: **Discord = comms + wake trigger**, with agent execution
living in whatever harness the bridge wakes. Any claim that Discord *runs* agents is unsupported here.

## Top three to build first

1. **ShareCLI** — strongest evidence, least effort. It already ships `session list` / `session inspect`
   and a `status --json` that explicitly advertises "detected agent inventory", so a consumer needs no
   shim, no parse of unstructured logs, and no privileged hook: one JSON call per poll.
2. **HERDR** — strong evidence, low effort. `herdr api snapshot` is a documented structured verb over a
   socket that is already live on this machine; the only catch is that herdr's inventory covers agents
   herdr itself enrolled, so the consumer must not read it as universal.
3. **Orca** — strong evidence, small surface. Port, token, transport (`raw-json-v1`) and env are all
   written down in `agent-hooks/endpoint.env` plus a `last-status.json` already on disk, so the first
   useful read costs one file read and at most one read-only hook call.

Runner-up worth naming: **Paperclip** has an HTTP API, but it is 403-gated and currently stopped for an
unknown reason, so it is the first *diagnostic* task, not the first *integration* task.

## Explicit non-claims

- No adapter, bridge, broker, or consumer integration was written or modified for any of the nine.
- "ATTACHABLE" means a surface is documented and reachable in principle. It does not mean this session
  called it, authenticated to it, or verified a response shape.
- Nothing here reclassifies any product's authority: Tracera = product, AgilePlus = acceptance,
  Paperclip = presentation, Discord = comms, HERDR = host session/terminal ops, ShareCLI =
  discovery/recovery, OmniRoute = routing/usage.

## 2026-11-05 — settling the two PARTIAL rows (Tracera, Paperclip)

Scope: read-only. No service started, restarted, stopped, killed, or authenticated; no token,
username, or message content printed. Searches bounded to `~/CodeProjects/Tracera/{api,apps,crates,src}`
and `~/.paperclip/{cli/current/node_modules,instances/default/logs}`. This section supersedes the
`class` and `smallest useful next step` cells for these two rows only.

| product | new evidence (file:line / log line) | updated class | smallest useful next step |
|---|---|---|---|
| **Tracera** | The only session-shaped routes are stubs: `crates/tracera-server/src/router.rs:327-333` registers `/api/v1/projects/{project_id}/codex/sessions` and `.../sessions/{session_id}` as `any(not_implemented)`. `crates/tracera-server/src/routes/codex.rs:1-2` states outright: *"No corresponding domain repository methods or SQLite tables exist."* `not_implemented.rs:19-28` returns `501` with header `x-tracera-not-implemented: true`. Every other `session_id` hit is a synthetic benchmark fixture under `crates/tracera-server/testdata/` (e.g. `agentora-mlx-benchmark-run.json:1`, envelopes `ses_*`/`run_*`/`att_*`). A repo-indexed search for `sessions` tables across `alembic/` and `crates/tracera-server/src` matched `router.rs` only. | **PARTIAL → closed negative on agent-session state.** Proven: Tracera persists no record naming a running agent session. It remains a product-liveness surface (`status`/`logs`/`doctor`/`/health`/`/ready`). | Remove Tracera from the session-inventory lane entirely; keep it as product liveness + log tail only. Do not re-open unless `routes/codex.rs` grows a repository + migration. |
| **Paperclip** | (a) It is **running**, contrary to the row above: `instances/default/runtime-info.json` → `pid 89217`, `127.0.0.1:3180`, `startedAt 2026-11-04T01:24:41Z`; confirmed by `lsof -nP -iTCP:3180 -sTCP:LISTEN` (node, pid 89217) and `GET /health 200` at 17:04:46 in `logs/service.log`. (b) Stop cause is recorded: `logs/service.log` last shutdown block → `graceful heartbeat run drain complete {"signal":"SIGTERM","drain":{"interrupted":0,...}}` then `plugin job scheduler stopped` / `Stopping embedded PostgreSQL {"signal":"SIGTERM"}` — an **external SIGTERM**, graceful, zero interrupted runs, followed by a fresh startup banner. Preceding DB instability is separately logged as postgres `FATAL` "postmaster has commanded this server process to roll back ... another server process exited abnormally and possibly corrupted shared memory" (`service.log:15399-15498`, `:535807`). (c) The 403 is auth gating, not failure: `GET /api/health 403` appears alongside `GET /health 200` in the same interval. (d) Session-shaped endpoints do exist in the bundled route table (`cli/current/node_modules`): `/agents`, `/agents/:id`, `/agents/:id/runtime-state`, `/agents/:id/runtime-state/reset-session`, `/agents/:id/task-sessions`, `/agents/:id/heartbeat/invoke`, `/agents/:id/pause`, `/agents/:id/terminate`. | **PARTIAL (upgraded specificity): live process + a session-adjacent API namespace, but nothing called.** The named state is agent runtime/config state (`runtime-state`, `task-sessions`), not a harness transcript. Stop cause is now **known**: external SIGTERM, graceful. | One read-only authenticated `GET /agents` and `GET /agents/:id/runtime-state` to record the actual field shape; until then do not treat `task-sessions` as harness session inventory. Cause-of-stop needs no further work. |

Ranking: **unchanged.** Tracera exiting the session lane only removes a non-candidate. Paperclip's
upgrade is real but does not promote it: it is still auth-gated, its endpoint shapes are unverified
(no adapter may be written against a guessed schema), and its `task-sessions` records agent task
state rather than harness session state. ShareCLI / HERDR / Orca stay 1-2-3. The runner-up sentence
above should now read: Paperclip is **live and reachable**, but session-shaped means authenticated
shape discovery first — still an integration-later, not integration-first.

Explicit non-claims (unchanged and extended): no adapter, bridge, broker, or consumer integration
was written or modified; no Paperclip endpoint was called in this pass; "session-shaped endpoint"
means a route string was observed in a bundled artifact, not that a response was ever read.


## 2026-11-06 — settling the last two rows (AgilePlus, Khostty)

Scope: read-only discovery. No service started, no config changed, no build run. Searches bounded to
`~/CodeProjects/Phenotype/repos/{AgilePlus,khostty}` (top-level listing + targeted greps), `/Applications`
(name filter), `~/.config` (listing), and `command -v`. Absence is recorded as *unproven*, never as
"does not exist".

| product | installed? (path) | surface | evidence (file:line or `--help` line) | class | smallest useful next step |
|---|---|---|---|---|---|
| **AgilePlus** | yes — `/Users/kooshapari/.local/bin/agileplus`; source `~/CodeProjects/Phenotype/repos/AgilePlus` (Rust workspace + `agileplus-mcp/` Python) | Spec/feature-management + work-package DAG engine. Exposes a read-only state DB (`~/Library/Application Support/AgilePlus/state/core.db`) and CLI verbs (`dashboard`, `cockpit`, `dag`, `list`, `implement`). `implement` *dispatches* agents, but nothing observes them. | `agileplus --help` → `implement  Implement work packages (dispatches agents)`; `agileplus-mcp/mcp-tools.json` describes only spec/cycle/queue/dashboard tools (no session/agent tool); `crates/agileplus-api/src/routes` grep for `session` returned zero hits; `crates/agileplus-grpc/src` grep for `session` returned zero hits; `crates/agileplus-domain/src/config/agents.rs` is config only (`default_agent`, `max_subagents`), no session store. | **NOT AN AGENT-SESSION CONSUMER.** Tracks work items an agent might report into; does not run or observe agent sessions. | Leave the target list. AgilePlus is acceptance authority, not session inventory. If an agent-session link is ever wanted, it would be a write-side integration (agent reports a WP state), not a read-side attach. |
| **Khostty** | source yes — `~/CodeProjects/Phenotype/repos/khostty` (+`khostty-wtrees`); binary no (`command -v khostty` → NONE); terminal present `/Applications/Ghostty.app` 1.3.1 | Terminal emulator + in-process VT library. Documented agent protocol v1 is **not reachable**: no server listens. | `docs/AGENT.md` §1: pane create/write/focus/query are "In-process only today"; §4.2: `src/apprt/ipc/protocol.md` specifies 13 commands (`pane.*`, `events.*`) but `server.zig` (no accept loop), `app_host.zig` (no app-backed host), and runtime wiring are all missing; "Do not build against this yet." `ghostty +list-actions` filtered for `session`/`agent`/`list` returned no matches (only `write_scrollback_file`, `write_screen_file`, `write_selection_file`). No `khostty` config under `~/.config` or cache under `~/Library/Caches`. | **NO AGENT-SESSION SURFACE.** A pane is not an agent session; the only reachable agent-facing capability is in-process terminal observation (`libghostty-vt`), which is a display/VT surface, not session inventory. | Leave the target list. Revisit only when `server.zig` + `app_host.zig` + runtime wiring land and a socket actually listens (then classify as terminal/pane surface, still not agent-session inventory). |

### Conclusion

Both products should **leave the target list** rather than remain `PARTIAL`:

- **AgilePlus** is a spec/feature-management and acceptance tool. It *dispatches* agents
  (`implement`) and tracks work packages, but persists no agent-session record and exposes no
  session/agent endpoint (MCP tools, gRPC, and API routes all confirmed session-free). It is not
  an agent-session consumer; it is acceptance authority.
- **Khostty** is a terminal emulator. Its only agent-facing capability today is in-process VT
  observation; the documented IPC agent protocol has no running server. A terminal pane is not an
  agent session, so conflating them would be exactly the overclaim this matrix exists to prevent.

Neither should be counted among the session-inventory consumers (ShareCLI, HERDR, Orca remain the
top three). AgilePlus and Khostty stay in the matrix as rows for traceability but are marked
non-consumers, not PARTIAL.

Explicit non-claims (extended): no Khostty IPC socket was connected (none exists to connect to);
no AgilePlus endpoint beyond `--help` and on-disk source was exercised; "no session tool" means no
such tool in `mcp-tools.json` and no such route in the searched crates, not a proof of absence
everywhere in the repo.
