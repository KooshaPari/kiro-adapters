# Kiro Crew — Codex Backend ACP Handshake Sequence

**Package under analysis (read-only, installed):**

```
/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/
  lib/python3.12/site-packages/kiro_crew
```

All citations below are `file:line` against that installed tree. Every line cited was read
in this session. Where a question could not be answered from source, it is marked **UNKNOWN**
rather than inferred.

---

## One-line answer

Kiro Crew's codex backend calls **`session/load` first** (when a resume session id is known and
the backend advertises `loadSession`), and **falls back to `session/new`** when no resume id is
present or the load fails. It **can** be pointed at a pre-existing session — but only through
Crew's own internal `resume_sid` plumbing (`AcpClient.set_resume_session_id`), **not** through
any agent-config key or environment variable in the `kiro_crew` package.

---

## 1. After `initialize`, does the codex driver call `session/new` or `session/load`?

**Both, in that order of preference. `session/load` is tried first; `session/new` is the fallback.**

The handshake driver is `AcpClient._initialize_session`:

- `kiro_crew/acp/client_start.py:762` — docstring: `"""Handshake: initialize → session/load or session/new → set_mode → set_model."""`
- `kiro_crew/acp/client_start.py:765` — `init_id = await self._send_request(METHOD_INITIALIZE, self._initialize_params())`
- `kiro_crew/acp/client_start.py:766` — `init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)`

### The `session/load` attempt (taken first, when a resume id exists)

- `kiro_crew/acp/client_start.py:827` — `resume_sid = self._resume_session_id`
- `kiro_crew/acp/client_start.py:836` — `if resume_sid and self._can_load_session:`
- `kiro_crew/acp/client_start.py:872-876` — restore verb selection:
  ```python
  restore_method = (
      METHOD_SESSION_RESUME
      if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD
      else METHOD_SESSION_LOAD
  )
  ```
  Codex is **not** in `ACP_BACKENDS_RESUME_WITHOUT_LOAD` — that set is
  `frozenset({ACP_BACKEND_DEEPSEEK})` at `kiro_crew/agent_sdk/backends_sessions.py:301`.
  So codex takes the `METHOD_SESSION_LOAD` branch.
- `kiro_crew/acp/client_start.py:919` — **exact call site**: `load_id = await self._send_request(restore_method, load_params)`
- `kiro_crew/acp/client_start.py:878-880` — load params: `{"sessionId": resume_sid, "cwd": ..., "mcpServers": [...]}`
- `kiro_crew/acp/client_start.py:933-935` — success test and assignment:
  ```python
  if "modes" in load_resp or self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES:
      self._session_id = resume_sid
      self._resumed = True
  ```
  Codex is **not** in `ACP_BACKENDS_LOAD_WITHOUT_MODES` — that set is
  `frozenset({ACP_BACKEND_OPENCODE, ACP_BACKEND_DEEPSEEK})` at
  `kiro_crew/agent_sdk/backends_sessions.py:265`. So codex requires a literal `modes` block in
  the load response, otherwise the load is treated as not-taken.
- `kiro_crew/acp/client_start.py:944-949` — load failure is swallowed and falls through:
  ```python
  except (AcpError, AcpTimeoutError):
      logger.info(
          "%s failed for %s, falling back to session/new",
          restore_method,
          resume_sid,
      )
  ```

### The `session/new` fallback

- `kiro_crew/acp/client_start.py:974` — `if not self._session_id:`
- `kiro_crew/acp/client_start.py:980` — `session_resp = await self._new_session_following_substitution()`
- `kiro_crew/acp/client_start.py:982` — `self._session_id = session_resp.get("sessionId")`
- `kiro_crew/acp/client_start.py:573-574` — `new_params` begins: `{"cwd": await self._session_work_dir(), ...}`
- `kiro_crew/acp/client_start.py:625` — **exact call site**: `session_id = await self._send_request(METHOD_SESSION_NEW, new_params)`
- `kiro_crew/acp/client_start.py:626-631` — `await self._wait_for_response(session_id, timeout=_INIT_TIMEOUT, method=METHOD_SESSION_NEW, ...)`

