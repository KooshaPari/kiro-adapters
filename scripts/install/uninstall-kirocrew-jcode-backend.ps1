#Requires -Version 5.1
<#
.SYNOPSIS
    Reverse the Kiro Crew -> jcode backend install.

.DESCRIPTION
    The installer (install-kirocrew-jcode-backend.ps1) makes three persistent
    changes: it rewrites ~/.kiro/crew/config.json (agent.acp_backend plus an
    override entry in agent.env), writes %USERPROFILE%\bin\jcode-acp-shim.cmd,
    and persists a user environment variable with setx. There was no reverse
    path, which is the actual defect: an operator could not choose a safer
    posture later without hand-editing a config file and editing their own
    environment by hand.

    This script restores the config from the OLDEST .bak-*-jcode backup the
    installer left behind - that is the pre-project state - then removes the
    shim and clears the persisted variable.

    Why oldest, not newest: a repeated install/uninstall cycle would otherwise
    ratchet the config forward one backup at a time, because each run leaves a
    fresh backup of the already-modified file.

    It is deliberately conservative:
      * nothing is written without -Confirm, unless -DryRun is used
      * the config is backed up again before being restored, so the uninstall
        is itself reversible
      * it only touches the two override variables this project uses, and
        restores a pre-existing value from the backup rather than blanking it
      * a variable is cleared with .NET, not setx, because setx cannot delete:
        `setx NAME ""` leaves an empty-string value that still reads as set

.PARAMETER DryRun
    Report every change that would be made, change nothing.

.PARAMETER Confirm
    Actually perform the uninstall. Without it (and without -DryRun) the script
    explains what it would do and exits.

.EXAMPLE
    .\uninstall-kirocrew-jcode-backend.ps1 -DryRun

.EXAMPLE
    .\uninstall-kirocrew-jcode-backend.ps1 -Confirm
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"

$overrideEnvs = @("CODEX_ACP_BIN", "CLAUDE_AGENT_ACP_BIN")

function Write-Step([string]$Text) { Write-Host "  $Text" }
function Write-Warn([string]$Text) { Write-Host "  WARN $Text" -ForegroundColor Yellow }
function Write-Head([string]$Text) { Write-Host "`n$Text" -ForegroundColor Cyan }

if (-not $Confirm -and -not $DryRun) {
    Write-Host @"
uninstall-kirocrew-jcode-backend: nothing was changed.

This command reverses the Kiro Crew -> jcode backend install. It will:
  * restore agent.acp_backend and the override entry in agent.env from the
    newest config.json.bak-*-jcode backup
  * delete %USERPROFILE%\bin\jcode-acp-shim.cmd
  * clear the persisted user variable CODEX_ACP_BIN / CLAUDE_AGENT_ACP_BIN

Re-run with -DryRun to see exactly what would change, or -Confirm to do it.
"@
    exit 2
}

$crewHome = Join-Path $env:USERPROFILE ".kiro\crew"
$configPath = Join-Path $crewHome "config.json"
$shimPath = Join-Path (Join-Path $env:USERPROFILE "bin") "jcode-acp-shim.cmd"

Write-Head "uninstall target"
Write-Step "crew home   : $crewHome"
Write-Step "config      : $configPath"
Write-Step "shim        : $shimPath"
Write-Step "mode        : $(if ($DryRun) { 'DRY RUN - nothing will change' } else { 'APPLY' })"

if (-not (Test-Path $configPath)) {
    throw "Kiro Crew config not found at $configPath. Nothing to reverse."
}

# ------------------------------------------------------------------- the backup
# The installer names its backups "<config>.bak-<YYYYmmdd-HHMMSS>-jcode".
# Pick the newest one, because that is the state immediately before the most
# recent install. If several installs happened, the oldest is the pre-project
# state; prefer the OLDEST so a repeated install/uninstall cannot ratchet the
# config forward one backup at a time.
$backups = @(Get-ChildItem -Path $crewHome -Filter "config.json.bak-*-jcode" -File -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime)

if ($backups.Count -eq 0) {
    Write-Warn "no config.json.bak-*-jcode backup found."
    Write-Warn "Without one the original acp_backend value is unknown, so this"
    Write-Warn "script will NOT guess. Restore the value by hand, or re-run the"
    Write-Warn "installer to produce a backup first."
    Write-Step "shim and env var are still reversible; config is not."
    $restoreConfig = $false
} else {
    $restoreConfig = $true
    $backup = $backups[0]
    Write-Step "backup      : $($backup.Name) (oldest of $($backups.Count))"
}

# ------------------------------------------------------------------ the changes
$pyRestore = @'
import datetime, json, shutil, sys

cfg, backup, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
with open(backup, "r", encoding="utf-8") as fh:
    original = json.load(fh)
with open(cfg, "r", encoding="utf-8") as fh:
    current = json.load(fh)

