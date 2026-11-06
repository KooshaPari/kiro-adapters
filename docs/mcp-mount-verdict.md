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

## Implementation entry point for the fix (scoped, not applied)

The fork is a workspace, so this is not a one-line change. What is now known:

- `jcode run` (headless) DOES register MCP: `src/cli/commands.rs:2469`
  `registry.register_mcp_tools(None, None, None).await`, guarded by
  `run_command_mcp_enabled()` (`:2536`, default on, `JCODE_RUN_MCP=0` to skip).
- There are only **two** call sites in the whole tree: that one and
  `src/cli/auth_test/choice.rs:299`. **The ACP path has none** - `src/cli/acp.rs` never
  constructs a tool registry at all; it connects to a daemon
  (`ensure_daemon`/`connect_daemon`, `acp.rs:851-870`) and drives a session over the wire.
- The MCP and session-tool wiring therefore lives in a workspace crate, not in `src/cli`.
  Root `src/` is only `bin/`, `cli/`, `herdr.rs`, `lib.rs`, `main.rs`; the workspace has
  `crates/jcode-agent-runtime` and ~10 siblings.

**Next concrete probe, before any edit:** search `crates/jcode-agent-runtime` for the
session tool-registry construction and check whether it calls
`register_mcp_tools`/`McpConfig::load`. Two outcomes, both actionable:

- **It does not** -> the daemon never mounts MCP for wire-driven sessions; add the same
  call the headless path makes. This is the smallest correct fix and it would make the
  already-configured `kiro` server (14 tools, `agent_sources` included) reachable.
- **It does, but only for some session modes** -> find the mode predicate and either widen
  it or document why ACP sessions are excluded.

Either way the change needs a `cargo build` to verify, and this repository is not where it
belongs. Recorded here so the next session starts at the entry point instead of
re-deriving the chain.

### Why this was not applied now

A blind patch in a multi-crate Rust workspace, unbuilt, would be exactly the failure mode
this whole investigation has been documenting: a change that looks correct in the diff and
was never observed to work. The value delivered here is the chain and the entry point, and
they are worth more than an unverified diff.

## ROOT CAUSE FOUND 2026-11-06: a resumed session skips MCP registration

This is the answer, with line numbers. `jcode` registers MCP per **subscribe**, and the
flag differs by which subscribe path runs:

| Site | Path | `register_mcp_tools` |
|---|---|---|
| `crates/jcode-app-core/src/server/client_lifecycle.rs:1655` | takeover / resume, guarded by `client_session_id == target_session_id` | **`false`** |
| `:1694` | ordinary subscribe | `true` |
| `:1730` | ordinary subscribe | `true` |

`handle_subscribe` (`crates/jcode-app-core/src/server/client_session.rs:593`) only calls
`registry.register_mcp_tools_for_dir(...)` when that flag is true (`:810-845`). So:

- a **fresh** jcode session registers MCP and gets the `mcp__*` tools;
- a session reached by **`session/load` / resume** - which is exactly what the adoption
  shim does, and exactly what a Kiro Crew turn does - takes the `false` branch and
  registers **nothing**.

That single asymmetry explains every observation in this file: the array is correct,
`~/.jcode/mcp.json` is correct and serves 14 tools, and the tools are still absent from
the session, with no error anywhere.

### Why this is almost certainly unintentional

The `false` is passed at the one site where `client_session_id` has just been bound to
`target_session_id`. The plausible original reason is **avoidance of double registration**
when taking over a session that is already live and already has MCP. But that reasoning
does not hold for the case we exercise: a persisted session that is being *loaded* by a new
client, with no live owner and therefore no prior registration. The two cases were collapsed
into one flag value.

### Correct fix, and the trap in the obvious version

The naive change - flip `false` to `true` at `:1655` - would double-register MCP tools for a
genuinely live takeover, which is the defect the `false` was presumably avoiding (duplicate
tool names, or a second connection to every MCP server).

The fix must distinguish the two cases at that site:

- **target session is live and already has an owner** -> keep `false` (its registration stands);
- **target session is being loaded with no live owner** -> register, i.e. `true`.

The discriminating condition needs one variable the site does not currently compute - a
"was this target live before I bound to it" check - which is why this is a small change with
a real design decision inside it, not a one-character edit.

### Verification plan for whoever lands it