`METHOD_SESSION_NEW` is `"session/new"` and `METHOD_SESSION_LOAD` is `"session/load"`:
`kiro_crew/acp/types.py` (constants block; `METHOD_SET_CONFIG_OPTION = "session/set_config_option"`
at `kiro_crew/acp/types.py:255`).

### The second, shared-runtime path (`AcpProvider`)

Kiro Crew also drives codex through `AcpRuntime`, which mirrors the same decision:

- `kiro_crew/providers/acp_runtime_start.py:600` —
  `if self.is_kas_backend or self._client.backend in (ACP_BACKENDS_HARNESS_OWNED_SESSIONS):`
- `kiro_crew/providers/acp_runtime_start.py:616-617` — for such a harness: `session_file = None` / `should_load = True`
  (codex is a member of `ACP_BACKENDS_HARNESS_OWNED_SESSIONS`, see §6)
- `kiro_crew/acp/runtime_session_load.py:289` — **exact call site**:
  `resp = await self._send_and_await(METHOD_SESSION_LOAD, load_params, timeout=budget)`
- `kiro_crew/acp/runtime_session_load.py:293-294` — success test:
  ```python
  if "modes" not in resp:
      raise AcpRuntimeError(f"session/load did not resume session {resume_sid}: {resp}")
  ```
- `kiro_crew/acp/runtime_session_load.py:216` — `load_params` begins: `{"sessionId": resume_sid, "cwd": session_work_dir, "mcpServers": wire_servers}`
- `kiro_crew/acp/runtime_session_create.py:395` — the `session/new` path:
  `resp = await self._send_and_await(METHOD_SESSION_NEW, params, timeout=budget)`
- `kiro_crew/acp/runtime_session_create.py:400` —
  `raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")`

- `kiro_crew/acp/runtime_session_load.py:66-70` — the `load_session` docstring states the intent:
  *"this issues session/load DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId"*.

---

## 2. Does it read the backend's advertised capabilities before choosing? What if `loadSession` is false?

**Yes. Capabilities are parsed from the `initialize` response and gate the load attempt.**

- `kiro_crew/acp/client_start.py:776` — `capabilities = init_resp.get("agentCapabilities") or {}`
- `kiro_crew/acp/client_start.py:778-783` — the capability read:
  ```python
  if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD:
      session_capabilities = capabilities.get("sessionCapabilities")
      self._can_load_session = isinstance(session_capabilities, dict) and isinstance(
          session_capabilities.get("resume"), dict
      )
  else:
      self._can_load_session = bool(capabilities.get("loadSession", False))
  ```
  Codex is not in `ACP_BACKENDS_RESUME_WITHOUT_LOAD`, so for codex the flag read is
  `agentCapabilities.loadSession`.

**What it does if `loadSession` is false:** `_can_load_session` stays `False`, so the guard at
`kiro_crew/acp/client_start.py:836` (`if resume_sid and self._can_load_session:`) is **false and
the `session/load` attempt is skipped entirely**. Execution falls through to
`kiro_crew/acp/client_start.py:974` (`if not self._session_id:`) and issues `session/new`.

The comment at `kiro_crew/acp/client_start.py:836-844` explains the *other* gate in the same
condition: for a harness NOT in `ACP_BACKENDS_HARNESS_OWNED_SESSIONS`, Crew additionally requires
the kiro transcript file to exist on disk (`kiro_crew/acp/client_start.py:857-858`:
`session_file = str(kiro_sessions_dir() / f"{resume_sid}.json")` / `file_ok = Path(session_file).exists()`),
otherwise it logs at `kiro_crew/acp/client_start.py:968`
(`"Session file missing for %s, skipping load"`) and goes to `session/new`. Codex **is** in
`ACP_BACKENDS_HARNESS_OWNED_SESSIONS`, so it is exempted from the disk check
(`kiro_crew/acp/client_start.py:845-855`).

### Runtime path equivalent

