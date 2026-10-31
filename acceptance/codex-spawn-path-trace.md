# codex ACP spawn path trace (read-only code analysis)

Date: 2026-10-30
Author: jcode (orchestrator delegated; code read only, no execution of the app)

Sources read (read-only, installed build):
`BT='/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64'`,
`SP="$BT/lib/python3.12/site-packages/kiro_crew"`.
All citations below are `file:line` inside that package unless stated otherwise.

## One-line answer

**The live codex spawn path DOES consult `CODEX_ACP_BIN`** (the override is wired in), so the
observed genuine-binary spawn is NOT "the override is not wired to this path". Decisive line:
`acp/harness/codex.py:273` — `argv, search_path = await asyncio.to_thread(client_mod._resolve_codex_acp_bin)`,
which reaches `acp/launch.py:291` — `override = os.environ.get(override_env)` where
`override_env == "CODEX_ACP_BIN"` (`acp/client_spawn_env.py:103`).

## Call chain (codex backend, macOS Seatbelt)

1. `acp/runtime_spawn_launch.py:300` — `plan = await self._harness.resolve_spawn(...)`
   (the runtime asks its harness for the argv; the runtime itself never picks a binary).
2. `acp/harness/__init__.py:86` — `ACP_BACKEND_CODEX: CodexHarness` (backend id -> harness).
3. `acp/harness/codex.py:273` — `argv, search_path = await asyncio.to_thread(client_mod._resolve_codex_acp_bin)`.
4. `acp/client_spawn_env.py:231` — `def _resolve_codex_acp_bin()` ->
   `acp/client_spawn_env.py:238-242` calls `launch_mod._resolve_node_adapter_argv(bin_name=CODEX_ACP_BIN, override_env=_ENV_CODEX_ACP_BIN, vendored_entry=...)`.
5. `acp/launch.py:291-293` — rung 1 is the operator override:
   `override = os.environ.get(override_env)` / `if override and Path(override).is_file(): candidates.append(override)`.
6. `acp/launch.py:298-322` — remaining rungs: vendored `node_modules` entry (298-300),
   `mise which` (304-306), mise installs glob (310-315), augmented PATH `shutil.which` (319-322).
7. `acp/launch.py:324-341` — candidate acceptance; for a directly executable file it returns
   `[script]` (336), for a node script `[node, resolved]` (328 / 339).
8. `acp/harness/codex.py:294-295` — `return SpawnPlan(argv=list(argv), ...)`.
9. `acp/runtime_spawn_launch.py:438` — `argv = plan.argv`; `acp/runtime_spawn_launch.py:662-665` —
   `launched = await launch(self, LaunchRequest(argv=argv, ...))`.
10. `acp/launch.py:943,966-969` — `launch()` -> `tools.apply_pod_bundle_spawn(request.argv, ...)` -> the
    sandbox spawner.
11. `sandbox_spawn.py:575` — `sandboxed_spawn_argv(...)`; `sandbox_spawn.py:640-656` calls
    `wrap_argv(argv, ...)` (the async variant `sandbox_spawn.py:385`).
12. `sandbox_wrap.py:438` — `def wrap_argv(...)`; `sandbox_wrap.py:877,886,899` dispatch to
    `sandbox_exec_argv(argv, ...)` when the macOS backend is `sandbox-exec`
    (backend detection `sandbox_wrap.py:391`, `sandbox_wrap.py:416`).
13. `sandbox_argv.py:731` — `def sandbox_exec_argv(argv, ...)`; the argv is assembled at
    `sandbox_argv.py:916-926`:
    `[outer_env, *unset_args, marker, level_assign, sandbox_exec, "-f", path, *resolved_argv]`
    where `sandbox_exec = platform_compat.trusted_system_bin("sandbox-exec") or "/usr/bin/sandbox-exec"`
    (`sandbox_argv.py:915`) and `path` is the Seatbelt profile. `<binary>` is `resolved_argv[0]`,
    i.e. `plan.argv[0]` from step 8.
14. `sandbox_argv.py:753-755` — `resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])`;
    `sandbox_argv.py:144-160` delegates to `current_context().agent_executable.resolve_executable`,
    whose default implementation is the identity (`platform/defaults.py:166-167`:
    `def resolve_executable(self, executable: str) -> str: return executable`).
    So no second binary substitution happens here.

## Answers to the five questions

### 1. What decides the backend BINARY path in `runtime_spawn_launch.py`?

