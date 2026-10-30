# jcode Harness API Bridge

The `jcode_bridge` package has two halves.

- **Task backend.** `jcode_bridge.backend` shells out to `jcode run --json` for one
  answer, and runs detached tasks. Those surfaces are `jcode_health`, `jcode_run`,
  `jcode_task_start`, `jcode_task_status`, `jcode_task_output`,
  `jcode_task_cancel`, `jcode_task_list`.
- **Harness API client.** `jcode_bridge.api` spawns `jcode api --stdio` and speaks
  jcode's own versioned harness API. The caller keeps a *session*, not a process:
  assistant text streams as it arrives, tool calls and token usage are visible,
  and more than one turn can be sent before disconnecting.

This document covers the second half. It is the daemon API surface, from a
client's point of view.

---

## 1. What "the daemon API" actually is

`jcode api --stdio` is a child process that speaks newline-delimited JSON over
stdin/stdout. It is not HTTP, and there is no port. One child serves one
connection.

| Direction | Frame shape |
|---|---|
| client to daemon | `{"req": "<name>", ...}` |
| daemon to client | `{"v": 1, "reply_to": <u64>?, "ev": "<name>", ...}` |

`reply_to` appears only on direct replies. Streaming events carry no `reply_to`
and are correlated by `session_id`. The client does **not** hardcode a server
version: `hello` negotiates `min_version` / `max_version`, and a mismatch is a
protocol error rather than a silent downgrade.

Observed server identity on this machine, 2026-10-28:

```
server      jcode-harness-api-bridge/0.1.0
version     1
binary      /Users/kooshapari/.local/bin/jcode
capabilities sessions, streaming, text_framing, turn_stop_reasons, side_panel,
             persisted_session_discovery, runtime_info, api_key_provisioning,
             auth_changed_notification, usage_invalidation, session_archive,
             session_retention, session_files, session_fork, todo_state,
             session_connections, runtime_commands, session_tools, ...
```

Capability lists are additive. The client counts unknown event kinds and skips
them (`ignored_events` in the turn payload) rather than raising, because the
protocol promises that a server-side addition cannot break an existing client.

### Binary resolution

`JCODE_BRIDGE_BIN` overrides the binary path. Otherwise `PATH` lookup of
`jcode` is used. The argv is always `[<binary>, "api", "--stdio"]`; no shell is
involved and no credential is parsed, logged or echoed. `PYTHONPATH` and
`PYTHONHOME` are scrubbed from the child environment.

---

## 2. Protocol lifecycle of one `ask`

```
client                          daemon
  |  hello (min_version=1)  --->  |
  |  <--- hello_ok {server, version, capabilities}
  |  ping                    --->  |
  |  <--- pong                     |
  |  create_session          --->  |
  |  <--- attached {session:{session_id,...}}
  |  send_message            --->  |   (notify, no reply)
  |  <--- message_accepted         |   queued by the agent
  |  <--- text_delta ...           |
  |  <--- tool_start ...           |
  |  <--- tool_input_delta ...     |
  |  <--- tool_done ...            |
  |  <--- token_usage ...          |
  |  <--- turn_done                |
  |  detach_session          --->  |
  |  <--- detached                 |
```

Handled server events: `hello_ok`, `pong`, `attached`, `session_forked`,
`message_accepted`, `text_delta`, `tool_start`, `tool_input_delta`, `tool_done`,
`token_usage`, `permission_request`, `session_status`, `turn_done`.

Client requests: `hello`, `ping`, `create_session`, `attach_session`,
`detach_session`, `list_sessions`, `send_message`, `permission_response`.

### `send_message` is a notify, not a request

This is the one place the protocol is easy to get wrong, so it is stated
plainly:

> `send_message` is a **notify**. The daemon does not answer it with a reply
> frame. Waiting for one blocks until the turn times out.

The daemon acknowledges indirectly, by emitting a `message_accepted` event once
the agent has the message queued. The bridge therefore sends and returns
immediately, then reads the turn with `collect_turn`, which records
`message_accepted` as `accepted: true` alongside the text deltas, tool calls
and `turn_done`.