- `kiro_crew/acp/runtime_spawn_launch.py:939` —
  `self._can_load_session = bool(self._agent_capabilities.get("loadSession", False))`
- `kiro_crew/acp/runtime_session_load.py:94-95` — the refusal:
  ```python
  if not self._can_load_session:
      raise AcpRuntimeError("Backend does not advertise session/load support")
  ```

### Session modes

Mode ids are read off the session-open response, not the `initialize` response:

- `kiro_crew/acp/client_class.py:441` (comment) — *"Mode ids the backend advertised at session init (session/new|load …)"*
- `kiro_crew/acp/client_class.py:445` — `self._available_mode_ids: list[str] = []` (advertised mode ids)

---

## 3. Does it ever WRITE a session mode, and via what request/argument?

**Yes — but NOT via `session/set_mode` for codex.** There are two distinct writes:

### (a) `session/set_mode` with `modeId` — **kiro-family only, codex excluded**

- `kiro_crew/acp/client_start.py:1078` — `if self._is_kiro:` (the gate)
- `kiro_crew/acp/client_start.py:1085` — the modes guard: `if not self._modes_advertised or self._agent in self._available_mode_ids:`
- `kiro_crew/acp/client_start.py:1092-1095` — **exact call site**:
  ```python
  await self._send_request(
      METHOD_SET_MODE,
      {"sessionId": self._session_id, "modeId": self._agent},
  )
  ```
- `kiro_crew/acp/_dispatch.py:186` — payload builder:
  `return {"sessionId": session_id, "modeId": agent}`
- `kiro_crew/acp/types.py:138` — `METHOD_SET_MODE = "session/set_mode"`

The argument name is **`modeId`**. The comment at `kiro_crew/acp/client_start.py:1066-1069`
explains why codex is excluded: *"claude-agent-acp does handle session/set_mode, but its modeIds
are permission modes (default, acceptEdits, plan, …), never an agent name"*.

### (b) `session/set_config_option` with `configId`/`value` — **this is codex's mode write**

Codex's enforceable boundary is written as a session config option, not a mode:

- `kiro_crew/acp/harness/codex.py:64-67` —
  *"codex-acp's default ``agent`` mode writes inside the workspace without asking. Its ACP v1
  ``mode`` selector is the enforceable boundary, written over ``session/set_config_option``"*
  (the section header is at `kiro_crew/acp/harness/codex.py:63`)
- `kiro_crew/agent_sdk/backends_routing.py:358` — `ACP_BACKEND_CODEX: Routing.SESSION_CONFIG,`
- `kiro_crew/agent_sdk/backends_routing.py:391-393` — **the exact option/value**:
  ```python
  ACP_BACKEND_PERMISSION_CONFIG: dict = {
      ACP_BACKEND_CODEX: ("mode", "read-only"),
  }
  ```
- `kiro_crew/acp/client_start.py:1135-1136` — the gate and call:
  ```python
  if acp_tool_gate.routing_for(self.backend) is acp_tool_gate.Routing.SESSION_CONFIG:
      await self._apply_session_permission_routing()
  ```
- `kiro_crew/acp/client_permissions.py:121` — `async def _apply_session_permission_routing(self: AcpClient) -> None:`
- `kiro_crew/acp/client_permissions.py:137-138` — `backend = self.backend` /
  `option_id, value = acp_tool_gate.permission_config_for(backend)`
- `kiro_crew/acp/client_permissions.py:139-140` — `issue = acp_tool_gate.session_config_issue(backend, self._acp_config_options)`
- `kiro_crew/acp/client_permissions.py:156` — **exact call site**: `await self.set_config_option(option_id, value)`
- `kiro_crew/acp/session_handle_model_selection.py:519-521` — the wire payload:
  ```python
  req_id = await self._send_awaited(
      METHOD_SET_CONFIG_OPTION,
      {"sessionId": self._session_id, "configId": config_id, "value": value},
  )
  ```
- `kiro_crew/acp/types.py:255` — `METHOD_SET_CONFIG_OPTION = "session/set_config_option"`

