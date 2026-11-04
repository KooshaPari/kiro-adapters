# kiro-inventory MCP integration result

Date: 2026-11-04
Agent spec: `~/.kiro/agents/kirocrew.json` (session template `kirocrew`, codex ACP backend)
MCP server: `/Users/kooshapari/bin/kiro-adapter-mcp --inventory-only`

Outcome: **NOT PROVEN.** The spec change was applied and verified, but one bounded
Kiro Crew CLI turn still did not expose `kiro-inventory` tools to the agent.

## 1. Change applied (only `~/.kiro/agents/kirocrew.json`)

Backup: `~/.kiro/agents/kirocrew.json.bak-20261104T005250` (mode preserved, `-rw-------`).
Write path: edit on a sibling temp copy, `python3 -m json.tool` validation, then
same-directory `mv` (atomic rename). Target mode after install: `-rw-------` (unchanged).

Exact diff (backup -> installed):

```diff
@@ -20,7 +20,8 @@ tools
     "@kirocrew-mate",
-    "@jcode-bridge"
+    "@jcode-bridge",
+    "@kiro-inventory"
   ],
@@ -58,7 +59,8 @@ allowedTools
     "@kirocrew-cron",
-    "@jcode-bridge"
+    "@jcode-bridge",
+    "@kiro-inventory"
   ],
@@ -156,6 +158,13 @@ mcpServers
       "command": "/Users/kooshapari/bin/jcode-bridge-mcp",
       "type": "stdio"
+    },
+    "kiro-inventory": {
+      "args": [
+        "--inventory-only"
+      ],
+      "command": "/Users/kooshapari/bin/kiro-adapter-mcp",
+      "type": "stdio"
     }
   }
 }
```

Shape chosen by inspection, not by guess: the file already declares
`"@server"` refs in both `tools` and `allowedTools` for every mounted MCP
server (`@jcode-bridge`, `@kirocrew-cron`, ...), so `@kiro-inventory` was
added to both. The `mcpServers` entry copies the `jcode-bridge` stdio shape
exactly (`args`, `command`, `type: "stdio"`), differing only in command and
the `--inventory-only` arg, which `src/kiro_adapters/mcp_server.py`
(`--inventory-only`, line 481) accepts.

Post-install verification: re-read of the file diffs against the backup with
exactly the three hunks above and nothing else; JSON parses; no other spec
touched; `~/.kiro/crew/mcp.json`, quarantine, sandbox settings and the
gateway were not modified and no restart was performed.

## 2. Bounded live turn

Command (documented CLI, single non-interactive message, ~34s wall):

```
kirocrew chat --agent kirocrew -m "List the agent sources you can see using the kiro-inventory tools. Keep it to one short call."
```

Exit code: **0** (pipe status of `kirocrew` itself).

Observation from the turn: the agent answered that `kiro-inventory` tools are
not exposed in its session, and enumerated only harness-side servers
(`portal-bridge`, `workos-docs`, `node_repl`, `phinbox-mcp`) plus two
configured-but-disconnected entries. No inventory call was attempted.

Crew logged, for the same session:

```
kiro_crew.acp.mcp_ref_guard: agent-spec tool refs name no MCP server in Crew's
projection for this session: backend='codex' agent='kirocrew'
unresolved=@kirocrew-computer, @kirocrew-guide, @kirocrew-mate mcp_gateway=off
```

`@kiro-inventory` is **not** in that unresolved list, which is weak,
ambiguous positive evidence (the guard may simply not classify an unknown
name). `gateway.log` contains zero mentions of `kiro-inventory`.

## 3. Structured session record

No new `*.jsonl` file appeared under `~/.kiro/crew/sessions/` and none was
modified during the turn (newest file predates the run by ~13 minutes). The
CLI `chat` path therefore did not emit a session record to inspect.

- Tool row for `kiro-inventory` / `agent_sources`: **not observed** (no record).
- `meta.done`: **n/a**.
- `meta.output` success payload vs error string: **n/a**.

The two pre-existing `*.jsonl` files that mention `kiro-inventory` are from
00:35/00:40, i.e. before this change, and are not evidence for it.

## 4. Statement

The integration is **NOT proven** for a Kiro Crew agent turn. The spec entry is
installed and valid, but the session still did not receive the server. No
retry was made and no further config change was attempted, per scope: a fix
beyond this single spec entry (for example a projection/mirror in
`src/kiro_crew/providers/mirrors/`, or resolving why Crew's codex projection
delivers none of the spec's MCP servers at `mcp_gateway=off`) would require a
restart or a config change outside the approved step 2.
