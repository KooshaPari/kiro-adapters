# KAS / KiroCrew ACP vs jcode ACP: capability evidence table

Question: **can KiroCrew attach to an externally started jcode session?**

Every row cites a `file:line` on the build actually installed or checked out.
No cell is inferred from a name.

## Provenance (read this before trusting a cell)

| Source | Exact identity |
|---|---|
| KiroCrew backend | `/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew/` (the `-x64` mirror carries the same files) |
| jcode ACP | `~/CodeProjects/Phenotype/repos/jcode-acp-native` @ `e373d80ed` |
| Not measured | The installed Windows/macOS **binary** capability advertisement. Source text is not a runtime measurement; use `initialize` on the real build to confirm. |

## Evidence table

| Concern | KiroCrew / KAS | jcode ACP | Consequence for adoption |
|---|---|---|---|
| **Session creation** | `session/new` sent by the Crew runtime; constant at `acp/types.py:136`. `acp/runtime_wire.py:90` (`_ONE_AT_A_TIME_METHODS = {session/new, session/load, set_mode}`) serialises creation. | `session/new` -> `handle_session_new` (`src/cli/acp.rs:383`); calls `create_new_session(cwd)`, then **must** pass `register_session(...)` or it returns an error (`acp.rs:448-470`). Returns `{sessionId}` plus `_meta.jcode.interactionController`. | Both sides can create. Creation is not the blocker. |
| **Session listing / discovery** | No `session/list` handling is implemented; the method does not appear in `acp/types.py`'s method block (`:135-255`) and no consumer exists. Crew knows only sessions it created in its own store. | `session/list` -> `discovery::list(params)` (`acp.rs:372`); `src/cli/acp/discovery.rs` (125 lines) reads `~/.jcode/sessions/*.json`, cursor-paged. | Only jcode can enumerate pre-existing sessions. KiroCrew has no inventory of sessions it did not create. |
| **Resume / load of a PRE-EXISTING EXTERNAL session** | `session/load` constant `acp/types.py:175`, consumer `acp/runtime_session_load.py:289` (`self._send_and_await(METHOD_SESSION_LOAD, load_params, timeout=budget)`). The load is sent **over the connection Crew owns** (`acp/client_transport.py:542` treats `session/new`, `session/load`, `session/resume` as init-framed requests), and the payload is rewritten with Crew's own `mcpServers` and hoisted KAS agents before sending (`runtime_session_load.py:274-284`). Resume state is a single field, `providers/acp_lifecycle.py:43` (`self._client._resume_session_id`). | `session/load` -> `handle_session_load(message, true)` (`acp.rs:384`), `session/resume` -> same fn with `replay_history=false` (`acp.rs:385`). Load resolves the id from jcode's own session store, not from a prior `session/new` in this process. `loadSession: true` is asserted at `acp.rs:2052`. | **Asymmetric.** jcode can load a session id it did not create in this process; KiroCrew's load only reaches a child runtime it spawned itself. KiroCrew cannot adopt an arbitrary external jcode process through its own transport. |
| **Prompt submission** | `session/prompt` constant `acp/types.py:139`; `_ONE_AT_A_TIME` excludes it but init budget gating applies. | `session/prompt` -> `handle_session_prompt` (`acp.rs:386`). | Both support it; prompt is not the blocker. |
| **Cancel** | `session/cancel` constant `acp/types.py:140`. | `session/cancel` -> `handle_session_cancel` (`acp.rs:387`); also fired internally by close when a prompt is running. | Both support it. |
| **Close / disconnect semantics** | `session/close` constant `acp/types.py:224`, plus KAS-specific `_kiro/session/delete` (`types.py:196`) and `_kiro.dev/session/terminate` (`types.py:190`). | `session/close` -> `handle_session_close` (`acp.rs:653`): **removes the session from the map**, cancels a running prompt, then `session.stop_pump()` (`acp.rs:665-673`). It is not a transport disconnect. | Connecting two harnesses is fine; calling close is destructive. An observer must drop the connection and let the lease expire instead of sending `session/close`. |
| **Capability negotiation** | Client advertises `ACP_CLIENT_CAPABILITIES` (`acp/types.py:310-312`): `fs.readTextFile: false`, `fs.writeTextFile: false`, `terminal: false`. Comment at `types.py:304-309` states Crew deliberately does not serve agent fs/terminal requests over ACP. | `initialize` stores `clientCapabilities/elicitation/form` (`acp.rs:361-366`); result at `acp.rs:1741-1745` sets `agentCapabilities._meta["jcode.interactionController"] = cfg!(unix)` and `loadSession: true`. Caller may override per request via `_meta.jcode.interactionController` (`acp.rs:467`, `:531`; parsed at `:1699`). | Compatible at the wire level. Mismatch is in semantics: jcode exposes an interaction-controller flag; KiroCrew exposes nothing equivalent and declines fs/terminal. |
| **Permission requests** | `session/request_permission` (`acp/types.py:141`), event `permission_request` (`types.py:110`), mapping table `ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL` (`types.py:55`). Permission surface is prepared before spawn (`agent_sdk/drivers/acp.py:366` `derived_agent_permissions`, `:398` `narrowed_agent_permissions`, `:505` `mirror.session_params(agent, permission_surface_owned=True)`). | Inbound client capability `elicitation/form` is the only negotiated client feature (`acp.rs:361`). Write-ownership of a session is gated by `require_interaction_ownership` (`src/cli/acp/ownership.rs`, 51 lines; RPCs list/claim/renew/respond/cancel in `src/cli/acp/interactions.rs`, 195 lines). | Two different permission models. Crew derives and narrows a static tool allowlist; jcode arbitrates a live per-session interaction lease. Bridging them needs an explicit mapping, not a passthrough. |
| **Process isolation model** | One **OS child process per agent runtime**, wrapped by Crew's sandbox plane: `acp/client_spawn_env.py:50-59` imports `wrap_argv`, `wrap_argv_async`, `cgroup_scope_argv`, `scrub_agent_subprocess_env`, `wrapped_by_crew_sandbox` from `kiro_crew.sandbox`; `kiro_crew/sandbox.py` is the sandbox module. Lifecycle described at `providers/acp_lifecycle.py:62`: "Spawn a runtime, create/resume a session, wrap in ...". | One `jcode acp` process is the JSON-RPC server; **all sessions live in one in-process `self.sessions` map** (`acp.rs:668` `self.sessions.lock().await.remove(&session_id)`). Isolation is logical (per-session ownership lease), not process-level. | A Crew-style adoption would need a sandboxed child; jcode's model assumes the server process is already trusted. Attaching to a *running* jcode daemon crosses that boundary rather than recreating it. |

