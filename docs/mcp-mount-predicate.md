# What decides whether a spec `mcpServers` entry is mounted (`session/new.mcpServers`)

Read-only investigation of the installed backend:

```
/Applications/KiroCrew Nightly.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/
  lib/python3.12/site-packages/kiro_crew/
```

All line numbers below are from that copy. Nothing was configured, restarted, or chatted.

## TL;DR (the finding that changes the diagnosis)

**Crew's own composer does NOT withhold `kiro-inventory`.** By the predicate in
`acp/session_mcp.py` the entry is mountable, and the `mcp_ref_guard` log line proves it was
already on the wire: `unresolved=@kirocrew-computer, @kirocrew-guide, @kirocrew-mate` lists
three refs and **omits `@kiro-inventory`**, and the guard only omits a ref it could satisfy
(`agent_sdk/mcp_refs.py:237-255`). So `@kiro-inventory` resolved; the array contained it.

The four servers the agent reported (`portal-bridge`, `workos-docs`, `node_repl`,
`phinbox-mcp`) are **not in the spec at all**, and three of them are the agent's *own*
config: `~/.codex/config.toml` has `[mcp_servers.workos-docs]` (line 312) and
`[mcp_servers.node_repl]` (line 316), and `~/.codex/mcp.toml:5` has
`[mcp_servers.phinbox_mcp]`. A recursive grep over the whole installed package, the app
bundle `Resources`, and `~/.kiro/crew/apps` finds **zero** occurrences of `portal-bridge`,
`phinbox-mcp` or `node_repl`. `workos-docs` *is* in the spec, but with `"disabled": true`.

So the absence of `kiro-inventory` is decided **downstream of `session_mcp.py`**, in the
harness's own configuration merge, not by a Crew predicate.

## 1. The full predicate in `acp/session_mcp.py`

`session_mcp_servers()` is `acp/session_mcp.py:1073-1218`. Its stages, in order:

1. **Spec resolution** — `session_mcp.py:1105-1107` calls
   `_session_spec_and_snapshot_for()` (`:631-642` -> `:644-729`), which is
   project-nearest first (`:704-706`), with `_project_mcp_trusted()` hard-wired to
   `return False` (`:537-557`) so a project-checkout spec never picks servers on a
   mirrored session; then user-level `_user_agent_spec()` (`:614`) -> `~/.kiro/agents/*.json`.
2. **Registry ceiling** — `:1162-1180`. `registry_mode = _registry_mode()` (`:961-984`,
   fail-closed to `True`). In registry mode **every** spec server is popped; outside it,
   any entry with `"type": "registry"` is popped (`_KIRO_REGISTRY_TYPE`, `:153`).
3. **Control plane re-added** — `:1182-1185`, from `managed_mcp_spec_entry(name)` for
   `CONTROL_PLANE_SERVERS`, not from the spec.
4. **Broker stubs win** — `:1187-1192` pops any name handed in `stub_server_names`.
5. **`tools` allowlist** — `:1206-1217` with `_tools_allowlist()` (`:440-446`),
   `ToolsAllowlist.grants()` (`:434-437`): a name must appear in `tools` as `@name` or
   `@name/tool`, or `tools` contains bare `*`. A missing/non-list `tools` is an **empty
   allowlist**, not "no filter" (`:1208-1216` comment).
6. **Whole-server mute** — `:1219-1222` via `session_mcp_disabled_servers()` (`:842-876`),
   which is `mcp_entry_is_muted()` (`mcp_cleanup.py:58`, fail-closed: anything but absent
   `disabled` or literal `false` is a mute) over the spec **and** the global settings file.
7. **Element shape** — `acp_server_element()` (`:168-216`): needs a non-empty `url`
   (http/sse) or a non-empty `command` (stdio, `:189-192`); `env`/`headers` are emitted as
   arrays of `{name,value}` (`:156-165`, `:214`).

**There is no allowlist of "known server names", no signed/known-servers set, and no
requirement that the server also be registered in `~/.kiro/crew/mcp.json`, a gateway
registry, or `~/.kiro/settings/mcp.json`.** The spec is the single source of truth
(module docstring, `:21-25`). The only name-keyed withholds are: the registry-marker rule
(`:1162-1180`), `disabled: true` (`:842-876`), the `tools` allowlist (`:434-437`), and codex's
own withhold set in the mirror (below).

## 2. Why the four mount and `kiro-inventory` does not

Not shape, and not a name registry. The decisive facts:

- All four reported names are **absent from `~/.kiro/agents/kirocrew.json`'s `mcpServers`**
  (`kirocrew-cron`, `kirocrew-core`, `kirocrew-guide`, `kirocrew-mate`,
  `chrome-devtools`, `cloudflare-*`, `octocode`, `phenotype-portal`, `workos`,
  `workos-docs`, `kirocrew-computer`, `jcode-bridge`, `kiro-inventory`). So Crew's array
  cannot be their source.
- `workos-docs`, `node_repl` and `phinbox_mcp` are byte-for-byte servers of the **codex
  agent's own configuration**: `~/.codex/config.toml:312` and `:316`, `~/.codex/mcp.toml:5`.
  This is exactly the documented behaviour at `acp/mcp_ref_guard.py:178-183`: "codex-acp
  merges the array on top of `~/.codex/config.toml`", and `~/.kiro/settings/mcp.json` is
  loaded into every kiro agent by default. `portal-bridge`: **UNKNOWN** — not found in any
  Crew file, the app bundle, or `~/.codex/*`.
