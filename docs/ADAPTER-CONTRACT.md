# Adapter Contract

Normative contract for every Kiro harness adapter in this package, and for any third harness
that wants to present the same surface to an MCP client. The contract has two halves: the
result envelope, where every adapter action returns exactly one JSON object with a fixed
set of keys so callers branch on `code` and never on the text of `stderr`; and the tool
surface, a fixed list of MCP tools with fixed parameter names. Anything that satisfies
both halves is interchangeable from an MCP client's point of view.

---

## 1. Result envelope

Every adapter call returns one JSON object with these ten keys, in this order.

| Key | Type | Meaning |
|---|---|---|
| `ok` | bool | `true` only when `code` is `OK`. |
| `harness` | string | Which adapter produced this: `kirocrew` or `kirocode`. The jcode bridge reports `jcode-api` for harness-API actions and `jcode` for task-backend actions. |
| `action` | string | The action name, matching the adapter method. |
| `code` | string | One of the six result codes below. |
| `detail` | string | One-line human summary. Never parse it. |
| `data` | object | Action-specific structured payload. Schema varies per action. |
| `stdout` | string | Captured stdout, ANSI stripped and redacted. |
| `stderr` | string | Captured stderr, ANSI stripped and redacted. |
| `duration_ms` | int | Wall-clock milliseconds for the action. |
| `hints` | array of string | Remediation steps. Populated mainly on failure. |

`stdout` and `stderr` are truncated to 20,000 characters. When truncation
happens, the tail is `...[truncated N chars]`. Source:
`src/kiro_adapters/result.py` (`AdapterResult.to_dict`).

### Result codes

| Code | `ok` | Meaning |
|---|---|---|
| `OK` | true | The action completed. `data` holds the payload. |
| `NOT_INSTALLED` | false | The harness binary could not be found or could not be launched. The environment is missing the tool. |
| `BLOCKED` | false | The harness is installed but not ready for this action. A readiness gate failed. Remediation is in `hints`. |
| `TIMEOUT` | false | The child process exceeded the action timeout and was terminated. |
| `ERROR` | false | The harness ran and failed, or the request was invalid (empty prompt, missing spec file). |
| `UNSUPPORTED` | false | The harness is installed but the adapter cannot infer a usable invocation for it. |

`NOT_INSTALLED` versus `BLOCKED` is the distinction that matters most: `NOT_INSTALLED` means
"install the tool", `BLOCKED` means "the tool is there, fix its state". See `docs/OPERATIONS.md`.

---

### The jcode bridge uses the same envelope

`jcode_bridge` (see `docs/JCODE-API.md`) returns the same ten keys and the same six codes, so a
client that already branches on `code` needs no new vocabulary. Two harness values distinguish it:
`jcode-api` for the daemon-API actions and `jcode` for the one-shot task backend. `BLOCKED` is not
produced by the API actions; a spawn failure is `NOT_INSTALLED`, a deadline overrun is `TIMEOUT`,
and a protocol violation or invalid argument is `ERROR`.

## 2. Tool surface

Ten tools.

| Tool | Required | Optional | Adapter action | Cost |
|---|---|---|---|---|
| `kirocrew_status` | none | none | `KiroCrewAdapter.status` | none |
| `kirocrew_agents` | none | none | `KiroCrewAdapter.list_agents` | none |
| `kirocrew_ask` | `message` | `agent`, `model`, `timeout_secs` | `KiroCrewAdapter.ask` | one model call |
| `kirocrew_spawn` | `prompt` | `agent`, `crew`, `background`, `timeout_secs` | `KiroCrewAdapter.spawn` | one model call |
| `kirocrew_spawn_list` | none | none | `KiroCrewAdapter.spawn_list` | none |
| `kirocrew_run_spec` | `spec_path` | `name`, `no_test`, `timeout_secs` | `KiroCrewAdapter.run_spec` | one long agent run |
| `kirocrew_dashboard_url` | none | `ttl` | `KiroCrewAdapter.dashboard_url` | none |
| `kirocode_status` | none | none | `KiroCodeAdapter.status` | one `--help` probe |
| `kirocode_ask` | `prompt` | `cwd`, `timeout_secs` | `KiroCodeAdapter.ask` | one model call |
| `kiro_dispatch` | `harness`, `prompt` | `agent`, `timeout_secs` | routes to `ask` on the chosen harness | one model call |

