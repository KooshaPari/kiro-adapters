# MCP server-side attestation: does `agent_sources` actually execute?

Date: 2026-11-04 (~01:21-01:30 local, UTC-08:00)
Repo: `~/CodeProjects/Phenotype/repos/kiro-adapters` (branch `main`)

## Why this exists

Client-side evidence is exhausted. KiroCrew's crew-log records `tool/completed`
events, but 0 of 4 across all sessions carry a tool name, so no client record
can attribute a call to `agent_sources`. The only remaining attribution point is
the server process itself, which is the one place that knows a tool body ran.

## Verdict

**(c) NOT PROVEN.** The opt-in audit path works end to end when the server is
invoked (verified locally), and the config change is in place, but the single
bounded real turn produced **no audit file at all**. Per the pre-agreed decision
rule, no audit lines means the server never received a call in that turn.

## Part 1 - attestation in the server

`src/kiro_adapters/mcp_server.py`:

* `AUDIT_LOG_ENV = "KIRO_INVENTORY_AUDIT_LOG"`.
* `_audit_tool_call(name, result)` reads the env var, and if it is unset, empty
  or whitespace, returns immediately: no file, no stat, no work. Behaviour with
  the var unset is unchanged.
* When set, it appends exactly one line:
  `{"ts": <UTC ISO8601>, "tool": <name>, "ok": <bool>, "result_bytes": <int>}`.
  Nothing else is recorded: no arguments, no result payload, no paths, no user
  data. There is deliberately no debug mode that logs arguments.
* The whole append is wrapped in `try/except Exception: pass`, so an unwritable
  path drops the record and never disturbs the tool response.
* `call_tool` was split into `_execute_tool` (resolution + dispatch) plus
  `_audit_tool_call`, so every invocation path - success, adapter failure,
  rejected tool, unknown tool - is attested once. Handler semantics and the
  JSON-RPC surface are otherwise identical.

`result_bytes` is the UTF-8 length of the serialized `AdapterResult`, i.e. the
same bytes the client would receive as tool text. It proves a real payload was
produced without recording the payload.

## Part 2 - tests

New file `tests/test_mcp_audit_attestation.py` (handler stubbed, so no real
home directory is read):

| test | asserts |
|---|---|
| `test_unset_env_writes_nothing` | env unset -> no file created, call still ok |
| `test_empty_env_writes_nothing` | whitespace-only value -> no file |
| `test_one_line_per_call_with_four_redacted_keys` | 2 calls -> 2 lines, key set is exactly `{ts, tool, ok, result_bytes}`, and an argument value (`SECRET-ARGUMENT-VALUE`) never appears in the file |
| `test_tools_call_over_json_rpc_is_attested` | the `tools/call` path attests once |
| `test_unwritable_path_does_not_break_the_response` | path under a nonexistent directory -> result still ok, no directory created |

Full suite: `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
-> `Ran 478 tests ... OK`.

## Part 3 - env under codex

Codex spawns MCP children with an empty environment, so the env var is supplied
by config rather than by inheritance.

* Backup: `~/.codex/config.toml.bak-20261104-012550-attestation`
  (`cp -p`, so mode `-rw-------` preserved).
* Change: append the child table, since `[mcp_servers.kiro-inventory]` is the
  final table in the file.
  ```toml
  [mcp_servers.kiro-inventory.env]
  KIRO_INVENTORY_AUDIT_LOG = "/tmp/kiro-inventory-audit.jsonl"
  ```
* Written atomically: temp file in the same directory, mode copied from the
  original, then `mv` over the target. Final mode is still `-rw-------`.
* Validation with `/opt/homebrew/bin/python3.12` + `tomllib`: parses; after
  removing only the new `env` key, the parsed document is exactly equal to the
  backup. Same 19 tables before and after.
* `codex mcp list | grep kiro-inventory` shows the server enabled with
  `KIRO_INVENTORY_AUDIT_LOG=*****`, confirming codex sees the env entry.

## Part 4 - the real turn

Stale file deleted, then one bounded turn:

```
kirocrew chat --agent kirocrew -m "Call the kiro-inventory tool agent_sources and say only whether it succeeded and how many sources came back."
```

Observed:

* Turn start 01:26:55; wall clock stopped at 2m30s (the 150s bound), so the turn
  was cut before it could report. Gateway log shows the session being projected
  (`PID 13535`, backend `codex`, `mcp_gateway=off`) and no tool line after it.
* `/tmp/kiro-inventory-audit.jsonl`: **does not exist**. No lines to report.
* Only unrelated warnings were emitted (cgroup not enforced on macOS, Playwright
  CLI not resolvable, three Crew-only servers withheld from the codex transport).

Plumbing control, run separately so it cannot be confused with the turn
(environment set explicitly, no codex, no KiroCrew):

```
printf '{"jsonrpc":"2.0","id":1,...initialize...}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"agent_sources","arguments":{}}}\n' \
  | KIRO_INVENTORY_AUDIT_LOG=/tmp/kiro-inventory-selftest.jsonl \
    /Users/kooshapari/bin/kiro-adapter-mcp --inventory-only
```

produced exactly:

```json
{"ts": "2026-11-04T09:29:26.387971+00:00", "tool": "agent_sources", "ok": true, "result_bytes": 527}
```

So the server code, the launcher path and the env var all work. What is missing
is a turn in which codex actually issues `tools/call` for `agent_sources`, or in
which the turn is allowed to finish.

## Next step (not done here: one turn only, no launcher/gateway changes)

Re-run the same single request with a longer bound (the 150s cap fired first) and
then read `/tmp/kiro-inventory-audit.jsonl`. A line with `"tool":
"agent_sources"` is server-side proof; lines for other tools mean the model
chose differently; no file means the call never crossed to the server and the
remaining suspect is the codex transport's env handling for this child
(`mcp_gateway=off`, Crew withholds servers it cannot bind).

## Commits

* `5d167bd` feat(mcp): opt-in redacted tool-invocation attestation (code + tests)
* this document: see `git log -1 -- docs/mcp-server-attestation-proof.md`
