# Operations

Day-2 runbook for the Kiro adapters. Everything here is a real command observed
on this machine against `kirocrew 0.11.0-nightly.20261021t201456`, or a real
file the adapters read. Nothing is speculative.

---

## 1. kirocrew readiness

The adapter runs two checks before it spends a model call, plus one for
subagents. Run them by hand whenever a call comes back `BLOCKED`.

### 1.1 Is the CLI there?

```bash
kirocrew --version
which kirocrew
```

If the binary is not on `PATH`, the adapter returns `NOT_INSTALLED`. It also
looks at `KIROCREW_BIN` first, so pinning the launcher is enough:

```bash
export KIROCREW_BIN=/Users/kooshapari/.local/bin/kirocrew
```

### 1.2 Is the gateway running?

```bash
kirocrew status
```

A healthy gateway prints:

```
Kiro Crew gateway is running (token auth enabled).
```

The dashboard binds loopback only, on `http://localhost:5476` by default.
Start it in the foreground with:

```bash
kirocrew gateway
```

Stop it with `kirocrew stop`, restart with `kirocrew restart`, and find the
process holding the gateway lock with `kirocrew gateway-pid` (JSON output).

For a long-lived gateway, install the background service instead. On macOS
this registers a launchd agent, on Linux a systemd unit that needs sudo:

```bash
kirocrew service install
kirocrew service status
kirocrew logs
```

Run `gateway` and `service` one at a time. Both bind the same port.

To move the dashboard off the default port, use `kirocrew gateway --port N`,
`KIROCREW_PORT=N`, or the `dashboard.url` config value.

### 1.3 Is the KAS engine bundle installed?

```bash
kirocrew kas status
```

Healthy output names an installed engine and the route it uses:

```
bundle route:  on  (agent.kas_bundle)
pinned:        KAS 0.66.33; runs 0.66.33
installed:     KAS 0.66.33 is installed
```

When the bundle is missing, the `installed:` line says so and names the fix:

```
installed:     KAS 0.66.33 is not installed  (run `kirocrew kas install`)
```

Fix it with:

```bash
kirocrew kas install
```

The engine bundle lives under `~/.kiro/crew/engines/kas` and is fetched from the
manifest base `https://kas.crew.kiro.dev`.

### 1.4 Which agents exist, and is the install healthy?

Agent definitions are JSON files in `~/.kiro/agents`. The adapter reads them
directly, with no model call and no CLI invocation:

```bash
kiro-adapter agents
kirocrew doctor
kiro-adapter doctor
```

---

## 2. `NOT_INSTALLED` versus `BLOCKED`

These are the two codes that get confused. The rule is simple: `NOT_INSTALLED`
means the tool is absent, `BLOCKED` means the tool is present but not ready.

| Code | Means | Exact remediation |
|---|---|---|
| `NOT_INSTALLED` | The `kirocrew` binary could not be found or launched. | Install Kiro Crew, or `export KIROCREW_BIN=/path/to/kirocrew`. |
| `NOT_INSTALLED` | The `kirocode`, `kiro-cli` or `kiro` binary could not be found. | Install it, set `KIROCODE_BIN`, or write `~/.kirocode/adapter.json`. |
| `BLOCKED` | The KAS engine bundle is not installed. | `kirocrew kas install`. |
| `BLOCKED` | The gateway is stopped and the action needs it. Only `spawn` needs it. | `kirocrew gateway`, or `kirocrew service install`. |
| `UNSUPPORTED` | A Kiro coding CLI was found but no non-interactive call could be inferred. | Write an argv template in `~/.kirocode/adapter.json`, see section 4. |

`ask` and `run_spec` do not need the gateway. Only `spawn` does.

The remediation text for a failed action is in the `hints` array of the result,
so a client can show it without a lookup table. A command that exits zero can
still carry an empty `hints` array, so for readiness read `data.ready` and
`data.kas.installed` rather than `hints`.

---

## 3. The dashboard token

`kirocrew token` prints a dashboard URL that carries a signed JWT. That URL is
a credential. The adapter redacts it, and the MCP tool surface never exposes
the raw value.

```bash
kiro-adapter dashboard            # redacted URL
kiro-adapter dashboard --ttl 30m  # shorter lived token, still redacted
```