## Decision

1. **KiroCrew -> external jcode session: not supported by Crew's own code path.** `session/load` is written against a runtime Crew spawned and configured (`runtime_session_load.py:274-289`), and Crew has no `session/list` to discover an id it did not mint.
2. **The direction that does work: adapter-side.** A client launches `jcode acp` and calls `session/load` with the id found on disk (or `session/list` first). jcode supports this directly, and supports observer mode by sending `_meta.jcode.interactionController: false` (`acp.rs:1699`, default in `acp/ownership.rs:5`).
3. **Never send `session/close` from an observer** (`acp.rs:653-673`). Detach by dropping the connection.
4. **Attaching to an already-running jcode process is a separate, unproven case.** jcode's ACP server holds sessions in-process; reaching an existing one means either a broker into that process or a fresh `jcode acp` that loads the id from disk. The former is not implemented in either codebase as of the revisions cited.
5. **Permission and isolation gaps are semantic, not syntactic.** Crew declines `fs`/`terminal` and derives a static permission surface; jcode arbitrates a live interaction lease. Any bridge must translate, not forward.

## What would have to change for native adoption

- KiroCrew: implement `session/list` consumption and allow `session/load` against a transport whose child was not spawned by `acp/client_spawn_env.py` (i.e. an external-endpoint transport, not a sandboxed spawn).
- jcode: nothing required for the observer path; a broker RPC for `list`/`claim` already exists (`acp/interactions.rs`).
- Both: a permission-model mapping. Neither side currently has one.
