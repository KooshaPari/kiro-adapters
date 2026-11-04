# ACP Adoption Shim

`jcode_bridge.acp_shim` lets an ACP client — Kiro Crew, or anything else that
speaks Agent Client Protocol — attach to a jcode/KCode session that the
operator is already running.

It is an **adopter**, not a launcher. The distinction is the whole point of
this module.

## Deployment status (measured 2026-10-30/31)

The shim is **built and tested but not yet reached by a live Kiro Crew turn**.
The distinction matters, so it is stated before the design:

| Layer | State | Evidence |
|---|---|---|
| Unit suite | **PASS** — 355 tests in the repo, 22 in `tests/test_acp_adoption_shim.py` | `acceptance/read-only-mode-verification.md` |
| Shipped KCode build carries the widened read-only denial set | **PRESENT** | `acceptance/shim-binary-forensics.md` |
| `CODEX_ACP_BIN` override honoured by the resolver | **PROVEN** — returns `['/Users/kooshapari/bin/jcode-acp-shim']` once `.env` is loaded | `acceptance/read-only-mode-verification.md` |
| Live `acp_backend=codex` turn spawns the shim | **NOT PROVEN** — it spawned the genuine `~/.bun/bin/codex-acp` | `acceptance/read-only-mode-verification.md` |
| Live turn with `acp_backend=codex` | **FAILED** — `initialize` timed out after 90 s | same, and the timeout is **unattributed**: host load was 91-144 at the time |
| Kiro Crew chat on the default backend | **PASS** — `RESTORED`, exit 0 | same |

`~/.kiro/crew/.env` now exists (mode 0600) with
`CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim`, and the gateway's own
stderr confirms it parses and propagates the key. It is **inert** while
`agent.acp_backend` is absent, and was deliberately left in place: it is the
only working half of the wiring.

Do not read this table as "read-only mode is active". It is not. See
`acceptance/codex-spawn-path-trace.md` for the open question of which spawn
path bypasses the override.

## What it does

```mermaid
flowchart LR
    client["ACP client<br/>(Kiro Crew)"]
    shim["jcode-acp-adopt<br/>stdin/stdout JSON-RPC 2.0"]
    gate{"read-only gate"}
    upstream["kcode acp<br/>(existing ACP child)"]
    client -->|"initialize, session/load"| shim
    shim --> gate
    gate -->|"session file exists<br/>AND daemon socket accepts connect"| upstream
```

1. Speaks ACP on newline-delimited JSON-RPC 2.0 over stdin/stdout. That is the
   framing the fork's ACP front end uses (`src/cli/acp.rs:316-322`), so the
   shim speaks to `kcode acp` without a translation layer.
2. Forwards `session/load` upstream, but only after two read-only checks pass.
3. Advertises `loadSession: true` and a single `read-only` mode.
4. Adopts the session for observation, but **does not forward
   `session/prompt`** unless the operator opts in. Adoption and prompting are
   separate decisions, because the shim cannot enforce the daemon's tool
   authority.

## What is actually enforced, and what is not

The shim is one process between the client and an ACP front end. It can refuse
methods; it cannot constrain what the upstream daemon does with a request it
does receive. This section is deliberately blunt.

### Enforced by the shim

| Enforced | How |
|---|---|
| `session/new` is never sent | Blocked in `Upstream.guard`, so every transport inherits it. |
| `session/close` is never sent | Same guard; `session/close` answers locally with `{"closed": false, "detached": true}`. |
| Adoption needs a real session file and a live socket | `gate.assess` runs both probes and fails closed with `-32010`. |
| No daemon is ever spawned | The shim never calls `ensure_daemon`; the probe only connects and closes. |
| Non-`read-only` modes are refused | `session/set_mode` rejects anything but `read-only` locally. |
| Write-enabling config options are refused | `writes`, `allow_writes`, `write`, `approval`, `auto_approve` are rejected locally. |
| `session/prompt` is refused by default | `-32012 SHIM_PROMPT_FORWARDING_DISABLED` with a structured `data` payload, before any upstream call. |
| `session/cancel` does not reach the daemon while prompting is disabled | Answered locally: no prompt can be in flight through the shim. |

### NOT enforced by the shim

| Not enforced | Why |
|---|---|
| The daemon's tool allowlist / file-write authority | The shim has no visibility into upstream tool execution. If a prompt is forwarded, the daemon applies its own permissions, not the shim's advertisement. |
| The KiroCrew permission model | KiroCrew derives a static narrowed tool allowlist and uses `session/request_permission`; jcode arbitrates a live interaction lease via `require_interaction_ownership`. These are different models; this shim does not bridge or translate them (see `KAS-VS-JCODE.md`). |
| Upstream `session/set_mode` | The fork answers `-32601 METHOD_NOT_FOUND`, so `--forward-mode` degrades to a local contract and the daemon mode is unchanged. |

`_meta.jcode.acpShim` in `initialize` advertises `readOnly`,
`promptForwarding` and `promptForwardingOptIn` so a client can see the posture
instead of inferring it. `readOnly` is `true` only while prompt forwarding is
disabled.

### The explicit opt-in

`--allow-prompt-forwarding` turns prompt forwarding on. It is **off by
default**, loudly named, and turning it on makes the read-only claim false:
the shim will then forward prompts with the upstream daemon's own authority.
The `_meta` advertisement flips `readOnly` to `false` and
`promptForwarding` to `true` so the client is told.