To open the dashboard by hand, print the URL yourself with `kirocrew token`.
Do not paste that URL into a chat, a log, a commit, or a shared channel.

---

## 4. kirocode argv profiles

The Kiro coding CLI was not installed on this machine when this package was
written, so the adapter cannot know its flags. Instead of guessing, it probes
the binary's own `--help` and picks an invocation the binary advertises. If
that fails, or if the CLI has a shape the probe does not recognise, write an
explicit profile at `~/.kirocode/adapter.json`:

```json
{
  "bin": "/path/to/kiro-cli",
  "argv": ["chat", "--no-interactive", "{prompt}"]
}
```

| Key | Meaning |
|---|---|
| `bin` | Path to the binary. Used only when `KIROCODE_BIN` is unset. A bare name is looked up on `PATH`. |
| `argv` | The argument list template. `{prompt}` becomes the prompt, `{bin}` becomes the binary path. The resolved binary is prepended automatically unless the template already contains `{bin}` or starts with an absolute path. |

Resolution order: `KIROCODE_BIN` environment variable, then the `bin` key,
then a `PATH` lookup of `kirocode`, `kiro-cli`, `kiro`.

Check the result with `kiro-adapter kirocode-status --json`. The `status`
output reports the resolved binary, its version, the probed capabilities, and
whether the argv template came from the config file or from the help probe.
When the config file exists but cannot be parsed, the adapter records an
`_error` entry and keeps working with the default probe.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `NOT_INSTALLED`, `kirocrew CLI not found` | The binary is not on `PATH`. | `export KIROCREW_BIN=/path/to/kirocrew`, or install Kiro Crew. |
| `NOT_INSTALLED` from a kirocode action | No `kirocode`, `kiro-cli` or `kiro` binary was found. | Install it, set `KIROCODE_BIN`, or write `~/.kirocode/adapter.json`. |
| `BLOCKED`, hint `Agent engine missing` | The KAS engine bundle is not installed. | `kirocrew kas install`. |
| `BLOCKED`, hint `Gateway stopped` | The action is `spawn`, which needs the gateway. | `kirocrew gateway`, or `kirocrew service install`. |
| `BLOCKED` on `ask` or `run_spec` | The KAS readiness gate failed. Neither action needs the gateway. | `kirocrew kas install`. |
| `UNSUPPORTED` from `kirocode_ask` | The binary was found but no non-interactive call could be inferred. | Write `~/.kirocode/adapter.json` with an `argv` template. |
| `TIMEOUT` | The child exceeded the timeout and was killed. `ask` defaults to 900s. | Raise `timeout_secs`, or `--timeout` on the CLI. `run_spec` defaults to 3600s. |
| `TIMEOUT` on a read-only probe | `kirocrew` startup was 12s to 46s here, and the short timeout is 30s. | Retry, then raise the timeout for that call. |
| `ERROR`, `spec file not found` | The spec path does not resolve to a file. | Pass an existing path. `~` is expanded. |
| `ERROR`, `message must not be empty` | An empty prompt was sent. | Send a non-empty prompt. |
| `ERROR`, `kirocrew exited N` | The CLI ran and failed. | Read `stdout` and `stderr`. `kirocrew doctor` often explains it. |
| Result says `unknown harness` | `kiro_dispatch` got a harness other than the two. | Use `kirocrew` or `kirocode`. |
| Result says `unknown tool` | The tool name is not in the contract. | See the tool table in `docs/ADAPTER-CONTRACT.md`. |
| `status` is `ok` but `hints` is empty | Hints attach only to a failing command, and `kirocrew status` exits zero. | Read `data.kas.installed` and `data.ready`. |
| Dashboard URL shows `<jwt:redacted>` | This is the intended behaviour. | Open the dashboard with `kirocrew token` in a terminal. |
| A child process has import errors | `PYTHONPATH` or `PYTHONHOME` leaked into the child. | The adapter scrubs both, so the leak came from elsewhere. |
| The MCP server prints nothing on stdout | Only protocol frames go to stdout. | Read stderr. Run `kiro-adapter-mcp --selftest`. |
| The MCP server dies on a bad tool call | It should not. Every call is wrapped. | Capture stderr and report it, that would be a bug. |
