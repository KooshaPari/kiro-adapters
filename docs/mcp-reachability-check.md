# MCP Reachability Check — the three new consumer readers, black-box

Date: 2026-11-05 (local). Repo: `~/CodeProjects/Phenotype/repos/kiro-adapters` @ `main`
(`dd73700` at time of run). Read-only probe. No config edits, no services started, no push.

## Why this exists

Unit tests prove the readers *work*. They do not prove a consumer can *reach* them. This repo
already names that failure mode: a capability that is built, tested, and then never surfaced to
anyone. So this check drives the real stdio MCP server with newline-delimited JSON-RPC 2.0 and
records only what the server actually returned.

Two runs, identical input, differing only in `KIRO_INVENTORY_ALLOW_CLI_SOURCES`:

```
printf '%s\n' '<initialize>' '<notifications/initialized>' '<tools/list>' \
  '<tools/call agent_sources>' | /Users/kooshapari/bin/kiro-adapter-mcp --inventory-only
```

- initialize: `protocolVersion 2024-11-05`, `capabilities {}`, `clientInfo {name,version}`
- server responded: `{'name': 'kiro-adapters', 'version': '0.1.0'}`, negotiated `2024-11-05`
- both runs exited 0 with empty stderr

## Result table

| source | flag OFF | flag ON | expected |
|---|---|---|---|
| `orca-fs` | **PRESENT** | **PRESENT** | present by default (file-backed, `KNOWN_SOURCES`) |
| `sharecli-session` | **ABSENT** | **PRESENT** | only with flag (`CLI_SOURCES`) |
| `herdr-pane` | **ABSENT** | **PRESENT** | only with flag (`CLI_SOURCES`) |
| `tools/list` count | **4** | **4** | unchanged; sources are dynamic data, not separate tools |

Tools advertised in both runs: `agent_sessions_list`, `agent_session_get`,
`agent_session_history`, `agent_sources`.

`agent_sources` envelope, both runs: `ok: true`, `code: OK`, `isError: false`.
Detail line moves from `4/4 source store(s) present` (OFF) to `6/6 source store(s) present` (ON).
OFF sources: `jcode-fs`, `kcode-fs`, `kirocrew-ledger`, `orca-fs`.
ON adds exactly: `sharecli-session`, `herdr-pane`.

## Plain answers

1. **Does `orca-fs` appear WITHOUT the flag?** Yes. Present in the OFF run at
   `4/4 source store(s) present`. File-backed, so it needs no flag.
2. **Do `sharecli-session` and `herdr-pane` appear ONLY with the flag?** Yes. Absent in the OFF
   run; both present in the ON run. The gate behaves exactly as designed.
3. **Does `agent_sessions_list`'s description mention the flag?** Yes, verbatim:
   > "List agent sessions detectable on this host (read-only; no process started. CLI-backed
   > sources such as ShareCLI are excluded unless KIRO_INVENTORY_ALLOW_CLI_SOURCES is set on the
   > server)."
   Identical text in both runs, so a consumer can discover the gate from `tools/list` alone.
4. **Is `agent_sources` shaped as expected?** Mostly. `data` = `{count, sources:[...]}` where each
   entry is `{source, harness_kind, path, present}`. That is source names + counts **plus two
   extra fields**: `harness_kind` and a store-root `path`. These are inventory roots
   (`~/.jcode`, `~/.config/herdr`, ...), not session paths and not session contents, so nothing
   sensitive leaked. Note it as a shape deviation from "names + counts only", not a leak.

## Extra evidence: sessions actually flow from the new sources

`agent_sources` proves a source is *registered*. It does not prove sessions come out of it. So
the same surface was driven with `agent_sessions_list`:

| run | sessions returned | by `source` |
|---|---|---|
| flag OFF | 500 | `jcode-fs` 500 |
| flag ON | 500 | `sharecli-session` 40, `herdr-pane` 13, `jcode-fs` 447 |

So the CLI-backed readers contribute real sessions through MCP, not just a registry entry.

## Caveat found (not fixed, per instructions)

`agent_sessions_list` returned **500 rows for both `limit: 500` and `limit: 5000`**, with
`count: 500` and detail `500 session(s) detected`. There is a hard cap at 500 that the requested
`limit` cannot exceed, and the tool has no `source` filter (`inputSchema` exposes only `limit` and
`include_stale`).

Consequence for this check: with ON, 447 + 40 + 13 fills the 500 cap exactly, so `kcode-fs`,
`kirocrew-ledger` and `orca-fs` are **clipped out of the session listing** and unobservable
through this surface at this limit. They remain reachable as *sources*. Whether `orca-fs` holds
sessions is **UNKNOWN from this probe**, not disproven — the cap, not absence, is what hid it.

This is the only discrepancy observed. It is a reporting/observability limit, not a reachability
gap: all three new readers are reachable through the real MCP surface.

## What was NOT done

- no source read to explain the cap (deliberately black-box)
- no config change, no service start, no push
- no session paths, titles, ids, or contents recorded anywhere in this document