**So for codex the answer to "mode=read-only" is: YES, it writes `mode=read-only`, via
`session/set_config_option` with argument names `configId="mode"` and `value="read-only"`.**
It is NOT written via `session/set_mode` / `modeId`.

**Failure handling of that write:** `kiro_crew/acp/client_permissions.py:144-149` — if the option was
**not advertised**, the verdict is **INDETERMINATE** (`acp_tool_gate.enforce_runtime_routing(...,
verdict=acp_tool_gate.Verdict.INDETERMINATE, ...)`), per the comment at
`kiro_crew/acp/client_permissions.py:141-143`: *"Not advertised: INDETERMINATE, never BYPASSED."*
A rejected write is recorded as an observed **BYPASSED** verdict. Neither verdict is silently
ignored.

---

## 4. What does the driver do with the returned session id? Can it be told to load a pre-existing id?

### What it does with the returned id

On the load path (`kiro_crew/acp/client_start.py:934`): `self._session_id = resume_sid` — the id is
the **caller-supplied** id, not one returned by the backend.

On the new path (`kiro_crew/acp/client_start.py:982`): `self._session_id = session_resp.get("sessionId")`
— the id returned by the backend.

The id is then used as the `sessionId` argument on every subsequent session-scoped request:
`session/set_mode` (`kiro_crew/acp/client_start.py:1094`), `session/set_config_option`
(`kiro_crew/acp/session_handle_model_selection.py:521`), and `session/prompt`.

### Can it be pointed at a pre-existing session?

**Yes — via the internal `resume_sid` / `_resume_session_id` plumbing.** The setter:

- `kiro_crew/acp/client_lifecycle.py:144-146`:
  ```python
  def set_resume_session_id(self: AcpClient, sid: str) -> None:
      """Set a kiro-cli session ID to restore via session/load on next ensure_ready()."""
      self._resume_session_id = sid
  ```
- `kiro_crew/acp/session_provider.py:1509-1511` — the provider-level twin:
  ```python
  def set_resume_session_id(self, sid: str) -> None:
      """Store a session ID for future resume via session/load."""
      self._resume_session_id = sid
  ```

Where the id is threaded through:

- `kiro_crew/session_allocation_parts/get_or_create.py:558` — `resume_sid: str | None = None`
- `kiro_crew/session_allocation_parts/get_or_create.py:568` — `resume_sid = owner._session_map.get(key)`
  — **the id comes from Crew's own internal session map, keyed by session key**, not from config.
- `kiro_crew/session_allocation_parts/get_or_create.py:889` —
  `cast(Any, provider).client.set_resume_session_id(resume_sid)`
- `kiro_crew/session_allocation_parts/get_or_create.py:892` —
  `cast(Any, provider).set_resume_session_id(resume_sid)`

**Config key: NONE.** The `kiro_crew` package exposes no agent-config key and no environment
variable that names an existing session to adopt. The only env var in the codex spawn path is
`CODEX_ACP_BIN` (`kiro_crew/acp/client_spawn_env.py:103` as `_ENV_CODEX_ACP_BIN`, applied at
`kiro_crew/acp/client_spawn_env.py:238-239`), which names the **binary**, not a session.
Grepping `kiro_crew/config/sections_agent.py` for "session" yields only prose in field
descriptions and unrelated fields such as `session_sharing`
(`kiro_crew/config/sections_agent.py:550`) — there is **no** `resume_sid`, `session_id`, or
`existing_session` field on `AgentConfig` (`class AgentConfig` at
`kiro_crew/config/sections_agent.py:73`).

The `resume_sid` is also **consumed once** — `kiro_crew/acp/client_start.py:828`:
`self._resume_session_id = None  # consume — no retry loop`.

---

## 5. CRITICAL: if the backend is an adopter that cannot do `session/new`, what error path fires?

There are **three** distinct failure surfaces, depending on which layer fails.

### (a) `session/new` returns an error frame → the JSON-RPC error becomes an `AcpError`

- `kiro_crew/acp/client_transport.py:508` —
  `raise AcpError(f"JSON-RPC error: {_err_log}", code=_jsonrpc_error_code(msg.error))`

