# Kiro Crew → jcode on native Windows

What was installed on the Windows desk, what was proven, and the one thing this
platform cannot give you.

## Result

```
PASS  kiro-crew-home       C:\Users\koosh\.kiro\crew
PASS  acp_backend          agent.acp_backend = "claude"
PASS  slot-usable          slot "claude" is not enforced (spawns without a sandbox "none")
PASS  override-var         CLAUDE_AGENT_ACP_BIN=C:\Users\koosh\bin\jcode-acp-shim.cmd (process)
PASS  agent-env            agent.env.CLAUDE_AGENT_ACP_BIN agrees
PASS  override-file        C:\Users\koosh\bin\jcode-acp-shim.cmd
PASS  override-runnable    directly runnable on Windows
PASS  jcode-binary         C:\Users\koosh\AppData\Local\jcode\bin\jcode.exe
PASS  provider-model       -p opencode-go -m deepseek-v4.1-flash
PASS  acp-handshake        jcode 0.99.1 speaks ACP through the shim
PASS  live-turn            Kiro Crew answered VERIFY_094CA8D5

RESULT: PASS
```

Turns run on `opencode-go` / `deepseek-v4.1-flash`, i.e. jcode's own route. No
Kiro API is involved.

## The shape of the install

Three pieces, and they are not interchangeable:

1. **`%USERPROFILE%\bin\jcode-acp-shim.cmd`** execs
   `jcode.exe acp -p opencode-go -m deepseek-v4.1-flash`. A `.cmd` works because
   the resolver treats `.cmd` on Windows as directly runnable
   (`platform_compat.is_executable_file` returns `True`) and spawns it as-is.
   Verified against the installed build rather than assumed.
2. **`agent.acp_backend`** names the slot whose override we control.
3. **The override variable in the process environment.** This is the piece that
   is easy to get wrong, and it has its own section below.

## What was measured, in order

Each step failed at least once. The failures are the documentation.

| Observation | Meaning |
|---|---|
| `.env` is absent on this host; config carries `agent: {kas_bundle, acp_backend:"kas"}` | Nothing was half-configured; the slot was explicitly `kas` |
| `is_executable_file(shim.cmd)` → `True`, resolver → `['...jcode-acp-shim.cmd']` | A `.cmd` shim is the supported shape on Windows |
| ACP `initialize` through the shim → `agentInfo.name = "jcode"`, version `0.99.1` | The transport genuinely works, independent of Kiro Crew |
| Config set to `codex` + `agent.env.CODEX_ACP_BIN` → turn failed `codex-acp not found` | **`agent.env` does not reach the resolver** |
| Same config, `CODEX_ACP_BIN` in the process env → `AcpToolGateUnroutable: ... no supported OS sandbox backend ... changing agent.sandbox cannot enable it` | The `codex` slot is **refused** on native Windows |
| `is_enforced()` → codex/opencode/deepseek/goose/pi `True`; claude/kas/kiro `False` | Only `claude` can host a third-party binary here |
| `detect_backend(mode)` → `none` for `off`/`standard`/`strict`/`auto` | There is no sandbox backend to enable, at any setting |
| `acp_backend="claude"` + `CLAUDE_AGENT_ACP_BIN` → live turn answered | Installed and proven |

## `agent.env` is a trap for this install

Kiro Crew's config has an `agent.env` map and it looks like the right place for
`CODEX_ACP_BIN`. It is not, for this purpose:

- The resolver reads `os.environ.get(override_env)` **in the parent process**.
- `agent.env` is applied to the **child** afterwards.

So `agent.env` alone produces a turn that dies with `codex-acp not found` while
every config surface looks correct. The variable has to be in the process
environment, which for the GUI app means a persisted user variable (`setx`)
inherited at launch. The installer sets both, and the verifier distinguishes
them: a value present for the *user* but not in the *process* is reported as a
WARN, not a PASS, because that shell would still fail.

## The cost, stated plainly

The `claude` slot is **not enforced**, and this host has **no OS sandbox
backend**. Two consequences, both real:

- Kiro Crew does **not** force `mode=read-only` on this slot. The read-only
  property measured on macOS came from the `codex` slot's `SESSION_CONFIG`
  routing; that guarantee does not transfer here.
- The agent subprocess runs **without credential isolation**. Kiro Crew says so
  itself at spawn:

  > SECURITY: this platform offers no OS-level sandbox backend for Kiro Crew to
  > apply (mode=auto), so the agent subprocess runs WITHOUT credential isolation
  > — ~/.aws, ~/.ssh and other secrets are readable by it

This is a Kiro Crew platform property, not something a shim can fix, and not
something this repo should work around. Treat the Windows install as
**convenient, not contained**. If that trade is unacceptable, the honest options
are to run Kiro Crew where a sandbox backend exists, or to set
`agent.sandbox_allow_unsandboxed_exec=false` to refuse these spawns instead.

## Reproducing

```powershell
# safe: reports the slot it would choose and changes nothing
.\scripts\install\install-kirocrew-jcode-backend.ps1 -DryRun

.\scripts\install\install-kirocrew-jcode-backend.ps1
.\scripts\install\verify-kirocrew-jcode-backend.ps1
.\scripts\install\verify-kirocrew-jcode-backend.ps1 -Live
```

The installer asks the installed build which sandbox backend exists and picks
the slot from that answer, so it does not need a per-host decision. It also
removes the other slot's override, so a config can never name a slot that
cannot run.

## Three bugs this verifier had, and why they are recorded

All three were in the verification tooling, not the install, and each made a
working setup look broken:

1. **Reading the last output line.** Windows `asyncio` prints
   `ValueError: I/O operation on closed pipe` from `__del__` *after* the verdict.
   Taking the tail turned a passing handshake into a bogus FAIL. Fixed by
   matching `RESULT:` across all output and by reaping the child.
2. **Unbounded cleanup.** The first fix added `await proc.wait()`, which can
   itself hang forever; it left orphaned verifiers holding the output file.
   Now bounded to 10s.
3. **Nested quoting.** `Start-Process -ArgumentList` around a launcher under
   `C:\Program Files\` split into `"C:\Program"`. Fixed by generating a `.bat`.

A verifier that reports a false FAIL is worse than no verifier, which is why
these are fixed rather than noted.
