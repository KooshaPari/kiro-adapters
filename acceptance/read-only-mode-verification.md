# Read-only mode: override proven at the resolver, NOT reached by a live turn

Measured 2026-10-30/31 on this Mac (KiroCrew `0.11.0-nightly.20261021t201456`).
Verdict: **PARTIAL**. The override works where it can be called directly, and the
live spawn path did not use it. Do not read this as "read-only mode is active".

## P4 — the override file exists

`~/.kiro/crew/.env`, mode `0600`, one line:

```
CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim
```

The file was **absent** before this session, so nothing overrode the backend.

## P5 — the gateway reads it

After a restart of launchd service `dev.kirocrew.gateway`, the gateway's own
stderr carried:

```
Unknown key CODEX_ACP_BIN in .env is not a recognised credential -- it will
propagate to child processes but is NOT agent-isolated.
```

That line is the gateway confirming it parsed the file. It is also an honest
statement of the limit: the key propagates, it is **not** agent-isolated.

## P6 — resolver level: PROVEN

Calling the resolver the gateway uses, after loading `.env` the way the gateway
does, on the backend's own interpreter:

```python
from kiro_crew.config.loader_config import KiroCrewConfig
from kiro_crew.acp import client_spawn_env as cse

KiroCrewConfig().load_credentials(propagate=True)
cse._resolve_codex_acp_bin()
```

Output:

```
propagated keys: ['CODEX_ACP_BIN']
CODEX_ACP_BIN after load: '/Users/kooshapari/bin/jcode-acp-shim'
RESOLVED_ARGV: ['/Users/kooshapari/bin/jcode-acp-shim']
IS_SHIM: True
```

Control: the same resolver **without** `load_credentials` returns the genuine
binary, which is what the override is supposed to replace:

```
CODEX_ACP_BIN in env: None
RESOLVED_ARGV: ['/Users/kooshapari/.bun/bin/codex-acp']
IS_SHIM: False
```

`load_credentials` is a **method** on `KiroCrewConfig` (`loader_config.py:1132`),
not a module function. Calling it as `loader_config.load_credentials(...)` raises
`AttributeError`; a reader who tries the module-level form will wrongly conclude
the mechanism is missing.

## P6 — live turn: NOT PROVEN, and the attempt failed

`agent.acp_backend` was absent, so the codex slot was never selected. `codex` is
a valid value (`selectable_backend_values()` -> `['', 'claude', 'codex',
'deepseek', 'goose', 'kas', 'opencode', 'pi']`), so it was set to `codex`
(pre-change backup: `config.json.pre-acp-backend-20261030T182127.bak`), the
gateway restarted, and a real turn was run.

The spawn that actually happened:

```
6978 /usr/bin/sandbox-exec -f .../kirocrew_sandbox_4651_70holxr3.sb \
     /Users/kooshapari/.bun/bin/codex-acp
```

That is the **genuine** `codex-acp`, not `~/bin/jcode-acp-shim`. And the turn
failed:

```
kiro_crew.acp.session_handle.AcpRequestTimeout: Request initialize timed out after 90s
EXIT=1
```

So: the override is honoured by `_resolve_codex_acp_bin`, and the live
`acp_backend=codex` spawn path did **not** consult it in this build. Root cause
is **not isolated**. Candidates, none verified: the sandboxed spawn goes through
`acp/runtime_spawn_launch.py` rather than the driver that uses the resolver; or a
bare process name is resolved through the augmented PATH, which reaches
`~/.bun/bin` first.

## Confound that must not be ignored

During the failing turn the host load average was **91-144** on 16 cores, with 78
`jcode`/`kcode` processes and unrelated `grep -rn` / `du -skx` sweeps from other
sessions. So:

- the `initialize timed out after 90s` **cannot** be attributed to the codex
  path; a starved host produces the same timeout;
- the gateway's cold bind took **70-300 s** during this window versus ~8-14 s
  earlier, and `/api/health` answered in **3-8 s** versus 0.009 s earlier. Those
  are **load effects, not regressions**. An earlier note in this repo refuted a
  "7 minute bind" premise; that refutation stands for the quiet-host measurement
  and should be re-measured before being repeated under load.

## Restored

`agent.acp_backend` was removed again (it was absent before), the gateway
restarted, and a real turn verified working:

```
RESTORED
EXIT=0
```

`~/.kiro/crew/.env` was **left in place**: it is inert while `acp_backend` is
absent, and removing it would discard the only working half of this result.

## What is left, precisely

To finish this node, one of these must be established:

1. Which code path spawns the codex ACP backend when `acp_backend=codex`, and
   whether it reads `CODEX_ACP_BIN` at all.
2. Whether `initialize` succeeds for the genuine `codex-acp` on an **idle** host.
   Until that is measured on a quiet machine, the timeout is unattributed.

## Retest on a quiet host (2026-10-31T09:08Z)

Host state at test time: load averages `18.52 20.14 19.06` on 16 cores, gateway
`/api/health` 200 in **0.007-0.185 s**, no cargo-mutants run active. This is a
quiet host by the baseline in `acceptance/host-idle-baseline.md`.

`agent.acp_backend` set to `codex`, gateway restarted, `~/.kiro/crew/.env` in place
with `CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim`.

Spawn observed at T+8 s:

```
1713 /usr/bin/sandbox-exec -f .../kirocrew_sandbox_945_vfsb3hen.sb \
     /Users/kooshapari/bin/jcode-acp-shim
```

That is the **shim**, not the genuine `~/.bun/bin/codex-acp`. The override now
reaches the live spawn path, on a quiet host, with the process env carrying it.

The turn itself did **not** complete: the model backend returned a transient
HTTP 5xx, and the CLI reported `❌ The model backend hit a transient error`.
That is a provider/model failure, not a read-only-mode failure, and it is unrelated
to the shim. The shim was selected and spawned, which is the claim P6 was
measuring.

`agent.acp_backend` was reverted and the gateway restarted; the default-backend
chat returned `RESTORED2`, exit 0.

### Verdict

**P6 is now PROVEN at the spawn layer.** The shim is selected by a live Kiro Crew
turn on a quiet host. What remains unverified is what the shim does *after* spawn:
whether the read-only mode survives `session/load` and arms the tool-call gate. That
requires a turn that also completes the model call, and is blocked on provider
availability, not on this host.
