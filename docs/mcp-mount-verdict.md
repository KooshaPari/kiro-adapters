# Is `kiro-inventory` mounted in a Kiro Crew session? No.

Status: **answered, negative, reproducible.** 2026-11-05.

## Why the earlier question was the wrong one

Every prior attempt asked "why doesn't the model call the tool?", which cannot be
answered from client-side evidence: Kiro Crew's `crew-log` records `tool/completed`
events but **0 of 4** across all sessions carry a tool name, and `request/configured`
carries only `context_window`/`model`/`provider`/`turn`. Model prose saying "not
configured" is not evidence either.

## The discriminator

Attestation was extended from tool calls to **server start**. A `tool` record proves a
call, which requires the model to choose the tool; a `start` record proves only that the
process ran, which is what "mounted" means.

`_audit_start()` writes one line before any protocol handling, gated on the same
`KIRO_INVENTORY_AUDIT_LOG` env var. Carries `ts`, `event`, `pid`, `mode`, `argv` and
nothing else.

## Experiment and result

```
$ rm -f /tmp/kiro-inventory-audit.jsonl
$ kirocrew chat --agent kirocrew -m "Reply with exactly: OK"
TURN_EXIT=0
$ cat /tmp/kiro-inventory-audit.jsonl
(absent)
```

**No start record.** The server process never ran during that turn. Not "the model
declined" - the host never launched it.

## Causes ruled out, with evidence

| Candidate | Ruled out by |
|---|---|
| Launcher broken under an empty environment | Fixed in `887a79b`; `env -i` handshake returns 4 tools |
| Server binary broken | `codex mcp list` shows it enabled; a direct Codex run wrote `agent_sources, ok=true, result_bytes=527` |
| Entry not in the codex config | `[mcp_servers.kiro-inventory]` present with its env table |
| Composer drops the entry | `@kiro-inventory` is absent from the `mcp_ref_guard` unresolved list, so the ref resolved (`docs/mcp-mount-predicate.md`) |
| Registry ceiling | `agent.mcp_registry_mode = false` |
| `mcp.json` vs spec confusion | Both carry the entry; `includeMcpJson = false` means only the spec path matters, and the spec has it |
| Quarantine | Would show as `mcp_quarantine_after_failures = 3` tripping; the guard log names only the three Crew-only servers |

## What remains

The one untested mechanism on this host is `agent.tool_search = true`
(`tool_search_min_tokens = 50000`): with deferred tool exposure, an MCP server's tools
can be hidden behind a search step that is only triggered by a large tool surface, and a
server may not be spawned until something asks for it. That would explain a mounted-but-
unspawned server exactly.

**Next step, one observation:** capture `session/new.mcpServers` for a turn with
`tool_search` off AND on, and compare. If the array contains the entry in both and the
process still does not spawn, the cause is downstream in `codex-acp`, not in Kiro Crew.

Do not read this document as "the integration is impossible" - it is "not mounted on
this host, with seven causes eliminated".

## Follow-up 2026-11-06: `tool_search` REFUTED

The last untested mechanism was deferred tool exposure
(`agent.tool_search = true`, `tool_search_min_tokens = 50000`). Tested as a controlled
comparison, one variable, on the operator's own config:

| `agent.tool_search` | turn exit | `start` records |
|---|---|---|
| `true` (original) | 0 | **0** |
| `false` | 0 | **0** |

**Refuted.** Deferred exposure is not the cause. The config was restored
unconditionally by an `EXIT/INT/TERM` trap rather than by a trailing step, and the
restore was verified by diffing the `agent` block against the pre-test backup:
`tool_search = True`, no other keys differ.

That is now eight causes eliminated: launcher, server binary, codex config, composer
routing, registry mode, `mcp.json`-vs-spec, quarantine, and tool_search.

### Process note worth keeping

An earlier attempt at this same comparison flipped the key and then abandoned the run,
leaving `tool_search = false` in the operator's live config. It was found by reading the
file, not by any report - the worker's own summary said only that it was "now flipping
that key". Two lessons, both already in this repository's rules and both violated here:

1. A setting restored by a trailing step is not restored; it is restored *unless
   something stops early*. Use a trap.
2. Verify a restore by diffing against the backup, not by trusting the tool that
   changed it.

## Closing state 2026-11-06: why this stops here rather than going further

The mount question is answered (not mounted) and eight causes are eliminated. The one
remaining observation - the actual `session/new.mcpServers` array - now requires
instrumentation that is not mine to add unilaterally, and that is worth stating plainly
rather than leaving the item looking merely unfinished.

**The obvious tap does not work.** The natural place to intercept the ACP traffic is the
binary override, and `~/.kiro/crew/.env` already contains:

```
CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim
```

Yet the live spawn executed the **genuine** `~/.bun/bin/codex-acp`, not the shim. So
pointing that override at a logging wrapper would prove nothing: the override is not on
the path Crew actually uses for this backend. This is the same `codex`-spawn-path
question recorded in `acceptance/codex-spawn-path-trace.md`.

**The remaining options, and why each needs a decision:**

| Option | What it costs |
|---|---|
| Wrap the real `~/.bun/bin/codex-acp` (a symlink to a global npm install) | Modifies a shared toolchain outside this repo; a bad wrapper breaks every Codex use on this machine, not just Crew's |
| Patch the fork to log inbound `session/new` params | A daemon change, i.e. the same class of work as item 2 - needs the same go/no-go |
| Watch `session/new` from the Codex side by enabling its own verbose logging | Unverified that codex-acp logs params at any level; would be one config change plus one turn to find out |

None of these is a read-only observation, which is why the previous eight causes were
worth eliminating first: each was free, and together they moved the item from "unknown"
to "not mounted, and here is exactly where to look next".

**What is NOT blocked by this.** The inventory tool is proven reachable and correct
through the MCP surface with real readers behind it (`docs/mcp-reachability-check.md`),
and it works when a host spawns it - Codex does. The gap is specific to Kiro Crew's
projection, not to the tool.