- `kiro-inventory` in the spec differs from its neighbours in **no relevant field**:
  `{"command": ".../kiro-adapter-mcp", "args": ["--inventory-only"], "type": "stdio"}` vs
  `jcode-bridge` `{"args": [], "command": ".../jcode-bridge-mcp", "type": "stdio"}`. No
  `env`, no `cwd`, no `enabled`, no `identity`, no `requires`, no `version` on either. It is
  not `disabled`, and it is not in `~/.kiro/crew/mcp-quarantine.json`
  (servers there: `jcode-bridge`, `kirocrew-computer`, `kirocrew-core`, `kirocrew-cron`,
  `kirocrew-guide`, `kirocrew-mate`).
- Its `@kiro-inventory` ref is in both `tools` and `allowedTools`, so stage 5 grants it, and
  its command is non-empty, so stage 7 emits an element.

Conclusion: Crew emits it (proven by its omission from the guard's `unresolved` list). The
name is dropped after `session_mcp.py` — in the adapter's own config-merge/launch path
(`codex_agent.rs`, Rust, not shipped in this bundle as source: **UNKNOWN** exact rule). The
Crew side of the loss is therefore not a predicate at all.

## 3. What `mcp_ref_guard` does with `@name`, and what "unresolved" means

- `warn_unresolved_server_refs()` — `acp/mcp_ref_guard.py:125-196`. It computes
  `unresolved_server_refs(spec, wire_servers, backend=...)` (`:158`) and logs **one**
  structured line (`:184-194`). It changes NOTHING: "it changes NOTHING -- not the array, not
  the session's fate" (`acp/client_mcp_array.py:632-634`).
- The resolver — `agent_sdk/mcp_refs.py:228-255`: refs come from `parse_tools_refs()`
  (`:84-126`, `@server` / `@server/<tool>`, bare `*` is grant-all, not a ref); satisfied =
  `wire_server_names(wire_servers)` (`:180-213`) plus, for a backend in
  `ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE` (kiro-cli via `--agent`, KAS via `_meta`), the spec's
  own `mcpServers` names (`:241-248`). A ref is **unresolved** iff
  `name not in satisfied and name not in RESERVED_TOOL_NAMESPACES` (`:250-255`;
  `RESERVED_TOOL_NAMESPACES = {"builtin"}`, `:81`).
- Managed servers whose gate answers closed are dropped from the report before logging
  (`mcp_ref_guard.py:90-124`, via `agent._spec_gate_answers_closed`), because their absence
  from the wire is healthy.
- `@kiro-inventory` is **resolved**, not ignored: it is absent from the logged
  `unresolved=...` list, and for a non-off-wire backend the only way to be satisfied is
  presence in the wire array. Its resolution is decided in `mcp_refs.py:241-255`.

## 4. Does `mcp_gateway = false` change which spec servers mount?

**No.** `gateway_enabled` is passed only so the log line can name the applicable remedy:
`"on" if gateway_enabled else "off"` — `acp/mcp_ref_guard.py:192-194`, with the rationale at
`:150-156` ("decides the REMEDY rather than the finding"). The call sites pass
`gateway_enabled=self._mcp_gateway_overlay is not None`
(`acp/client_mcp_array.py:648-653`, `acp/runtime_session_setup.py:846-851`).

The gateway's only mount-visible effect on this path is indirect and goes the *other* way:
with the gateway on, wrapped servers arrive as broker stubs and stage 4 (`session_mcp.py`
`:1187-1192`) replaces the raw entry with the stub (same name, added by the caller). With it
off, the projection is the only channel — stated verbatim at `acp/mcp_ref_guard.py:152-156`.
`~/.kiro/crew/config.json` currently has `mcp_gateway.enabled = false`.

## 5. The one minimal change

**No change to `~/.kiro/agents/kirocrew.json` can mount it, because Crew's projection already
carries it.** Every stage of §1 passes today: name in `tools`/`allowedTools`, non-empty
`command`, no `disabled`, no registry marker, not quarantined. Adding `"disabled": false`
would be a no-op (`mcp_cleanup.py:58-76`). Adding `env`, `cwd`, `identity` etc. changes
nothing in the predicate.

The single change that would make the *harness* mount it is on the harness side:

```toml
# ~/.codex/config.toml
[mcp_servers.kiro-inventory]
command = "/Users/kooshapari/bin/kiro-adapter-mcp"
args = ["--inventory-only"]
```

Restart required: the ACP backend process re-reads its config at process start, so the change
takes effect on the **next codex session/gateway restart**, not mid-session. (Crew's own spec
read is per-spawn and needs no restart — `acp/session_mcp.py:21-24`.)

If the real goal is instead to make the *Crew* array carry a name it currently withholds, the
one field is `"disabled"` (`false`/absent) — but that is not this case.

## Explicit "no fixed registry" statement

There is **no** compiled-in known-servers registry that a name must appear in. The only
name-shaped gates are data-driven and all currently pass for `kiro-inventory`:
registry-marker filtering (`session_mcp.py:1162-1180`), `disabled` muting (`:842-876`),
the `tools` allowlist (`:434-437`), the codex mirror's
`codex_withheld_servers()` / `withheld_servers()` (`providers/mirrors/codex.py:188-219`,
restricted-by-`disabledTools` and identity-bound Crew servers only — neither applies), and
codex's transport filter `drop_unadvertised_transports()`
(`providers/mirrors/codex.py:98-149`, which always keeps the `stdio` baseline, `:136-137`).

## UNKNOWNS

- `portal-bridge`: origin not found in the app bundle, `~/.kiro/crew/*`, or `~/.codex/*`.
- The exact codex-acp rule that drops an array element Codex cannot start (`codex_agent.rs`
  is Rust inside the adapter binary; no source shipped in this bundle).
- Why `kirocrew-core`/`kirocrew-cron` (Crew's control plane, always re-added at
  `session_mcp.py:1182-1185`) also failed to appear in the reported four.
