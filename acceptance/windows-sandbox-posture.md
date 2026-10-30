# Windows sandbox posture: `agent.sandbox_allow_unsandboxed_exec = false`

Measured 2026-10-30 on host `desk` (`koosh@100.96.135.160`, Windows 11 build 28120),
KiroCrew installed at `C:\Program Files\KiroCrew`, backend
`C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend`.

## What was set

`C:\Users\koosh\.kiro\crew\config.json`:

```
agent.sandbox_allow_unsandboxed_exec = False
```

Read back from the live file, not from a plan or a patch:

```
=== CONFIG KEY ===
sandbox_allow_unsandboxed_exec = False
```

## What the setting does when it is enforced

It is live, and it is fail-closed. A real chat invocation
(`kirocrew chat --agent kirocrew-guest -m '...'`) exits **1** with:

```
kiro_crew.sandbox.SandboxUnavailableError: Sandbox backend unavailable and
agent.sandbox_allow_unsandboxed_exec is set to false on this host, so
unsandboxed execution stays refused. Change that key to true (or remove it to
accept this platform's default) if you want these spawns to run unconfined.
No OS-level sandbox backend is available on this host, and the agent
subprocess cannot be safely isolated. Probe detail: not Linux.
```

Traceback origin: `kiro_crew/providers/acp_lifecycle.py` ->
`sandbox.py:4993 wrap_argv_async` -> `sandbox.py:4879 wrap_argv` ->
`SandboxUnavailableError`.

## The consequence, stated plainly

On Windows this install has **no sandbox backend** ("Probe detail: not Linux").
So `false` does not mean "sandboxed"; it means **the harness cannot start an
agent at all**. The Windows pilot that previously returned `PONG` in 29.5 s no
longer runs while this key is `false`.

This is the honest cost of the fail-closed choice, and it is a choice:

| value | behaviour on this Windows host |
|---|---|
| `false` | agent execution refused; `chat` fails with `SandboxUnavailableError`; nothing runs unconfined |
| `true` | agent execution runs **unconfined**; `chat` works as before |
| absent | platform default applies — not measured here |

`true` is not a security improvement over `false`; it trades the refusal for a
working but unisolated agent. Neither state provides isolation, because the
platform cannot provide it.

Reversible with one edit to the `agent` object in
`C:\Users\koosh\.kiro\crew\config.json`; no reinstall required.

## What is NOT claimed

- Not claimed: that Windows KiroCrew is sandboxed. It is not, in any state.
- Not claimed: that `true` is unsafe in a way `false` is not. `false` merely
  refuses; it does not isolate.
- Not claimed: that the public edition has a jail. Its own `--no-jail` help text
  says the flag is "no-op on the public edition, which has no jail backend",
  which is consistent with the measured refusal above.

## Credential inventory (asked for alongside Q1)

Not performed in this pass. Enumerating `~/.kiro/crew/.env`, the KAS vault at
`C:\Users\koosh\.kiro\crew\kas\.vault`, and the dashboard token store was
deferred rather than half-done: reading them is cheap, but reporting them
without leaking secret values needs its own redaction pass. Status: **NOT_RUN**.
