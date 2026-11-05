# KiroCrew turn loop: does a real Crew turn execute `agent_sources`?

Date: 2026-11-04
Scope: bounded read-only probe. No config edits, no launcher edits, no gateway restart.

## Method

- Deleted `/tmp/kiro-inventory-audit.jsonl` once at the start. The file is appended by the
  `kiro-inventory` MCP server on every tool call and records only `{ts, tool, ok, result_bytes}`.
- Ran a bounded loop of Kiro Crew turns via
  `kirocrew chat --agent kirocrew-guest -m "<prompt>"`, each capped at 120 s.
- Prompt varied only in insistence; nothing else was changed between turns.

| Turn | Prompt escalation | Audit line appeared? |
|---|---|---|
| 1 | Plain request: use the kiro-inventory MCP server, call `agent_sources`, report the count | No |
| 2 | First enumerate the kiro-inventory tools by name, then answer | No |
| 3 | Declare the `agent_sources` call a required step; reply count only or `CALL_FAILED` | No |
| 4 | Name the tool explicitly as `mcp__kiro-inventory__agent_sources`; fall back to any name containing `agent_sources` | No |
| 5 | Enumerate every available tool name, then call any agent-sources-related tool | No |

Turns attempted: 5. Turns that produced an audit line: 0.

## Audit lines

None. `/tmp/kiro-inventory-audit.jsonl` never came into existence during the loop, so the
server received zero calls across all five turns.

## Tool names the model claimed (names only)

Turn 1 referenced `kiro-inventory` four times and `agent_sources` once in its text, with no
`mcp__` tool identifier.

Turn 5 enumerated its available tool surface explicitly. All 43 `mcp__` identifiers it listed
belong to four other servers:

| Server prefix | Tools claimed |
|---|---|
| `portal_bridge` | 34 |
| `node_repl` | 4 |
| `workos_docs` | 4 |
| `phinbox_mcp` | 1 |

No `mcp__kiro-inventory__*` identifier appeared in that enumeration. This is a claim about the
session's advertised tool list, not an independent measurement of the mount.

## Verdict

**NOT PROVEN** for `agent_sources`.

- Case (c) applies: no audit line appeared across 5 turns of escalating insistence. The
  `kiro-inventory` server never received a call.
- The Codex path demonstrably works on the same host: a direct codex invocation wrote
  `{"tool":"agent_sources","ok":true,"result_bytes":527}` to the same audit file.
- The Crew turn's own tool enumeration contained 43 `mcp__` names across four servers and none
  from `kiro-inventory`. That is consistent with the server not being present in the Crew
  session's mounted set, but the turn's self-report is not proof of the mount state, and
  absence of a call remains distinct from absence of a tool.

Per the hard rules of this probe, no configuration or launcher change was made to test that
hypothesis. The follow-up (out of scope here) is a server-side check of which MCP servers the
Crew agent session actually mounts, rather than relying on model-reported tool lists.
