# Codex MCP Reachability: kiro-inventory

Date: 2026-11-04
Scope: bounded diagnostic. No config edits, no gateway restart, no Crew chat turns.

## Question

Does the Codex side actually deliver the `kiro-inventory` MCP server and its
env, independent of KiroCrew's ACP projection?

## Config under test

`~/.codex/config.toml`:

- `[mcp_servers.kiro-inventory]` command `/Users/kooshapari/bin/kiro-adapter-mcp`, args `["--inventory-only"]`, `startup_timeout_sec = 30.0`
- `[mcp_servers.kiro-inventory.env]` `KIRO_INVENTORY_AUDIT_LOG = "/tmp/kiro-inventory-audit.jsonl"`

## Method

1. Read `codex exec --help` for the exact non-interactive invocation (no
   guessed flags). Used `codex exec -s read-only --skip-git-repo-check -C <repo> <prompt>`.
2. `rm -f /tmp/kiro-inventory-audit.jsonl` before each run.
3. Run 1 (`-s read-only`, default approval policy) asked the agent to call
   `agent_sources` and reply with only the source count. Wall time 29s.
4. Run 2 (`--dangerously-bypass-approvals-and-sandbox`, to remove the
   non-interactive approval blocker, not to change config) repeated the same
   prompt. Wall time 44s.

Both runs were bounded to 120s via `perl -e 'alarm 120; exec @ARGV'`.

## Observations

Run 1:

```
mcp: kiro-inventory/agent_sources started
mcp: kiro-inventory/agent_sources (failed)
MCP tool call requires approval, but approval policy is never
```

This is decisive for reachability: the MCP server was spawned and the
`agent_sources` tool was enumerated and dispatched. Only the approval gate
stopped execution. Config and env therefore reached the child process.

Run 2 (approvals bypassed):

```
mcp: kiro-inventory/agent_sources started
mcp: kiro-inventory/agent_sources (completed)
codex
3
```

Audit file after run 2 (verbatim):

```
{"ts": "2026-11-04T23:54:27.104044+00:00", "tool": "agent_sources", "ok": true, "result_bytes": 527}
```

The `ok: true` line proves the child received `KIRO_INVENTORY_AUDIT_LOG` and
wrote to it, i.e. the `[mcp_servers.kiro-inventory.env]` table is delivered to
the spawned server, not just the command.

## Verdict

(a) CODEX-SIDE DELIVERY WORKS. An audit line with `tool=agent_sources`,
`ok=true` was produced by a plain non-interactive Codex run. The env audit
path reached the child.

Consequence: the observed gap ("a real KiroCrew turn never reached the
server") is inside KiroCrew's ACP projection, not in Codex's MCP config or
spawn path.

## Notes / follow-ups

- Codex non-interactive runs default to approval policy `never`, which blocks
  MCP tool calls. Any KiroCrew-driven ACP session that inherits a `never`
  approval policy will see tool calls rejected with "MCP tool call requires
  approval". This is a plausible proximate cause of the KiroCrew-side gap and
  should be checked in the ACP projection's approval handling.
- Unrelated noise observed: `rmcp` transport errors for
  `http://127.0.0.1:8765/mcp` and `https://mcp.workos.com/mcp` (other servers,
  channel closed). They did not affect the kiro-inventory result.
- Deprecation warnings in config (`[features.guardianv2].thread_context`,
  `features.transcript_v2`, `shell_zsh_fork`, `unified_exec_zsh_fork`) were
  left untouched per instructions.