`permission_response` is the same shape: send only, the ack is ignored.

A `permission_request` that arrives with no `on_permission` callback is recorded
and left unanswered, so the turn normally stalls until it times out. That is
deliberate: silently granting a tool permission from a bridge would be a
privilege decision the caller never made.

---

## 3. CLI subcommands

`jcode-bridge` is the entry point. `--json` is a **global** flag and must
precede the subcommand: `jcode-bridge --json api-health`, not
`jcode-bridge api-health --json`. Exit code is 0 when the result is `ok` and 1
when it is not.

| Command | Arguments | Default timeout |
|---|---|---|
| `api-health` | `--timeout SECONDS` | 30 |
| `api-ask PROMPT` | `--cwd DIR`, `--timeout SECONDS`, `--keep-session` | 900 |
| `api-sessions` | `--limit N`, `--timeout SECONDS` | 30 |

```bash
# Handshake and ping only, no session
jcode-bridge --json api-health

# One turn over a real session
jcode-bridge api-ask "list the files in this repo" --cwd . --timeout 120

# Keep the session open for follow-ups
jcode-bridge --json api-ask "step one" --keep-session

# What sessions can the harness API see
jcode-bridge api-sessions --limit 10
```

`api-ask` prints the answer text in human mode; `--json` prints the whole
envelope.

---

## 4. MCP tools

Server name `jcode-bridge`, protocol `2024-11-05`, same shape as
`kiro_adapters.mcp_server`, standard library only.

| Tool | Required | Optional | Backend action |
|---|---|---|---|
| `jcode_api_health` | none | none | `JcodeApiBackend.health` |
| `jcode_api_ask` | `prompt` | `cwd`, `timeout_secs`, `keep_session` | `JcodeApiBackend.ask` |
| `jcode_api_sessions` | none | `limit`, `timeout_secs` | `JcodeApiBackend.sessions` |

Argument validation happens at the MCP edge: `timeout_secs` must be a finite
number in `(0, 86400]`, `limit` must be a positive integer, and a bad value is
reported as `ERROR` naming the offending key rather than reaching the daemon.

---

## 5. Result envelope

The same ten keys as every other adapter in this package, with `harness` set to
`jcode-api`. Codes: `OK`, `NOT_INSTALLED`, `BLOCKED`, `TIMEOUT`, `ERROR`,
`UNSUPPORTED`. See `docs/ADAPTER-CONTRACT.md` section 1; this section records
only what is specific to the API actions.

`data` per action:

| Action | `data` keys |
|---|---|
| `health` | `argv`, `resolve{binary,found,tried}`, `version`, `server`, `capabilities`, `ping` (bool), `stderr`?, `exit_code`? |
| `ask` | `argv`, `working_dir`, `keep_session`, `version`, `server`, `capabilities`, `session`, `turn{session_id,text,done,accepted,events,tool_calls,token_usage,permission_requests,statuses,ignored_events}`, `answer`, `answer_chars`, `turn_done`, `stderr`?, `exit_code`? |
| `sessions` | `count`, `sessions[]`, plus the hello keys, `stderr`?, `exit_code`? |

An `ask` that reads to EOF without `turn_done` is reported as `TIMEOUT`
("turn ended without turn_done"), not `OK`. A spawn failure is `NOT_INSTALLED`;
a deadline overrun is `TIMEOUT`; a protocol violation is `ERROR`.

### `sessions` bounds the result client-side

The released daemon this bridge is tested against (`jcode 0.99.1`) ignores the
`limit` field of `list_sessions` and returns every session. The bridge applies
the bound itself, newest first by `updated_at_ms` / `last_active_at_ms`, so
`limit` means the same thing on every daemon version.

---

## 6. Verification status

Measured 2026-10-28 on this machine, macOS arm64, `jcode 0.99.1`.

