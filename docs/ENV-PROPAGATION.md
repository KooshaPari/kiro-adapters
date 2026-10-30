# `CODEX_ACP_BIN` propagation: measured, not assumed

Question: if the `codex` ACP slot is pointed at a jcode shim through
`CODEX_ACP_BIN`, which launch paths actually carry that variable to the process
that spawns the agent?

Answer, measured against the installed Kiro Crew backend on this host:

| Launch path | Carries `CODEX_ACP_BIN` | Evidence |
|---|---|---|
| Login shell (`~/.zshrc` export, then `kirocrew ...`) | **YES** | the export is in the shell's environment; the CLI inherits it |
| `kirocrew service install` (launchd plist / systemd unit) | **NO** | `service_environment()` builds a fixed whitelist |
| Desktop app launched from Finder / Dock | **NO** | a GUI launch does not read `~/.zshrc` |
| Desktop app launched from a terminal that exported it | **YES** | inherited from the terminal process |

## Why the service path drops it

`kiro_crew/service/common.py:152` `service_environment(home)` is the single
source of truth for both the launchd `EnvironmentVariables` and the systemd
`Environment=` lines. It does **not** inherit the installer's environment. It
returns exactly:

```python
env = {
    "HOME": home,
    "PATH": service_path(home),
    "LANG": utf8_locale,
    "LC_ALL": utf8_locale,
    "KIROCREW_SERVICE_MANAGED": "1",
}
# optional, captured from the installer's environment:
#   KIROCREW_KIRO_BIN, KIROCREW_PORT
```

The docstring states the reason the list is closed: launchd and systemd start a
service with a minimal, non-login environment, so anything the gateway needs
must be captured explicitly at install time. That is a deliberate design, not a
bug — and the same file explains, for `KIRO_API_KEY`, why a credential is
*deliberately* kept out of this function (both baked locations are readable by
every local user).

So `CODEX_ACP_BIN` set in `~/.zshrc` is silently absent under
`kirocrew service install`. The `codex` slot then resolves its own default
binary instead of the shim, with no error: the agent that runs is not the agent
the operator pointed at.

## What follows, honestly

- The jcode-backed `codex` slot works today because the sessions that proved it
  were started from a login shell.
- A service-installed gateway is a **different** launch path and is
  **UNVERIFIED** for this override. It should be treated as not carrying it
  until measured.
- Nothing was changed to "fix" this. Adding an arbitrary env pass-through to
  `service_environment` would defeat the closed-list property the function was
  written to have, and would need its own decision about which variables are
  safe to bake into a world-readable unit file.

## If someone does want it carried

The narrow, reviewable options, in order of preference:

1. **Measure first.** Run `kirocrew service install` on a host where the
   override matters, inspect the generated plist/unit for the variable, and
   record the result. Today that result is UNKNOWN.
2. **Use the existing extension point if one exists.** `KIROCREW_KIRO_BIN` and
   `KIROCREW_PORT` are captured by name. ACP binary overrides are not. Adding
   one more named capture is in keeping with the existing design; a general
   pass-through is not.
3. **Do not bake a path a later update can prune.** The service unit outlives
   the process that wrote it, which is the same reasoning `kirocrew_bin()`
   already applies when it resolves through a stable link.