This is raised inside `_wait_for_response` (`kiro_crew/acp/client_transport.py:413`), which is
what `kiro_crew/acp/client_start.py:627` awaits for the `session/new` reply. The exception
propagates out of `_new_session_following_substitution` and out of `_initialize_session`.

### (b) `session/new` succeeds but returns no `sessionId` → explicit, actionable error

- `kiro_crew/acp/client_start.py:996-1018`:
  ```python
  if not self._session_id:
      # Both the initial attempt and the substitution retry failed to
      # yield a session. Raise a clear, actionable error instead of
      # letting the next step die on the opaque "Cannot set config
      # option before session is initialized" guard.
      ...
      raise AcpError(
          "session/new returned no sessionId"
          + (...)
          + "; the backend did not create a session."
      )
  ```
  The message string is at `kiro_crew/acp/client_start.py:1011`; the rationale comment is at
  `kiro_crew/acp/client_start.py:997-1000`.

- Runtime equivalent: `kiro_crew/acp/runtime_session_create.py:400` —
  `raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")`

### (c) The exception is caught by `ensure_ready`, which tears down the child and re-raises

- `kiro_crew/acp/client_start.py:1248` — `except (AcpTimeoutError, AcpError, OSError) as exc:`
- `kiro_crew/acp/client_start.py:1266`, `1281`, `1290`, `1310` — `await self._cleanup_failed_live_spawn()`
- `kiro_crew/acp/client_start.py:1052-1058` — the `DerivedSpecStale` case is likewise converted to
  `AcpError` so it reaches this same handler: *"that is the type ``ensure_ready`` handles, and its
  handler is what ends the child and drops the half-registered session state"*.

**So for an adopter that cannot do `session/new`: the `session/new` error frame becomes an
`AcpError`; if it returns a response with no `sessionId`, the driver raises
`AcpError("session/new returned no sessionId; the backend did not create a session.")`;
`ensure_ready` catches it, calls `_cleanup_failed_live_spawn()` and re-raises.**

### Note on the adopter case specifically

If the backend is an **adopter** (like the `jcode-acp-shim`, which refuses to create a session
and only forwards `session/load`), then:

- the `session/load` path is reachable **only** if a `resume_sid` is present
  (`kiro_crew/acp/client_start.py:836`) **and** the backend advertises
  `agentCapabilities.loadSession` (`kiro_crew/acp/client_start.py:783`);
- with no `resume_sid`, the driver goes straight to `session/new`
  (`kiro_crew/acp/client_start.py:974`), which the adopter cannot serve → path (a) or (b) above.

The `session/load` attempt itself would also need codex's `modes`-block success signal
(`kiro_crew/acp/client_start.py:933`) — an adopter's `session/load` response would have to carry
a `modes` block for Crew to accept the restore.

---

## 6. Is there an agent config key that names an existing session to resume/adopt?

**NO such key exists in the `kiro_crew` package.**

Grep results for `resume_sid`, `resume_session_id`, `existing_session`, `session_id` across
`kiro_crew/config/`, `kiro_crew/config/sections_agent.py`, and the agent-spec modules returned
**no agent-config field** naming a session. The only matches are:

| Symbol | Location | What it is |
|---|---|---|
| `_resume_session_id` | `kiro_crew/acp/client_class.py:410` (init to `None`) | in-memory client field |
| `set_resume_session_id` | `kiro_crew/acp/client_lifecycle.py:144` | client setter |
| `set_resume_session_id` | `kiro_crew/acp/session_provider.py:1509` | provider setter |
| `resume_sid` (local) | `kiro_crew/session_allocation_parts/get_or_create.py:558, 568, 889, 892` | derived from Crew's internal `_session_map` |
| `resume_sid` (local) | `kiro_crew/acp/client_start.py:827` | read once, then cleared at `:828` |
| `resume_sid` (local) | `kiro_crew/acp/runtime_session_load.py:216` | the `sessionId` sent on load |
| `CODEX_ACP_BIN` | `kiro_crew/acp/client_spawn_env.py:103` (env name), `:238-239` (applied) | names the **binary**, not a session |

