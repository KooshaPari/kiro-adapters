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

## RESOLVED SCOPE 2026-11-06: Kiro Crew is correct; the fault is in codex-acp

The "closing state" section above said the missing observation was the
`session/new.mcpServers` array. **That observation is now made**, and it overturns the
assumption behind it: Kiro Crew produces the right array, so nothing on the Crew side is
at fault.

No instrumentation was needed after all - the installed package's own pure functions can
be called directly with its bundled interpreter:

```
$ "/Applications/KiroCrew Nightly.app/.../kirocrew-backend-arm64/bin/python3" -c \
    "from kiro_crew.acp.session_mcp import session_mcp_servers; \
     print([e['name'] for e in session_mcp_servers('kirocrew')])"
['jcode-bridge', 'kiro-inventory', 'kirocrew-computer', 'kirocrew-core',
 'kirocrew-cron', 'kirocrew-guide', 'kirocrew-mate']
```

and after the codex mirror's narrowing, which is what actually reaches the wire:

```
['jcode-bridge', 'kiro-inventory', 'kirocrew-core', 'kirocrew-cron']
kiro-inventory survived: True
```

The mirror withheld only `kirocrew-computer`, `kirocrew-guide` and `kirocrew-mate`, with
a logged reason (per-tool restriction with no deny channel, or a Crew-bound session
identity). `kiro-inventory` is not in that category and is emitted with the exact shape
intended: command `/Users/kooshapari/bin/kiro-adapter-mcp`, args `["--inventory-only"]`,
type `stdio`.

### What this eliminates and what it leaves

Nine causes are now excluded: the eight configuration causes listed earlier, **plus
KiroCrew's own projection, composer and mirror**. The array is correct.

**The defect is therefore inside `codex-acp`.** It receives four well-formed stdio
entries and launches none of them, while the servers the agent actually reported
(`node_repl`, `workos-docs`, `phinbox-mcp`, `portal-bridge`) are exactly the ones in
`~/.codex/config.toml`. The consistent reading is that `codex-acp` builds its MCP set from
its own config file and does not mount the per-session array - but that is now a
*testable claim about codex-acp*, not a guess about Crew.

### Next step, now cheap and non-invasive

`codex-acp` is a plain Node package (`~/.bun/install/global/node_modules/@agentclientprotocol/codex-acp/dist/index.js`),
not an opaque binary. Two options, neither of which touches Crew:

1. Read its `session/new` handler for what it does with `mcpServers` - a source read, no
   execution, no config change.
2. Set `CODEX_HOME` to a scratch directory containing a config with a marker server, and
   observe whether a Crew turn mounts the array entry, the config entry, or both. This
   finally settles merge-vs-replace.

Option 1 first: it is free and decisive if the handler is readable.

### Correction to the earlier section

The "three remaining options" table above (wrap the real binary, patch the fork, enable
Codex verbose logging) was written on the assumption that the payload was unknown. It is
known now, so the cheapest path is a source read of `codex-acp`, and none of the three
invasive options is required to reach a conclusion.

## FINAL 2026-11-06: complete causal chain, with the fix located in our own fork

Three observations, each measured, close this out.

**1. What the codex slot actually spawns.** With `acp_backend = "codex"`, a live Crew turn
spawns:

```
/Users/kooshapari/.jcode/builds/versions/0.99.1-kcode-mode/jcode acp -p opencode-go -m deepseek-v4.1-flash
```

That is **jcode**, reached through `CODEX_ACP_BIN` -> our shim -> `jcode acp`. It is not
codex-acp. Supporting detail: `~/.bun/bin/codex-acp` is a **broken symlink** - it points at
`@agentclientprotocol/codex-acp/dist/index.js` and that package directory does not exist.
So the earlier conclusion that "the fault is in codex-acp" was wrong twice over: not only
is codex-acp not on this path, it is not installed at all.

**2. Kiro Crew's side is provably correct.** Calling the installed package's own functions
shows the composer produces 7 entries and the codex mirror narrows them to
`['jcode-bridge', 'kiro-inventory', 'kirocrew-core', 'kirocrew-cron']` - `kiro-inventory`
survives with the exact command, args and type intended.

**3. `jcode acp` drops the array.** `src/cli/acp.rs:1776` `validate_acp_mcp_servers` only
*validates* that `mcpServers` is an array, and the accompanying test is named
`non_empty_mcp_servers_are_tolerated_until_session_scoped_mcp_is_supported()`. Tolerated,
not implemented.

### The chain, stated once

```
KiroCrew composer  -> correct array (verified by direct call)
KiroCrew mirror    -> keeps kiro-inventory (verified)
session/new        -> contains it (verified)
CODEX_ACP_BIN      -> our shim -> jcode acp   (verified by observing the live spawn)
jcode acp          -> validates mcpServers, then IGNORES it (source + test name)
                   => server never spawned, no tool, no error, nothing in any log
```

### Why the inventory tools still are not reaching the agent, and the fix

`jcode` has its own native MCP config at `~/.jcode/mcp.json`, and it already declares a
`kiro` server (the same binary) which `jcode mcp test kiro` confirms exposes **14 tools
including `agent_sources`**. That is the normal route for a jcode session - but it is not
reaching this ACP session either, which is consistent with the ACP adapter not wiring MCP
at all rather than wiring it from the wrong file.

**So the fix belongs in `jcode-acp-native`, not in any Kiro Crew config.** Two candidate
shapes, in order of size:

1. Make `jcode acp` mount the servers from its own `mcp.json` for the session. Smallest
   change that makes inventory reachable, because the config already exists and is proven
   to serve the tools.
2. Implement session-scoped MCP properly: honour `session/new.mcpServers`, which is what
   the existing test name already anticipates.

Option 1 is the right first step: it needs no protocol work and it makes an existing,
tested server reachable from the backend that is actually in use.

### Corrections this file has had to make

Three, each caught by measuring instead of trusting the previous conclusion: the doc said
`map_event` was unwired (it is wired, in the concurrent pump); then said the fault was in
codex-acp (it is not on the path and is not installed); and this section replaces the
"three invasive options" table that assumed the payload was unknown. The pattern worth
keeping: every wrong turn here came from reasoning about a component instead of observing
which one actually ran.