| Surface | Result |
|---|---|
| `api-health` | `OK`. `detail: "jcode-harness-api-bridge/0.1.0 (1)"`; handshake negotiated version 1, `ping: true`, binary resolved to `/Users/kooshapari/.local/bin/jcode`. |
| `api-ask` | `OK`. Returned `BRIDGE_OK` and `JSON_OK` with the full streaming envelope: `message_accepted`, ordered `text_delta`s, `turn_done`, `answer`, `answer_chars`, and `turn_done: true`. |
| `api-sessions` | Exercised against the same daemon; `limit` enforced client-side as described above. |
| MCP tools | Advertised by `jcode-bridge-mcp --selftest`; covered by `tests/test_jcode_bridge_wiring.py` against a fake handshake reporting `jcode-harness-api-bridge/0.1.0`. |
| Kiro Crew calls jcode | `OK`, end to end. A Kiro Crew agent invoked `@jcode-bridge/jcode_api_ask` and received the answer `KIRO_BRIDGE_OK` from jcode. The call is recorded as an auto-approved tool invocation in `~/.kiro/crew/security_events.jsonl`, and the answer appears in the task-runner transcript. |
| Kiro Crew runs ON jcode | `OK`, end to end. Two Kiro Crew sessions with `CODEX_ACP_BIN=~/bin/jcode-acp-shim` completed their task and answered `JCODE_BACKEND_OK`. The security ledger records `governance_decision / agent_backend:codex / allowed`, and the transcript shows the answer. |
| Read-only enforcement | `OK`, proved at the daemon level. With `write`, `edit` and `bash` disabled those tools are absent from the session's tool definitions while `read` is still offered (`crates/jcode-app-core/src/tool/tests/sdk.rs`). |

Re-run the health probe before trusting any of the above on a different host:

```bash
PYTHONPATH=src python3 -m jcode_bridge.cli --json api-health
```

### The reproducible probe

`scripts/acceptance/daemon_api_probe.py` drives the whole protocol end to end
against any jcode build you point it at: `hello`, `ping`, `create_session`, an
optional `set_model`, one turn, and `detach_session`. It exits 0 only when the
turn completed (`turn_done` observed) with a non-empty answer.

```bash
make daemon-api-probe JCODE=/path/to/jcode
```

The provider and model come from the environment, because the operator's
configured default may be out of credit, which is an account problem and not a
protocol problem:

```bash
JCODE_PROBE_PROVIDER=openai-compatible \
JCODE_PROBE_MODEL=beam-free \
make daemon-api-probe JCODE=/path/to/jcode
```

Measured 2026-10-30 against `/Users/kooshapari/.local/bin/jcode` with
`openai-compatible` / `beam-free`:

```
HELLO_OK server=jcode-harness-api-bridge/0.1.0 version=1 caps=20
PING_OK
SESSION_CREATED id=session_cow_... (via session_status)
MODEL_SET beam-free
TURN accepted=True done=True events=26 text_chars=15 tools=0 tokens=1
ANSWER: DAEMON_PROBE_OK
```

Two protocol details the probe depends on:

- `send_message` is a **notify**. The daemon does not reply to it. It
  acknowledges with a `message_accepted` event, and waiting for a reply to
  `send_message` blocks until the turn times out.
- The turn's answer arrives as `text_delta` events followed by `text_done` and
  `turn_done`. A model that answers only in the reasoning channel produces
  `reasoning_delta`s and no `text_delta`, which is a model problem, not a
  protocol one.

### How a Kiro Crew agent reaches jcode

MCP entries *are* the arbitrary-command slot. `agent.acp_backend` is the wrong
lever; the right one is the MCP server list, which is merged into the agent
spec with the highest precedence:

```json
{
  "mcpServers": {
    "jcode-bridge": {
      "args": [],
      "command": "/Users/kooshapari/bin/jcode-bridge-mcp",
      "type": "stdio"
    }
  }
}
```

That goes in `~/.kiro/crew/mcp.json` (the dashboard store, which survives a
rebuild). A rebuild then writes the entry into `~/.kiro/agents/kirocrew.json`
and adds `@jcode-bridge` to both `tools` and `allowedTools`; without the
`@server` entry in `tools` the server is mounted but not callable. A rebuild is
triggered by `kirocrew setup --agent-only`.