Parameter semantics:

- `timeout_secs` is a wall-clock cap in seconds. It maps onto the adapter
  timeout and, where relevant, is also passed to the child CLI as `--timeout`.
- `background` on `kirocrew_spawn` maps to `--async`, so the spawn returns
  immediately and the subagent shows up in `kirocrew_spawn_list`.
- `no_test` on `kirocrew_run_spec` maps to `--no-test`, which skips the build
  and test verification step after each step of the spec run.
- `ttl` on `kirocrew_dashboard_url` maps to `--ttl`. The CLI default is 20h.
- `harness` on `kiro_dispatch` is exactly `"kirocrew"` or `"kirocode"`.

### jcode bridge tools, a separate server

The `jcode-bridge` MCP server is not part of the ten above; it advertises its own ten tools and
follows this same contract. Three of them are the daemon-API surface:

| Tool | Required | Optional | Backend action | Cost |
|---|---|---|---|---|
| `jcode_api_health` | none | none | `JcodeApiBackend.health` | one spawn, no session |
| `jcode_api_ask` | `prompt` | `cwd`, `timeout_secs`, `keep_session` | `JcodeApiBackend.ask` | one model call |
| `jcode_api_sessions` | none | `limit`, `timeout_secs` | `JcodeApiBackend.sessions` | none |

The remaining seven are the one-shot task backend: `jcode_health`, `jcode_run`, `jcode_task_start`,
`jcode_task_status`, `jcode_task_output`, `jcode_task_cancel`, `jcode_task_list`. `timeout_secs` is
bounded to `(0, 86400]` at the MCP edge.

### Default timeouts

Taken from the adapter modules. A caller that passes `timeout_secs` overrides
the default.

| Action | Default seconds |
|---|---|
| `kirocrew_status`, `kirocrew_agents`, `kirocrew_spawn_list`, `kirocrew_dashboard_url` | 30 |
| `kirocrew_ask` | 900 |
| `kirocrew_spawn` | 1800 |
| `kirocrew_run_spec` | 3600 |
| `kirocode_ask` | 900 |

---

## 3. Readiness gates

A readiness gate runs *before* an action that would spend a model call. If the
gate fails, the adapter returns `BLOCKED` with the failing check in `hints`,
and no child model process is started.

### kirocrew

| Check | Applies to | Failure code | Remediation in `hints` |
|---|---|---|---|
| CLI binary resolvable | every action | `NOT_INSTALLED` | install Kiro Crew, or set `KIROCREW_BIN` |
| KAS engine bundle installed | `ask`, `spawn`, `run_spec` | `BLOCKED` | `kirocrew kas install` |
| Gateway running | `spawn` only | `BLOCKED` | `kirocrew gateway` |
| Spec file exists | `run_spec` | `ERROR` | pass an existing spec path |

`ask` and `run_spec` do **not** require a running gateway. `spawn` does,
because a subagent is dispatched through the gateway. The read-only probes
(`status`, `agents`, `spawn_list`, `dashboard_url`) never gate on KAS or the
gateway, they report that state instead.

### kirocode

| Check | Applies to | Failure code | Remediation in `hints` |
|---|---|---|---|
| CLI binary resolvable | every action | `NOT_INSTALLED` | install, or set `KIROCODE_BIN`, or write `~/.kirocode/adapter.json` |
| A non-interactive argv can be inferred | `ask` | `UNSUPPORTED` | write an explicit argv template in `~/.kirocode/adapter.json` |

Binary resolution order for kirocode is: `KIROCODE_BIN` environment variable,
then the `bin` key of `~/.kirocode/adapter.json`, then `PATH` lookup of
`kirocode`, `kiro-cli`, `kiro`.

---

## 4. Redaction rules

Every string an adapter returns passes through `redact` before it leaves the
process. The rules, in order of application:

| Pattern | Replacement |
|---|---|
| JWT, that is `eyJ....` header, payload, signature | `<jwt:redacted>` |
| `token`, `access_token`, `api_key`, `api-key`, `secret`, `password`, `authorization` followed by `=` or `:` and a value of 8 or more characters | `<redacted>` |
| `Bearer` followed by a value of 8 or more characters | `<redacted>` |
| `sk-`, `pk-`, `rk-` prefixed provider keys of 12 or more characters | `<key:redacted>` |
| `AKIA` followed by 16 uppercase alphanumerics | `<aws-key:redacted>` |
| `ghp_` followed by 20 or more alphanumerics | `<gh-token:redacted>` |

