# MCP Integration Verification — kiro-inventory via Kiro Crew CLI

Date: 2026-11-04 (local PST) / 2026-11-04T09:08Z
Scope: VERIFY ONLY. No config edits, no gateway restart, no launcher changes, one turn, no retry.

## Turn executed

```
kirocrew chat --agent kirocrew -m "Use the kiro-inventory MCP tool agent_sources ..."
```
(timeout 120s)

- exit code: 0
- duration: 105s
- stdout: 355 bytes, stderr: 2667 bytes (diagnostics only; message bodies never printed here)

## Outcome: (c) NOT PROVEN

No structured `role: "tool"` row was produced for this turn. The designated store
`~/.kiro/crew/sessions/*.jsonl` gained **zero** files from the turn
(`find ~/.kiro/crew/sessions -name '*.jsonl' -newermt '2026-11-04 00:50'` -> 0).

Therefore there is no tool row to evaluate, so `kiro-inventory` / `agent_sources`
execution is **not proven**. Exit code 0 and the model's prose do not count as proof.

## Session file examined

`dashboard_chat-3-1793781501.jsonl` — mtime 2026-11-04 00:40:51 PST.

This is the newest file in the store, but it was **not created by the verification
turn** (it predates it by ~28 minutes). Its contents describe an earlier attempt and
are not evidence about the current fix:

| index | role | meta.done | meta.output shape |
|-------|------|-----------|-------------------|
| 3 | tool | true | short string (len 2), non-error |
| 4 | tool | true | short string (len 2), non-error |
| 5 | tool | true | string len 590, matched error markers |

Rows 3-5 are discovery attempts (tool search, server list, server existence check),
not an `agent_sources` execution. Row 5 returned an error payload.

## Connected-server metadata for this turn

The CLI's own structured/diagnostic projection for the verified turn reported the
Codex backend session as carrying only these servers: `portal-bridge`, `workos-docs`,
`phinbox-mcp`, `node_repl`. `kiro-inventory` was **not** among them.

```
WARNING kiro_crew.acp.mcp_ref_guard: agent-spec tool refs name no MCP server in
Crew's projection for this session: backend='codex' agent='kirocrew'
unresolved=@kirocrew-computer, @kirocrew-guide, @kirocrew-mate mcp_gateway=off
```

Also withheld by the Codex mirror projection: `kirocrew-computer`, `kirocrew-guide`,
`kirocrew-mate`.

Metadata-only read of the turn's newest session record (`grep -c kiro-inventory` on the
pre-existing file) returns 5 mentions, all from the 00:40 turn, none from this one.

## Interpretation (no fix applied, as instructed)

The launcher fix under `env -i` is not what the turn exercised. The failure surface now
observed is upstream of the child process:

1. `kiro-inventory` does not appear in Crew's per-session server projection for
   `backend='codex'`, so the child harness is never handed the server to spawn.
2. The pre-existing session row that does mention the server records an error payload,
   consistent with the earlier broken launch.
3. `mcp_gateway=off` in the guard warning.

So the earlier "HOME: unbound variable" defect may well be fixed at the launcher level,
but the integration is still unproven end to end. A future fix would need to make
`kiro-inventory` survive Crew's Codex projection (allowlist / per-tool restriction /
session-identity handling), and the next verification must show a `role: "tool"` row
with `meta.done: true` and a non-error payload in a session file whose mtime is after
the turn started.

## Method notes

- Only metadata was read: role, meta.done, output type/length/error-marker presence,
  input key names and prefixes. No message bodies, tokens, or secrets were printed.
- Single attempt, no retry, no mutation of any config or process.