A dedicated spec (`~/.kiro/agents/kirocrew-jcode.json`) that names the server in
its own `mcpServers` and `tools` works too, and is not overwritten by a
rebuild; `kirocrew agent create --name <member> --kiro-agent kirocrew-jcode`
then points a crew member at it.

Note that `jcode_run` is synchronous and can be slow, so prefer
`jcode_task_start` plus `jcode_task_status` / `jcode_task_output` for work that
takes a while.

---

## 7. Kiro Crew's backend slots, and what jcode fills

Kiro Crew's `agent.acp_backend` is a **closed enum**. It selects from a fixed
set of backends the application ships with; it is not an arbitrary command slot.
So there is no config value to set to "run Kiro Crew on jcode".

What changed is that the enum was never the only lever. Each backend slot
resolves the ACP binary it launches through an **operator override**:

| Slot | Override | Shape |
|---|---|---|
| `codex` | `CODEX_ACP_BIN` | an executable file, run **as-is** with no arguments |
| `opencode` | `OPENCODE_BIN` | an executable file, run as `<binary> acp` |

The `codex` slot's permission routing is `SESSION_CONFIG`, so a harness in
that slot is only accepted when it advertises a `mode` config option and
accepts `mode=read-only`. jcode's ACP now implements exactly that, so the
slot's own gate is satisfied by a real, enforced restriction.

`~/bin/jcode-acp-shim` is that override. It execs `jcode acp`, and it is the
only piece of glue:

```bash
CODEX_ACP_BIN=~/bin/jcode-acp-shim kirocrew run TASK.md
```

The override is exported from `~/.zshrc`, so a Kiro Crew session started from
a login shell picks it up without any per-command configuration. It points at
a **versioned** jcode build (`~/.jcode/builds/versions/0.99.1-kcode-mode/jcode`)
rather than a build tree, so a rebuild or a `cargo clean` cannot leave the
shim pointing at nothing.

One gap worth knowing: `kirocrew service install` bakes a fixed set of
variables into the launchd plist, and `CODEX_ACP_BIN` is **not** among them
(`service/common.py::service_environment`). A session started by the
installed service therefore does not get the override; a session started from
a shell does.

Consequences, stated without hedging:

- **jcode can serve as Kiro Crew's ACP backend.** The agent Kiro Crew spawns is
  jcode, and it says so: `agentInfo.name` is `jcode`.
- **The slot id in the logs says `codex`.** That is the *slot* Kiro Crew was
  told to use, not a claim about the agent. The agent reports itself as jcode.
- **The only mode is `read-only`.** A Kiro session running on jcode through
  this slot cannot write: every write-capable tool is disabled in the
  session's tool set, so it is not offered to the model at all. A mode this
  agent does not implement is refused rather than accepted and ignored.
- **Kiro Crew can also call jcode as a tool.** The reverse direction works
  too: jcode (or any MCP client) dispatches work into Kiro Crew over the
  `kirocrew` CLI, and jcode itself is reachable over its harness API from an
  agent that chooses to call it.

So the relationship is now available in both directions, and each direction is
labelled with what it actually is.

### Verified

Two Kiro Crew sessions run with `CODEX_ACP_BIN=~/bin/jcode-acp-shim` completed
their task and answered `JCODE_BACKEND_OK`, and Kiro Crew's security ledger
recorded `governance_decision / agent_backend:codex / allowed` for the
backend. The read-only denial is proved at the daemon level: with `write`,
`edit` and `bash` disabled those tools are absent from the session's tool
definitions while `read` is still offered.

---

## 8. Security notes

- No shell. Every child process is launched from an argument list.
- No credential is parsed, logged or echoed. The child inherits the caller's
  environment minus `PYTHONPATH` / `PYTHONHOME`.
- Every string in the envelope passes through `redact` before it leaves the
  process. Captured stderr is truncated and redacted the same way.
- Redaction is pattern based, so it will not catch a credential in an
  unfamiliar format. Treat adapter output as potentially sensitive.