## What it refuses to do

| Refused | Why |
|---|---|
| Start a daemon | `session/load` upstream calls `connect_daemon` → `ensure_daemon`, which *spawns* a daemon when none is running (`src/cli/acp.rs:851-867`). The shim checks for a live socket first and fails closed with `SHIM_PRECONDITION_FAILED` instead. |
| `session/new` | The shim adopts; it does not create. Blocked at the transport, not by policy. |
| `session/close` | Upstream, `session/close` cancels a running prompt and stops the event pump (`src/cli/acp.rs:653-677`). It is not a detach. Detach is dropping the connection. |
| Write-capable `session/set_mode` | The shim advertises `read-only` and nothing else. Selecting any other mode is refused locally. |
| Write config options | Anything named `write`, `writes`, `allow_writes`, `approval` or `auto_approve` is refused. |
| `session/prompt` (default) | The shim cannot enforce the daemon's tool authority, so forwarding is refused with `-32012` unless `--allow-prompt-forwarding` is set. |

`sessionCapabilities.close` is **dropped** from the advertised capabilities.
The shim must not advertise what it will not do.

## The adoption gate

`gate.assess` is the fail-closed gate. It runs two probes and both must pass:

1. **Session exists.** The session id must resolve to an existing `<id>.json`
   under a known session directory (`~/.jcode/sessions`, `~/.kcode/sessions`, or
   an explicit `--session-dir`; `JCODE_ACP_SESSION_DIR` overrides the list).
   The id is rejected before any filesystem access if it is empty, longer than
   200 characters, starts with `.`, or contains `/`, `\` or a NUL byte.
2. **Daemon is live.** A daemon socket must already accept a connect. The
   probe sends no protocol bytes — it opens and closes the socket. If no socket
   is live the shim fails closed; it never starts one.

If either check fails, the answer is `SHIM_PRECONDITION_FAILED` with
`data.willSpawnDaemon = false`.

`session/list` is a directory read and never touches the daemon
(`src/cli/acp/discovery.rs:5-60`), so it is always allowed.

## Running it

```bash
jcode-acp-adopt --upstream kcode acp \
    --session-dir ~/.kcode/sessions \
    --socket ~/.kcode/scratch/kcode.sock
```

| Flag | Meaning |
|---|---|
| `--upstream ARGV...` | ACP front end argv. Default `kcode acp`. This is the only child process the shim can create. Everything after `--upstream` goes to the child verbatim, so `--upstream kcode --no-selfdev acp` works. Put the shim's own flags **before** `--upstream`. |
| `--session-dir DIR` | Session store to search. Repeatable. |
| `--socket PATH` | Daemon socket to probe. Repeatable. Never created. `JCODE_ACP_SOCKET` overrides the list. |
| `--mode MODE` | Only `read-only` is accepted. |
| `--forward-mode` | Also forward `session/set_mode`, degrading on upstream `-32601`. |
| `--allow-prompt-forwarding` | **DANGEROUS.** Forward `session/prompt` upstream. Off by default; the forwarded prompt runs with the daemon's own permissions. |
| `--timeout SECONDS` | Upstream request timeout. |

The shim also exports `JCODE_ACP_SHIM=1` into its own environment so a
downstream process can tell it is running under the shim.

### Why the argv is split by hand

`--upstream` is split out of `argv` before argparse sees it. argparse's
`REMAINDER` is not used, because its handling of a literal `--` differs
between Python 3.9 and 3.12, and because a flag-like child argument must
never be parsed as the shim's own flag. With the split done first,
`--upstream kcode --no-selfdev acp` reaches the child unchanged on every
supported Python.

This was found by live probing, not by the unit suite: the in-process double
never goes through the command line.

## Errors

| Code | Meaning |
|---|---|
| `-32010` `SHIM_PRECONDITION_FAILED` | A documented precondition was not met (no session file, or no live daemon socket). |
| `-32011` `SHIM_UPSTREAM_UNAVAILABLE` | The upstream ACP link is gone or timed out. |
| `-32012` `SHIM_PROMPT_FORWARDING_DISABLED` | Prompt forwarding is off (the default). `data.precondition = prompt_forwarding_disabled`; `data.willSpawnDaemon = false`. |

Standard JSON-RPC codes (`-32700`, `-32600`, `-32601`, `-32602`, `-32603`) are
used for protocol faults.

Error messages and `data` payloads are scrubbed: JWT-shaped strings, bearer
tokens, `token=`/`api_key=`/`secret=`/`password=` assignments and `sk-`/`pk-`/
`rk-` prefixed keys are redacted before they leave the process. The shim never
keeps a transcript body — callers pass only method names and already-derived
error text.

## Transports

`Upstream` is the seam. Two implementations ship:

- `StdioUpstream` — spawns the ACP front end and speaks newline-delimited JSON
  to it. This is the production path.
- `InProcessUpstream` — the test double. It never spawns anything and records
  only method names, never parameters.

Both enforce the forbidden-method list in `Upstream.guard`, so the invariant
lives in one place rather than at each call site.

## Tests

`tests/test_acp_adoption_shim.py` covers the contract with no real jcode
install: the gate, the transport block list, the mode handling, the error
scrubber, the parse-error path and the serve loop. The upstream is an
`InProcessUpstream` double, so the suite never touches a daemon, a socket or
a session file.

```bash
make test
```