The kirocrew dashboard URL carries a signed JWT. `kirocrew_dashboard_url`
redacts the URL by default and reports `token_included: false`. The adapter
API accepts `include_token=True` for a deliberate opt-in, but the MCP tool
surface does not expose that switch, so a token never reaches a tool result, a
log line, or a commit. Redaction is pattern based, so it will not catch a
credential in an unfamiliar format. Treat adapter output as potentially
sensitive.

---

## 5. Why argv only

The adapter never invokes a shell. Every child process is launched with
`subprocess.run` against an argument list, never a command string. A prompt
containing `; rm -rf /` is therefore a prompt, not a command. Two supporting
rules. **Environment scrubbing:** `PYTHONPATH` and `PYTHONHOME` are removed from
the child environment, because a bundled interpreter that inherited
`PYTHONPATH` could otherwise import foreign code. **Process isolation:** child
processes start in a new session (`start_new_session=True`), so a timeout kill
targets the child process group rather than leaving orphans behind. ANSI escape
sequences are stripped from captured output.

---

## 6. Implementing the contract for a third harness

A new harness adapter must satisfy the following.

1. **Return `AdapterResult`.** Build it with `success` or `failure` from `kiro_adapters.result`.
2. **Never raise for ordinary failure.** A missing binary, a non-zero exit and
   a timeout are all results, not exceptions. Call `run` from
   `kiro_adapters.runner`, which already converts them.
3. **Map failures to the right code.** Binary missing is `NOT_INSTALLED`,
   readiness gate failure is `BLOCKED`, uninferable invocation is
   `UNSUPPORTED`, timeout is `TIMEOUT`, everything else is `ERROR`.
4. **Gate before spending.** If the action costs a model call and a precondition
   is unmet, return `BLOCKED` with the remediation in `hints` rather than
   starting the child process.
5. **Redact.** Run stdout, stderr, and any string inside `data` through
   `redact` or `redact_obj`.
6. **Resolve the binary.** Use `resolve_binary` with an environment override
   variable and an ordered candidate list, so the user can pin a path.
7. **Do not invent flags.** Probe the binary's own `--help` and only use a flag
   you actually observed, or accept an explicit argv template from the user.
8. **Expose it through the MCP server** under a `kiro<name>_` prefix, and add
   it to the `kiro_dispatch` routing table.

---

## 7. Surfaces that implement this contract

All modules below live in `src/kiro_adapters/`.

| Surface | Module | Notes |
|---|---|---|
| MCP server | `mcp_server.py` | JSON-RPC 2.0 over stdio, protocol `2024-11-05`, name `kiro-adapters`. Advertises the ten tools in section 2. A `tools/call` returns the envelope as tool text, with `isError` set when `ok` is false. |
| CLI | `cli.py` | `kiro-adapter` subcommands mirror the same actions and return the same envelope. `--json` prints the envelope, exit code is 0 when `ok` and 1 when not. |
| kirocrew adapter | `kirocrew.py` | Implements the kirocrew side. |
| kirocode adapter | `kirocode.py` | Implements the kirocode side. |

The jcode bridge lives in `src/jcode_bridge/` and returns the same envelope.

| Surface | Module | Notes |
|---|---|---|
| daemon-API backend | `api.py` | Spawns `jcode api --stdio` and maps harness-API outcomes onto the envelope. `harness` is `jcode-api`. |
| protocol client | `api_client.py` | NDJSON frame state machine, version-negotiated `hello`/`hello_ok`, session lifecycle, turn collection. |
| task backend | `backend.py` | One-shot `jcode run --json` and detached tasks. `harness` is `jcode`. |
| MCP server | `mcp_server.py` | Server name `jcode-bridge`, protocol `2024-11-05`, the three `jcode_api_*` tools plus seven task tools. |
| CLI | `cli.py` | `jcode-bridge` with `api-health`, `api-ask`, `api-sessions` and the task subcommands. `--json` is a global flag. |

The current verification status of each surface is recorded in `README.md`.