Not the runtime. `acp/runtime_spawn_launch.py:300` delegates to the harness's
`resolve_spawn`; for codex that is `CodexHarness.resolve_spawn`
(`acp/harness/codex.py:241`, class declared at `acp/harness/codex.py:223`), whose argv comes from
the resolver at `acp/harness/codex.py:273`.

`ACP_BACKEND_PROCESS_NAMES` is **not** the binary decider on this path. It only supplies the
*name* `"codex-acp"` (`agent_sdk/backends_launch.py:226`), consumed at
`acp/client_spawn_env.py:95` (`CODEX_ACP_BIN = ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_CODEX]`)
and at `agent_sdk/backend_install.py:102` (install-probe vocabulary). The package states this
explicitly: `agent_sdk/backends_launch.py:148-154` — "claude-agent-acp, codex-acp and pi-acp are Node
adapters resolved through a different ladder ... none of the four is a member and none of their
spawn arms reads this table." The PATH lookup that produced the observed genuine binary is rung 5
*inside the same resolver* (`acp/launch.py:319-322`), not a separate arm.

### 2. Where is the `sandbox-exec -f <profile>.sb <binary>` argv assembled?

- `sandbox_argv.py:731` `sandbox_exec_argv` — assembly at `sandbox_argv.py:916-926`
  (`sandbox_exec` at 915, `"-f"` at 923, profile `path` at 924, then `*resolved_argv` at 925).
- `sandbox_argv.py:745` — docstring: "Wrap *argv* with ``sandbox-exec -f <profile>``".
- Called from `sandbox_wrap.py:886` and `sandbox_wrap.py:899` (inside `wrap_argv`, `sandbox_wrap.py:438`).
- Which is called from `sandbox_spawn.py:640` / `sandbox_spawn.py:652` (inside
  `sandboxed_spawn_argv`, `sandbox_spawn.py:575`).

The symbol that supplies `<binary>`: `plan.argv[0]`, produced by
`CodexHarness.resolve_spawn` -> `_resolve_codex_acp_bin` -> `_resolve_node_adapter_argv`
(`acp/harness/codex.py:273`, `acp/client_spawn_env.py:231-242`, `acp/launch.py:252`), carried in
`SpawnPlan(argv=list(argv), ...)` (`acp/harness/codex.py:294-295`) and read back at
`acp/runtime_spawn_launch.py:438`.

### 3. Is `_resolve_codex_acp_bin` called on THIS path?

Yes. Call sites of `_resolve_codex_acp_bin` in the whole package:

| site | what it is | used by the codex runtime spawn? |
|---|---|---|
| `acp/client_spawn_env.py:231` | definition | n/a |
| `acp/harness/codex.py:273` | the spawn-time resolution | **YES — this is the one** |
| `acp/client.py:178` | re-export (`from kiro_crew.acp.client_spawn_env import _resolve_codex_acp_bin  # noqa: F401`) | makes it reachable as `client_mod._resolve_codex_acp_bin` |
| `agent_sdk/drivers/acp.py:673-675` | `codex_adapter_resolves()` — an install/probe seam | no (dashboard/install check) |

`acp/harness/codex.py:267-273` explains why it goes through `client_mod`: circular import, so
the call is made at call time. The runtime reaches this through
`acp/runtime_spawn_launch.py:300` -> `acp/harness/__init__.py:86` (`ACP_BACKEND_CODEX: CodexHarness`).

The other spawn entry point, `AcpClient` (`acp/client_start.py:204-212`), uses
`process_adapter_for(self.backend)`; `acp/harness/__init__.py:111-117` shows codex is **not** in
`_PROCESS_ADAPTERS`, and `acp/client_start.py:223-250` is the kiro-cli arm. So the codex runtime
spawn is the harness path, i.e. `acp/harness/codex.py:273`.

### 4. Is there a second, separate codex resolution that ignores `CODEX_ACP_BIN`?

**No such second resolution was found in the package.** The full grep for `CODEX_ACP_BIN`
across `kiro_crew` returns only: `acp/client_spawn_env.py:95,103,234,238` (the name, the env-var
name, and the resolver that reads it), `acp/client.py:161,165,178` (re-exports),
`acp/harness/codex.py:263` (a docstring naming the ladder order), `acp/launch.py:129`
(a docstring). The neighbouring resolutions that DO exist are for other backends and do not touch
codex's binary:

- `_resolve_self_served_bin` — used via `agent_sdk/drivers/acp.py:720-723`
  (`self_served_resolves`), for the self-served backends; codex is not a member of
  `ACP_BACKEND_LAUNCH` (`agent_sdk/backends_launch.py:148-154,155`).
