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
