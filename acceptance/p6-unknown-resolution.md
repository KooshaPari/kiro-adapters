# P6 unknown resolved: which code path spawns the codex ACP backend

Measured 2026-10-31, read-only, against the installed Kiro Crew backend
(`/Applications/KiroCrew Nightly.app/.../kirocrew-backend-arm64`, Python 3.12).

## The code path

`acp/harness/codex.py` calls `client_mod._resolve_codex_acp_bin()` (line 673-675),
which delegates to `launch_mod._resolve_node_adapter_argv(..., override_env=_ENV_CODEX_ACP_BIN,
...)`, i.e. `launch.py:_resolve_node_adapter_argv`. The override env var is
`CODEX_ACP_BIN` (defined in `acp/client_spawn_env.py:103`).

## Does it read `CODEX_ACP_BIN`?

YES. Direct call (on the bundled interpreter, with `CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim`
in the environment):

```
resolved argv: ['/Users/kooshapari/bin/jcode-acp-shim']
search path:  <the resolver's PATH>
```

So the override works when the environment carries it.

## The catch, and why the live turn failed

The spawn path is `acp/harness/codex.py` -> `client_mod._resolve_codex_acp_bin` ->
`launch_mod._resolve_node_adapter_argv(..., override_env="CODEX_ACP_BIN")`.
That resolver DOES honour `CODEX_ACP_BIN`. But the earlier live attempt set
`acp_backend=codex` and the gateway spawned the genuine `codex-acp` anyway, and
then `initialize` timed out after 90 s.

The reason is not that the resolver ignores the env var. The reason is that the
gateway process itself never had `CODEX_ACP_BIN` in its environment: it is a
launchd service (`dev.kirocrew.gateway`), and `service_environment()` in
`kiro_crew/service/common.py:152` builds a closed whitelist (`HOME`, `PATH`,
`LANG`, `LC_ALL`, `KIROCREW_SERVICE_MANAGED`, plus the named `KIROCREW_KIRO_BIN`
and `KIROCREW_PORT`). `CODEX_ACP_BIN` is not on that list. `~/.kiro/crew/.env`
is parsed, but only recognised credential keys propagate; `CODEX_ACP_BIN` is
not a recognised credential, so it is dropped at boot. The resolver therefore
falls back to its node_modules / mise / PATH ladder and finds the genuine
codex-acp.

So the answer to "which code path spawns the codex ACP backend, and does it
read CODEX_ACP_BIN" is: yes, the resolver reads it; the gateway service path
never delivers it. The live-turn timeout is a separate, unattributed question
(load 91-144 on a 16-core host during the attempt).

## What this means for the override

The override works for a gateway started from a login shell that exports
`CODEX_ACP_BIN`. It does not work for `kirocrew service install`. That is the
same conclusion as `docs/ENV-PROPAGATION.md`, now with the resolver side
confirmed rather than assumed.
