# Rolling back the Kiro Crew -> jcode backend on Windows

**One command reverses it, and one command makes the safe posture permanent.**
Before this file existed there was no reverse path at all, so the choice below was
effectively irreversible; that was the real defect, not the choice itself.

## Current posture (as recorded, not as assumed)

`acceptance/windows-desk-backend.md:70-89` records what was installed on `desk`:

- `agent.acp_backend = "claude"`. The `claude` slot is **not enforced**, so Kiro
  Crew does **not** force `mode=read-only`.
- `CLAUDE_AGENT_ACP_BIN` is persisted as a user variable, inherited by every
  process started afterwards.
- The agent subprocess runs **without credential isolation**. Kiro Crew says so
  itself at spawn: `~/.aws`, `~/.ssh` and other secrets are readable by it.
- `detect_backend()` returns `none` for `off`/`standard`/`strict`/`auto`, so
  there is **no OS sandbox backend on this host at any setting**. The enforced
  slots (`codex`/`opencode`/`deepseek`/`goose`/`pi`) are therefore structurally
  unavailable, which is why the unenforced `claude` slot was the only one that
  could run jcode there.

The current live state of `desk` is **unknown**. Nothing in this repository was
written against it after the install was recorded.

## The three long-term options

| Option | How | Security | Capability | Reversible |
|---|---|---|---|---|
| **A. Revert to KAS** | `uninstall-kirocrew-jcode-backend.ps1 -Confirm` | Back to the shipped engine. No third-party binary, no persisted override, no credential-exposed subprocess. | Loses: Kiro Crew talking to jcode at all on Windows. | Yes, re-run the installer |
| **B. Keep the unsandboxed `claude` slot** | leave as-is | **Weakest.** Unenforced read-only, no credential isolation, a persistent user-global variable. Every future launch inherits it. | Full jcode-backed chat on the desktop. | Yes, once, via this uninstaller |
| **C. Fail closed: refuse unsandboxed spawns** | set `agent.sandbox_allow_unsandboxed_exec = false` (and revert the slot, or leave it to be refused) | **Strongest available.** Kiro Crew refuses to spawn rather than running uncontained. The refusal is loud, so it cannot be mistaken for working. | No jcode on this host until a sandbox backend exists. | Yes: flip the flag back |

## Recommendation: option C, and keep jcode-on-KiroCrew on macOS

Set `sandbox_allow_unsandboxed_exec = false` on `desk`. Run jcode-on-KiroCrew
only where a real OS sandbox backend exists, which is macOS.

The reasoning is not caution for its own sake, it is the asymmetry:

- On macOS the `codex` slot is enforced and read-only routing genuinely arms, and
  there is now first-party evidence of a live adoption of a specific session
  (`acceptance/acp-live-adoption.md`).
- On Windows the only working slot gives up **both** properties at once, in
  exchange for a chat surface. The cost is an always-available path to `~/.ssh`
  and `~/.aws` from a model-driven subprocess, persisted for every future process.
- A refusal is a better failure than a silent downgrade. Option B fails silently:
  everything looks configured and works, with less protection than the operator
  believes they have. That is precisely the class of failure this repository's
  evidence rules exist to prevent.
- If Windows gains a sandbox backend, option A/B reopen with no code change.

## Commands

```powershell
# 1. See what a rollback would change. Writes nothing.
.\scripts\install\uninstall-kirocrew-jcode-backend.ps1 -DryRun

# 2. Option A: actually revert (config, shim, persisted variable).
.\scripts\install\uninstall-kirocrew-jcode-backend.ps1 -Confirm

# 3. Option C: after (or instead of) reverting, make refusal permanent.
#    Edit the Kiro Crew config and set, under "agent":
#        "sandbox_allow_unsandboxed_exec": false
#    then restart the gateway so it is read at boot.

# 4. Re-apply the install later, if a sandbox backend appears.
.\scripts\install\install-kirocrew-jcode-backend.ps1 -DryRun
.\scripts\install\install-kirocrew-jcode-backend.ps1
```

## What a reviewer should check first

These scripts are **reviewed, not executed**: there is no PowerShell on the macOS
authoring host, so nothing here was run. Check, in this order:

1. `[Environment]::SetEnvironmentVariable($name, $null, "User")` is what actually
   clears the variable. `setx NAME ""` does **not** delete it - it writes an empty
   string, which reads back as present. This is the single most likely place for a
   false "it worked".
2. The backup glob `config.json.bak-*-jcode` and the **oldest**-wins selection, so
   repeated cycles cannot ratchet the config forward.
3. That `acp_backend` is **removed** rather than set to null when the backup shows
   the key was absent originally.
4. That the config restore writes a `.bak-*-uninstall` backup *before* overwriting,
   so the uninstall is itself reversible.
5. That `-DryRun` performs no write at all, including no backup.

## Note on persistence

Reverting the config does not change the already-running gateway: Kiro Crew reads
its config at boot. Restart the gateway before expecting the old backend.