`AgentConfig` is defined at `kiro_crew/config/sections_agent.py:73`. Its fields include
`approval_mode`, `streaming`, `model`, `role_models`, `role_efforts`, `model_context_tokens`,
`session_sharing` (`kiro_crew/config/sections_agent.py:550`), and `acp_backend` (read at
`kiro_crew/config/loader.py:513`) — **none of which names a session to adopt**.

**Conclusion for §6:** the pre-existing-session entry point is a **runtime API call**
(`set_resume_session_id(sid)`), fed by Crew's own session map keyed by session key
(`kiro_crew/session_allocation_parts/get_or_create.py:568`). There is **no declarative config
key** and **no environment variable** by which an operator can point Kiro Crew at an
arbitrary pre-existing ACP session id.

---

## Appendix — capability-set membership (codex)

| Set | Definition | Is codex a member? |
|---|---|---|
| `ACP_BACKENDS_HARNESS_OWNED_SESSIONS` | `kiro_crew/agent_sdk/backends_sessions.py:235-244` — `{CLAUDE, CODEX, OPENCODE, PI, GOOSE, DEEPSEEK}` | **YES** |
| `ACP_BACKENDS_LOAD_WITHOUT_MODES` | `kiro_crew/agent_sdk/backends_sessions.py:265` — `{OPENCODE, DEEPSEEK}` | NO — codex's `session/load` must return a `modes` block |
| `ACP_BACKENDS_RESUME_WITHOUT_LOAD` | `kiro_crew/agent_sdk/backends_sessions.py:301` — `{DEEPSEEK}` | NO — codex uses `session/load`, not `session/resume` |
| `ACP_BACKEND_ROUTING[CODEX]` | `kiro_crew/agent_sdk/backends_routing.py:358` — `Routing.SESSION_CONFIG` | YES — drives the `mode=read-only` write |
| `ACP_BACKEND_PERMISSION_CONFIG[CODEX]` | `kiro_crew/agent_sdk/backends_routing.py:391-393` — `("mode", "read-only")` | YES |

---

## Method sequence, condensed (codex backend, `AcpClient` path)

```
initialize                                   client_start.py:764
  └─ parse agentCapabilities.loadSession      client_start.py:783
if resume_sid AND _can_load_session:          client_start.py:836
  └─ session/load {sessionId, cwd, mcpServers}   client_start.py:919
     └─ success iff "modes" in response       client_start.py:933
     └─ on error: log + fall through          client_start.py:944-949
if not _session_id:                           client_start.py:974
  └─ session/new {cwd, mcpServers}            client_start.py:625
     └─ _session_id = resp["sessionId"]       client_start.py:982
if _is_kiro:  session/set_mode {modeId}        client_start.py:1092-1095   [codex SKIPS this]
_apply_startup_model()                         client_start.py:1121
if Routing.SESSION_CONFIG:                    client_start.py:1135
  └─ session/set_config_option {configId:"mode", value:"read-only"}   client_permissions.py:156
```

(shared-runtime path: `AcpRuntime.load_session` → `runtime_session_load.py:289`;
`AcpRuntime.create_session` → `runtime_session_create.py:398`.)

---

## Verification notes

- Every `file:line` above was read directly from the installed package at
  `/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew`.
- No code was executed; this is a static read of the installed source.
- The `x64` sibling at `backend-dist/kirocrew-backend-x64/` contains a parallel `kiro_crew`
  tree; it was not line-by-line compared against the `arm64` tree read here.
- **UNKNOWN:** whether the codex backend in a live deployment actually advertises
  `loadSession: true` — that is a runtime handshake fact, not a source fact. The driver reads
  it at `client_start.py:783`; this document does not assert its value.
- **UNKNOWN:** whether a concrete adopter backend (e.g. a `jcode acp` shim) returns a `modes`
  block from `session/load`. The driver's acceptance test is at `client_start.py:933`.