1. Apply the conditional at `client_lifecycle.rs:1655`.
2. `cargo build` (workspace; expect it to be slow - this is the only honest way to know).
3. Re-run the Crew turn used throughout this file: `kirocrew chat --agent kirocrew -m
   "Call the MCP tool agent_sources ..."` and check for a real payload instead of
   `TOOL_UNAVAILABLE`.
4. Negative control: a live takeover must still show exactly one registration, not two.

Not applied here on purpose: this is a multi-crate Rust change with a design decision and no
build performed. The chain is finished; the edit is not something to guess at.

## Fix applied in the fork, and exactly how far it is verified

`jcode-acp-native` @ `8e2e22ec5` on `fix/acp-session-list-window` (local, unpushed).

```rust
// client_lifecycle.rs, takeover/resume branch
let target_is_live = sessions.read().await.contains_key(&target_session_id);
if crate::session::session_exists(&target_session_id) || target_is_live {
    ...
    handle_subscribe(..., !target_is_live, ...)
```

Verification ladder, with the honest stopping point:

| Rung | Result |
|---|---|
| `cargo check -p jcode-app-core` | **PASS** - exit 0, workspace clean (1m59s) |
| `cargo build --bin jcode` | **PASS** - exit 0, `target/debug/jcode`, 348 MB |
| Binary runs | **PASS** - `version v0.0.0-dev (e373d80ed, dirty)` |
| **Runtime proof of the resume path** | **NOT DONE** |

The last rung needs an ACP client harness: start the patched binary, persist a session,
adopt it with a `session/load`, and observe that a configured MCP server spawns. That is
the same class of harness as the containment work in `docs/ACP-CONTAINMENT-PLAN.md`, and it
is not something to fake with a log line.

Two cheaper observables were checked and are unavailable:

- `handle_subscribe` already logs `register_mcp_tools` under `SESSION_LIFECYCLE`, which
  would be a perfect before/after signal - but today's `~/.jcode/logs/jcode-2026-11-06.log`
  contains no `SESSION_LIFECYCLE` lines at all, so event logging is not enabled in the
  configuration this host runs.
- The attestation route (`KIRO_INVENTORY_AUDIT_LOG` -> a `start` record) works and would
  prove the MCP child spawned, but reaching it needs the same ACP harness.

So: the defect is diagnosed to the line, the fix is committed, type-checks and builds, and
the runtime assertion remains open. Stated plainly because a type-checked patch that was
never executed is precisely the failure mode this file documents.

## RUNTIME PROOF 2026-11-06: the fix is confirmed by A/B

The last rung is done. `acceptance/mcp_resume_harness.py` tests the resume path directly
instead of inferring it: it starts a daemon on a private scratch socket, sends a `subscribe`
whose `target_session_id` names a session that exists **only on disk**, and watches whether a
configured MCP server is actually launched. The observable is the inventory server's own
`start` attestation record.

Both cases used the identical session id, request, socket pattern and audit file; the only
difference is the binary:

| Binary | MCP spawned on resume |
|---|---|
| pinned `~/.jcode/builds/versions/0.99.1-kcode-mode/jcode` | **False** |
| patched `target/debug/jcode` (from `8e2e22ec5`) | **True** |

Record from the patched run:

```json
{"ts": "2026-11-06T23:03:21.341101+00:00", "event": "start", "pid": 30361,
 "mode": "None", "argv": []}
```

That is the `kiro` server from `~/.jcode/mcp.json` (its spec has `args: []`), launched
because the resumed session finally registered MCP.

### What this does and does not prove

- **Proved:** a disk-only resumed session registers MCP only with the patch. The hardcoded
  `false` was the cause, and `!target_is_live` fixes it.
- **Proved:** the live takeover case still passes `false`, because the discriminator is
  `sessions.contains_key(target)` - the protection against double registration is intact.
  (Asserted from the code path, not from a separate takeover run.)
- **Not proved:** that a Kiro Crew turn now surfaces `agent_sources` end to end. That needs
  the patched binary installed over the pinned build, which is a toolchain change. The
  harness is the gate for the code; installing is the remaining operator step.

### Reproducing

```
python3 acceptance/mcp_resume_harness.py     # exit 0 == fix confirmed, 1 == inconclusive, 2 == not confirmed
```

It starts and signals only its own daemons, uses a private socket per case, deletes stale
sockets, and redirects nothing else. `HOME` is deliberately NOT redirected: the resume path
needs the real session store, and it is only read.