- `ACP_BACKEND_PROCESS_NAMES` PATH lookups in `agent_sdk/backend_install.py:82,94,102,110` are
  install-probe component names, not spawn argv.

The only PATH-based fallback for codex is rung 5 *of the override-aware resolver*
(`acp/launch.py:319-322`), i.e. it is reached only when rung 1 (the override) did not produce a
usable candidate.

### 5. Conclusion

The override **is wired to the live codex spawn path**. The resolver reads the override from the
*process environment at resolution time* (`acp/launch.py:291`), takes it as the first candidate
(`acp/launch.py:292-293`), and returns it when the path is an existing file
(`acp/launch.py:292`, accepted at `acp/launch.py:324-336`).

Two code-supported facts make "the env did not carry a usable `CODEX_ACP_BIN` at that moment" the
explanation class that fits:

- The override rung is **silently skipped** when the value is empty or is not an existing file
  (`acp/launch.py:292`: `if override and Path(override).is_file():`). There is no log on that
  branch, so the ladder falls through to the PATH rung and yields the genuine
  `/Users/kooshapari/.bun/bin/codex-acp` with no trace that an override was requested.
- The value read is `os.environ` of the process doing the resolution, not the file
  `~/.kiro/crew/.env`. The file only reaches the process environment through
  `KiroCrewConfig.load_credentials(propagate=True)` (`config/loader_config.py:1132`,
  parse loop at `config/loader_config.py:1166-1172`, propagation loop at
  `config/loader_config.py:1260-1273`).

Measured host state at analysis time (read-only):

- `~/.kiro/crew/.env` line 1: `CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim` (file mtime
  2026-10-30 16:57:31).
- The shim exists and is executable: `/Users/kooshapari/bin/jcode-acp-shim` (`-rwxr-xr-x`, mtime
  Oct 29 13:51), a `#!/bin/bash` script.
- The live KiroCrew backend processes **do** carry it in their environment:
  gateway pid 86254 (`kiro_crew gateway --no-o...`) and children 10683, 13111, 17873, 17888 all
  show `CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim`; the gateway's `PATH` contains
  `/Users/kooshapari/.bun/bin`, which is where the genuine adapter lives
  (`~/.bun/bin/codex-acp -> ../install/global/node_modules/@agentclientprotocol/codex-acp/dist/index.js`).

Therefore, a spawn performed by any of those processes **now** should resolve to the shim, not to
the genuine adapter.

**UNKNOWN (deliberately not inferred):** why the operator's earlier live turn spawned the genuine
binary. The read-only evidence cannot date that spawn, cannot show that process's environment at
that instant, and cannot exclude a spawn path other than `CodexHarness.resolve_spawn`. The
discriminating checks (all read-only, runnable by the operator) are:

1. `ps eww -p <pid-of-the-spawning-backend>` at the time of the spawn — does its env carry
   `CODEX_ACP_BIN`, and is the value a path that exists?
2. Was the value in `~/.kiro/crew/.env` at that time the same string as today? The file was
   modified 2026-10-30 16:57:31, and the currently running gateway processes started
   2026-10-30 19:16-19:24 — i.e. after that edit, so they have today's value.
3. Was the spawn even issued by `kiro_crew`? `sandbox_argv.py:916-926` always emits a leading
   `outer_env` (`sandbox_argv.py:914`, `_pinned_env_bin()`) plus `env -u` unset args before
   `sandbox-exec`, whereas the observed command line was literally
   `/usr/bin/sandbox-exec -f <sandbox>.sb /Users/kooshapari/.bun/bin/codex-acp`. If that command
   line was captured verbatim (not trimmed by `ps` width or by a wrapper), it does not match
   `sandbox_argv.py:916-926` exactly and would point at a different assembler — which this read
   did not locate. **UNKNOWN.**

## Not determined

- The exact process, environment, and timestamp of the operator's observed genuine-binary spawn.
- Whether the observed `sandbox-exec` command line was produced by `kiro_crew.sandbox_argv.sandbox_exec_argv`
  or by another assembler (the observed form lacks the leading `env` element that
  `sandbox_argv.py:916-926` always emits).
- Whether any sandbox-level environment scrub (`sandbox_spawn.py:451` `scrub_env`,
  `sandbox_spawn.py:514` `scrub_agent_denied_env`) removes `CODEX_ACP_BIN` from the child's
  environment. Not read beyond the definition sites; it would affect the *child*, not the
  resolution, which happens in the parent before the spawn.
