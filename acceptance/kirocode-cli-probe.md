# Kiro Code adapter: the harness now resolves, and the probe bug it exposed

Measured 2026-11-02 on this host. The Kiro Code adapter previously reported
`NOT_INSTALLED` because no Kiro coding CLI existed here. That is no longer true,
and installing the real binary exposed a genuine adapter defect.

## The harness exists and is installable

"Kiro Code" in this repo's vocabulary is the Kiro command-line product. It ships
as a Homebrew cask, which is how it was installed:

```
$ brew info kiro-cli
==> kiro-cli (Kiro CLI): 2.35.2 (auto_updates)
AI-powered productivity tool for the command-line
https://kiro.dev/docs/cli/
==> Artifacts
Kiro CLI.app (App)
/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli (Binary)

$ brew install --cask kiro-cli
🍺  kiro-cli was successfully installed!

$ command -v kiro-cli && kiro-cli --version
/opt/homebrew/bin/kiro-cli
kiro-cli 2.37.1
```

There is a separate `kiro` cask for the IDE ("Agent-centric IDE with
spec-driven development", https://kiro.dev/). The adapter targets the CLI, which
is the `kiro-cli` name already in `BINARY_CANDIDATES`.

Reverse with `brew uninstall --cask kiro-cli`.

## The probe bug this exposed

Before installing, the adapter's status probe was untestable. Afterwards it
resolved the binary but reported that the CLI had **no non-interactive mode**:

```
"capabilities": {
  "has_chat": true,
  "has_exec": false,
  "supports_no_interactive": false,
  "supports_print": false,
  "help_bytes": 1913
},
"ask_argv_template": ["/opt/homebrew/bin/kiro-cli", "chat", "ping"]
```

That argv is wrong in a way that hangs: `kiro-cli chat <prompt>` without
`--no-interactive` drops into a TUI and waits for a human, so an agent call
would block until its timeout instead of failing fast.

The flag does exist. It is documented on the **subcommand**, not at the top
level:

```
$ kiro-cli chat --help
      --no-interactive
          Whether the command should run without expecting user input
```

`capabilities()` was testing `"--no-interactive" in self.help_text()`, and
`help_text()` runs `<binary> --help` only. Top-level help lists subcommands but
not their flags, so the check could never pass for any CLI shaped this way.

### Fix

`subcommand_help_text(subcommand)` runs and caches `<binary> <subcommand>
--help`, and `capabilities()` folds that text in before looking for flags. The
top-level `help_bytes` is kept as-is and a separate `chat_help_bytes` is
reported, so a reader can see which help produced the verdict.

After the fix:

```
"capabilities": {
  "has_chat": true,
  "supports_no_interactive": true,
  "help_bytes": 1913,
  "chat_help_bytes": 4129
},
"ask_argv_template": ["/opt/homebrew/bin/kiro-cli", "chat", "--no-interactive", "ping"]
```

## Where it stops: authentication

The corrected invocation now runs, and fails only on credentials:

```
$ kirocode-ask "Reply with exactly: KIRO_OK"
"argv": ["/opt/homebrew/bin/kiro-cli", "chat", "--no-interactive", "Reply with exactly: KIRO_OK"]
"exit_code": 1
"stderr": "Not logged in. Set the KIRO_API_KEY environment variable or run `kiro-cli login` first."
```

That is an operator action, not an adapter defect: either
`export KIRO_API_KEY=...` or run `kiro-cli login` interactively. Until then
`verified_live` stays `false` and the adapter is wired correctly but unproven
end to end.

## Test suite

```
$ PYTHONPATH=src python3 -m unittest discover -s tests -t .
Ran 367 tests in 29.394s

OK
```

## What this does and does not establish

Established by execution:

- A real Kiro CLI resolves, and its version is read (`kiro-cli 2.37.1`).
- The non-interactive flag is found, and the generated argv is the correct one.
- The ask path builds that argv and executes it.

Not established:

- Any successful Kiro Code turn. Blocked on authentication.
- Anything about Kiro Code's ACP behaviour. This adapter drives the CLI as a
  subprocess; there is no ACP layer for Kiro Code, unlike the Kiro Crew path.

## Why there is no "Kiro Code uses jcode as its backend" mode

Recorded 2026-11-02 after attempting it, because the symmetric goal — make the
Kiro coding CLI run on jcode, the way Kiro Crew does — is **not achievable with
kiro-cli 2.37.1**, and it is worth not rediscovering.

Kiro Crew supports it because it honours an unpinned operator override,
`CODEX_ACP_BIN`, and only consults it for the `codex` slot. `kiro-cli` has no
equivalent. What it has, and why each is not the answer:

| Surface | What it is | Why it does not help |
|---|---|---|
| `KIRO_CLI_ACP_CLI` (env var) | Present in the binary, and the obvious candidate | The login gate is evaluated **first**. Setting it and running `kiro-cli chat --no-interactive` still prints `Not logged in`, and a wrapper named by it was **never invoked** (verified with an invocation-logging shim). |
| `kiro-cli acp` | A real ACP **agent** server; it answers `initialize` | It advertises `authMethods: [{"id": "kiro-login", ...}]` and empty `sessionCapabilities`, so it cannot serve a session without Kiro credentials. It is an agent to be driven, not a client that delegates. |
| `acp.agentEngine` | The only `acp.*` setting (`v1`\|`v2`\|`v3`) | Selects one of Kiro's **own** engines, not an external one. |
| `external_acp` | Appears in the binary next to `interactive_cli` / `noninteractive_cli` | It is a **telemetry label** for `session_interface`, not a selectable mode. |
| `kiro-cli serve` | Persistent WebSocket server | "for the v3 engine" — still Kiro's engine. |
| `~/.kiro/settings/cli.json` | Settings file | Contained `{}` (2 bytes). No provider, endpoint, or BYO-model key exists to set. |

`kiro-cli --help` lists `chat`, `acp` (hidden), `serve`, `agent`, `login`,
`settings`, and so on; every inference path terminates at the same Kiro login
check.

### Consequence

Without Kiro's API there is nothing for this adapter to *drive*. A request to
`kiro-cli chat --no-interactive <prompt>`, correctly built, terminates in:

```
Not logged in. Set the KIRO_API_KEY environment variable or run `kiro-cli login` first.
```

So the adapter's honest scope is **detection and capability probing**: resolve
the binary, read its version, and report the auth state so an operator can see
why a turn would fail. `ask()` remains implemented and correct — it builds a
valid invocation and surfaces the CLI's own error — but it cannot succeed
unauthenticated, and `verified_live` stays `false` to say so.

If Kiro credentials are ever acceptable, `ask()` should work unchanged; the
adapter needs no rework, only authentication.

### The asymmetry worth stating plainly

- **Kiro Crew → jcode: works, and uses no Kiro API.** The shim pins
  `-p opencode-go -m deepseek-v4.1-flash`, so Kiro Crew's turns run through
  jcode on the opencode-go pool. Verified by live turns returning
  `JCODE_BACKEND_OK`.
- **Kiro Code → jcode: not possible.** Not a missing adapter feature — the
  harness offers no injection point that is not gated behind its own login.
