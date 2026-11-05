# ACP attachment containment: can the shim govern the daemon's tools?

Status: **Stage 1 design. Stage 2 verdict: no shim-enforceable slice exists;
full containment requires a daemon change.** No enforcement is faked and the
existing prompt refusal is not weakened.

## 1. The problem, restated from existing source (not re-derived)

- Kiro Crew drives permission with a static derived/narrowed tool allowlist
  plus `session/request_permission` (`docs/KAS-VS-JCODE.md:27`).
- jcode arbitrates a **live per-session interaction lease** via
  `require_interaction_ownership`; its ACP front end answers `-32601` to
  `session/set_mode` (`docs/KAS-VS-JCODE.md:27`, and
  `src/jcode_bridge/acp_shim/server.py:283-291`).
- The shim already fails closed at the boundary:
  `session/prompt` is refused with `-32012` unless `--allow-prompt-forwarding`
  (`server.py:230-257`). The advertised `read-only` mode therefore never
  reaches the daemon; it is a local contract only.

The open question this document answers: **when prompting IS enabled, can the
shim make the client's policy actually govern the daemon's tool execution?**

## 2. What the shim can and cannot observe (source findings)

The shim is a JSON-RPC proxy between an ACP client and the jcode/KCode ACP
front end (`server.py`), linked by `StdioUpstream` (`upstream.py`).

### 2.1 The front end's method surface (inbound, client -> server)

`handle_message` dispatch (`jcode-acp-native/src/cli/acp.rs:350-432`) accepts
exactly: `session/list`, `session/new`, `session/load`, `session/resume`,
`session/prompt`, `session/cancel`, `session/close`, `session/set_config_option`,
`session/set_model`, `session/set_reasoning_effort`.

There is **no** `session/request_permission` handler and no
`session/set_permission_policy` (or equivalent) method. The client cannot push
a tool-allowlist or permission policy into the front end at all.

### 2.2 The front end's notification surface (outbound, server -> client)

The front end emits only `session/update` notifications, in three shapes:

- agent message chunks (`acp.rs:1021-1028`),
- `usage_update` (`acp.rs:1086-1097`),
- `config_option_update` (`acp.rs:1117-1127`).

These are all emitted from `run_prompt` (`acp.rs:1008-1138`). That loop
handles only `Ack`, `Done`, `Interrupted`, `Error`, `TokenUsage`,
`ModelChanged`, and discards every other daemon event with `_ => {}`
(`acp.rs:1131`).

### 2.3 The tool-lifecycle mapper is dead code

`map_event` (`acp.rs:1547-1664`) **does** translate daemon tool events into
`session/update` notifications with `sessionUpdate: "tool_call"` /
`"tool_call_update"`:

- `ToolStart` -> `status: "pending"` (`acp.rs:1551-1560`),
- `ToolExec` -> `status: "in_progress"` (`acp.rs:1579-1596`),
- `ToolDone` -> `status: "completed"` / `"failed"` (`acp.rs:1598-1620`).

Two facts kill this as a containment surface:

1. **It is not wired.** `map_event` is referenced only by its definition and
   one unit test, `event_mapper_maps_tool_lifecycle` (`acp.rs:2065-2089`).
   `run_prompt` never calls it, so the live prompt path emits **no** tool
   notifications to the client.
2. **Even wired, it carries no permission.** The state machine is
   `pending -> in_progress -> completed/failed` with no "awaiting approval"
   state and no `request_permission` notification. By the time the shim could
   see `ToolExec`, the tool has already executed inside the daemon.

### 2.4 Where tool authority actually lives

Tool execution is governed inside the daemon by the per-session ownership
lease (`src/cli/acp/ownership.rs`, `require_interaction_ownership`) and the
daemon's own tool allowlist. Neither is reachable over ACP. The ACP front end
is a *relay* for text, usage and config; it is not a tool-gate.

## 3. Enforcement points

| # | Point | Cost | Provably stops | Leaves open |
|---|---|---|---|---|
| 1 | **Client-side gate** (Kiro Crew's static narrowed allowlist) | Out of this repo; already exists upstream of the shim | Only what Crew's allowlist blocks, and only for Crew's own runtime | Everything the jcode daemon executes; Crew's allowlist is not connected to the jcode session |
| 2 | **Shim translation** (this repo, `server.py` + `upstream.py`) | Already implemented: `session/prompt` refused unless opted in; `session/new`/`session/close` blocked; write-config refused | Prompt submission (the sole entry point that causes tool execution) | Once `--allow-prompt-forwarding` is on, the daemon's tool authority is ungoverned by the shim, because the shim cannot see or veto individual tool calls |
| 3 | **Daemon-side tool allowlist** (jcode/KCode Rust change: add an ACP surface to receive and enforce a client tool policy) | Real Rust change; a new method plus a policy check in the daemon's tool path | Arbitrary tool calls, at the true point of execution | Nothing, if the policy is applied at the daemon tool boundary |
| 4 | **OS sandbox** (seatbelt / sandbox-exec / cgroup around the adopted session) | Heavy, platform-specific, and requires *spawning* a child, which contradicts the shim's "adopt, never spawn" contract | Filesystem / network / syscall access | Does not integrate with jcode's in-process session model; cannot be layered on an already-running daemon without a spawn |

## 4. Recommendation

**The shim's fail-closed prompt boundary (#2) is the only containment this
repository can genuinely enforce today, and it already does.** It is honest
and maximal given the front end's surface: without `--allow-prompt-forwarding`,
no prompt (and therefore no tool) can be initiated through the shim.

**Real containment requires #3, a daemon change.** The front end must be
extended to (a) accept a client tool policy over ACP and (b) emit a
permission-request notification with a real "awaiting approval" state *before*
executing a tool, so a bridge can default-DENY and approve. Until then, any
shim that claims to "bridge permission requests" would be bridging a signal
that does not exist and is therefore fake enforcement.

The recommended concrete daemon change, scoped for a follow-up:

1. Add `session/request_permission` handling to the front end dispatch and a
   corresponding outbound `permission_request` notification gated on a new
   `pending` tool state (not the current `ToolStart -> ToolExec -> ToolDone`
   pipeline).
2. Expose a `toolAllowlist` / permission-policy parameter on `session/load` and
   `session/prompt`, enforced in the daemon's tool path alongside
   `require_interaction_ownership`.
3. Wire `map_event` into `run_prompt` so tool lifecycle is observable.

Only after (1)-(3) land can the shim bridge the permission model described in
`docs/KAS-VS-JCODE.md:27` and translate (not passthrough) Crew's narrowed
allowlist into a jcode policy.

## 5. Stage 2 verdict

**Not implemented, by design.** The candidate slice ("bridge upstream
permission-request notifications to the ACP client and default-DENY tool calls
the client does not approve") is not implementable on the shim:

- The front end emits **no** permission-request notification (§2.2, §2.3).
- The only tool notifications the front end *can* emit (`map_event`) are not
  wired into the live prompt path, and even if they were, they carry no
  approval state — the tool has already run by the time the shim could react.

Implementing the candidate would therefore fake enforcement against a signal
that never arrives, and could only be satisfied by weakening the existing
prompt refusal, which is explicitly forbidden. The honest, correct outcome is
to implement nothing and record the daemon change as the real path (§4).