orig_agent = original.get("agent") or {}
cur_agent = current.get("agent") or {}

print("restore acp_backend:", repr(cur_agent.get("acp_backend")), "->", repr(orig_agent.get("acp_backend")))

# The backup may predate the key entirely, which means "absent", not "null".
if "acp_backend" in orig_agent:
    cur_agent["acp_backend"] = orig_agent["acp_backend"]
else:
    cur_agent.pop("acp_backend", None)

orig_env = dict(orig_agent.get("env") or {})
cur_env = dict(cur_agent.get("env") or {})
for name in ("CODEX_ACP_BIN", "CLAUDE_AGENT_ACP_BIN"):
    if name in orig_env:
        cur_env[name] = orig_env[name]
        print("restore agent.env.%s" % name)
    elif name in cur_env:
        cur_env.pop(name, None)
        print("remove  agent.env.%s" % name)
cur_agent["env"] = cur_env
current["agent"] = cur_agent

if dry:
    raise SystemExit(0)

again = cfg + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-uninstall"
shutil.copy2(cfg, again)
with open(cfg, "w", encoding="utf-8") as fh:
    json.dump(current, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("pre-uninstall backup:", again)
'@

function Invoke-RestoreConfig {
    param([string]$Python)
    $tmp = Join-Path $env:TEMP ("jcode-uninstall-" + [guid]::NewGuid().ToString("N") + ".py")
    Set-Content -Path $tmp -Value $pyRestore -Encoding ASCII
    try {
        & $Python -s -P $tmp $configPath $backup.FullName $(if ($DryRun) { "1" } else { "0" })
        if ($LASTEXITCODE -ne 0) { throw "config restore failed with exit $LASTEXITCODE" }
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

Write-Head "changes"

if ($restoreConfig) {
    # Prefer the bundled interpreter, exactly as the installer does; fall back
    # to any Python on PATH so an uninstall still works if the app moved.
    $backendRoot = "C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend"
    $py = Join-Path $backendRoot "python.exe"
    if (-not (Test-Path $py)) {
        $candidate = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $candidate) {
            Write-Warn "no python found - config left untouched"
            $restoreConfig = $false
        } else {
            $py = $candidate.Source
        }
    }
    if ($restoreConfig) { Invoke-RestoreConfig -Python $py }
}

if (Test-Path $shimPath) {
    if ($DryRun) {
        Write-Step "would delete $shimPath"
    } else {
        Remove-Item -LiteralPath $shimPath -Force
        Write-Step "deleted  $shimPath"
    }
} else {
    Write-Step "shim already absent"
}

foreach ($name in $overrideEnvs) {
    $userValue = [Environment]::GetEnvironmentVariable($name, "User")
    $procValue = [Environment]::GetEnvironmentVariable($name, "Process")
    if ($null -ne $userValue -or $null -ne $procValue) {
        if ($DryRun) {
            Write-Step "would clear persisted $name (user=$([bool]$userValue) process=$([bool]$procValue))"
        } else {
            # null removes it. setx cannot: it writes an empty string, which
            # GetEnvironmentVariable still returns as "" - indistinguishable
            # from "set" to anything testing for presence rather than content.
            [Environment]::SetEnvironmentVariable($name, $null, "User")
            Write-Step "cleared  persisted $name"
        }
    } else {
        Write-Step "$name already absent"
    }
}

Write-Head "verification"
$restored = $true
if ($restoreConfig -and -not $DryRun) {
    $checkPy = @'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
agent = data.get("agent") or {}
print("acp_backend =", repr(agent.get("acp_backend")))
print("agent.env   =", json.dumps(sorted((agent.get("env") or {}).keys())))
'@
    $tmp = Join-Path $env:TEMP ("jcode-uninstall-check-" + [guid]::NewGuid().ToString("N") + ".py")
    Set-Content -Path $tmp -Value $checkPy -Encoding ASCII
    try { & $py -s -P $tmp $configPath } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

foreach ($name in $overrideEnvs) {
    $userValue = [Environment]::GetEnvironmentVariable($name, "User")
    if ($null -ne $userValue -and -not $DryRun) {
        Write-Warn "$name is still set for the user scope"
        $restored = $false
    }
}
if ((Test-Path $shimPath) -and -not $DryRun) {
    Write-Warn "shim still present at $shimPath"
    $restored = $false
}

if ($DryRun) {
    Write-Host "`nDRY RUN: nothing was changed." -ForegroundColor Yellow
    exit 0
}
if ($restored) {
    Write-Host "`nuninstall complete." -ForegroundColor Green
    Write-Host "Kiro Crew reads its config at boot: restart the gateway before"
    Write-Host "expecting the old backend to be in effect."
    exit 0
}
Write-Host "`nuninstall finished with warnings above." -ForegroundColor Yellow
exit 1
