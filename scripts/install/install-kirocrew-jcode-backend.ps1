<#
.SYNOPSIS
    Point Kiro Crew (Windows) at jcode as its agent backend, with no Kiro API.

.DESCRIPTION
    Kiro Crew's agent.acp_backend is a closed set of SLOTS, and none of them is
    named "jcode". Each slot resolves the ACP binary it launches through a
    documented environment override, so the install is:

        1. a shim that execs `jcode acp -p <provider> -m <model>`
        2. agent.acp_backend naming the slot whose override we control
        3. the override variable present in the PROCESS environment

    Step 3 is not optional and is the trap. Kiro Crew reads the override with
    os.environ in the parent process; the config's agent.env overlay is applied
    to the CHILD afterwards, so agent.env alone leaves the resolver unable to
    find the binary and the turn dies with "codex-acp not found" while the
    config looks perfect. Measured, not assumed.

    SLOT SELECTION is forced by the host, and this script decides it by asking
    the installed build rather than guessing:

      - The codex/opencode/deepseek/goose/pi slots are ENFORCED: Kiro Crew
        demands an OS sandbox to mask credential files before it will spawn a
        third-party binary, and REFUSES otherwise.
      - Native Windows has no sandbox backend at all (detect_backend returns
        "none" for every mode), so every enforced slot is unavailable there.
      - The claude slot is the one slot that is NOT enforced, so it is the only
        slot that can host jcode on native Windows.

    The cost of that fallback is real and is printed: the claude slot does not
    force mode=read-only, and on a sandboxless host the agent subprocess runs
    without credential isolation.

.PARAMETER JcodePath
    Path to jcode.exe. Defaults to whatever `jcode` resolves to on PATH.

.PARAMETER Provider
    jcode provider for the pinned route. Default opencode-go (no Kiro API).

.PARAMETER Model
    jcode model for the pinned route. Default deepseek-v4.1-flash.

.PARAMETER DryRun
    Report every change without writing any.
#>
[CmdletBinding()]
param(
    [string] $JcodePath,
    [string] $Provider = "opencode-go",
    [string] $Model    = "deepseek-v4.1-flash",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" }
function Write-Warn($msg) { Write-Host "WARN  $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- locate the app
$crewHome = Join-Path $env:USERPROFILE ".kiro\crew"
$configPath = Join-Path $crewHome "config.json"
$backendRoot = "C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend"

if (-not (Test-Path $configPath)) {
    throw "Kiro Crew config not found at $configPath. Install and run Kiro Crew once first."
}
$py = Join-Path $backendRoot "python.exe"
if (-not (Test-Path $py)) {
    throw "Kiro Crew's bundled python not found at $py; is this a Kiro Crew install?"
}

if (-not $JcodePath) {
    $cmd = Get-Command jcode -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "jcode not found on PATH. Pass -JcodePath." }
    $JcodePath = $cmd.Source
}
if (-not (Test-Path $JcodePath)) { throw "jcode binary not found at $JcodePath" }

Write-Step "jcode       : $JcodePath"
Write-Step "provider    : $Provider"
Write-Step "model       : $Model"
Write-Step "crew home   : $crewHome"

# ------------------------------------------------------- decide the slot honestly
# Ask the installed build which sandbox backend this host has. An enforced slot
# is only usable when one exists.
$probe = @'
import sys
try:
    from kiro_crew.sandbox import detect_backend
    print(detect_backend("auto"))
except Exception as exc:
    print("ERROR:" + type(exc).__name__ + ":" + str(exc))
'@
$probeFile = Join-Path $env:TEMP ("jcode-slot-probe-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $probeFile -Value $probe -Encoding ASCII
$sandboxBackend = (& $py -s -P $probeFile 2>&1 | Select-Object -Last 1).ToString().Trim()
Remove-Item $probeFile -ErrorAction SilentlyContinue

Write-Step "sandbox     : $sandboxBackend"

$enforcedUsable = ($sandboxBackend -ne "none") -and (-not $sandboxBackend.StartsWith("ERROR:"))

if ($enforcedUsable) {
    $slot = "codex"
    $overrideEnv = "CODEX_ACP_BIN"
    Write-Step "slot        : codex (enforced; sandbox backend present)"
} else {
    $slot = "claude"
    $overrideEnv = "CLAUDE_AGENT_ACP_BIN"
    Write-Step "slot        : claude (not enforced)"
    Write-Warn "This host has no OS sandbox backend, so Kiro Crew REFUSES every"
    Write-Warn "enforced slot (codex/opencode/deepseek/goose/pi) here. The claude"
    Write-Warn "slot is the only one that can spawn jcode on native Windows, and"
    Write-Warn "it gives up two things the codex slot would have provided:"
    Write-Warn "  - Kiro Crew does NOT force mode=read-only on this slot."
    Write-Warn "  - The agent subprocess runs WITHOUT credential isolation, so"
    Write-Warn "    ~/.ssh, ~/.aws and similar are readable by it."
    Write-Warn "Treat this install as convenient, not contained."
}

# --------------------------------------------------------------------- the shim
$shimDir = Join-Path $env:USERPROFILE "bin"
$shimPath = Join-Path $shimDir "jcode-acp-shim.cmd"
$template = Join-Path $PSScriptRoot "jcode-acp-shim.cmd"
if (-not (Test-Path $template)) { throw "Shim template not found at $template" }

$shimText = Get-Content $template -Raw
$shimText = $shimText.Replace("@JCODE_BINARY@",  $JcodePath).
                      Replace("@PROVIDER@",       $Provider).
                      Replace("@MODEL@",          $Model)

if ($DryRun) {
    Write-Step "dry run: shim would be written to $shimPath"
} else {
    New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
    # ASCII, not UTF-8: a BOM on line 1 makes cmd.exe choke on @echo off.
    Set-Content -Path $shimPath -Value $shimText -Encoding ASCII
    Write-Step "shim        : $shimPath"
}

# ------------------------------------------------------------------- the config
$cfgPy = @'
import datetime, json, shutil, sys

cfg, slot, env_name, env_value, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1"
with open(cfg, "r", encoding="utf-8") as fh:
    data = json.load(fh)

agent = data.setdefault("agent", {})
before = dict(agent)
agent["acp_backend"] = slot

env = dict(agent.get("env") or {})
# Remove the other slot's override so nothing looks configured that cannot run.
for stale in ("CODEX_ACP_BIN", "CLAUDE_AGENT_ACP_BIN"):
    if stale != env_name:
        env.pop(stale, None)
env[env_name] = env_value
agent["env"] = env

if dry:
    print("dry-run agent:", json.dumps(agent, ensure_ascii=False))
    raise SystemExit(0)

backup = cfg + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-jcode"
shutil.copy2(cfg, backup)
with open(cfg, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("backup:", backup)
print("agent before:", json.dumps(before, ensure_ascii=False))
print("agent after :", json.dumps(agent, ensure_ascii=False))
'@
$cfgFile = Join-Path $env:TEMP ("jcode-cfg-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $cfgFile -Value $cfgPy -Encoding ASCII
$dryArg = if ($DryRun) { "1" } else { "0" }
& $py -s -P $cfgFile $configPath $slot $overrideEnv $shimPath $dryArg
Remove-Item $cfgFile -ErrorAction SilentlyContinue

# ------------------------------------------------- the variable the resolver reads
# Persist it for the GUI app: a setx value is inherited by processes started
# later, which is how Kiro Crew sees it after a restart.
if ($DryRun) {
    Write-Step "dry run: would setx $overrideEnv=$shimPath"
} else {
    & setx $overrideEnv $shimPath | Out-Null
    Write-Step "persisted   : $overrideEnv"
}

# -------------------------------------------------------------- verify it loads
$verifyPy = @'
import json, sys
from kiro_crew.config import KiroCrewConfig
c = KiroCrewConfig.load()
print("acp_backend =", c.agent.acp_backend)
print("agent.env   =", json.dumps(dict(c.agent.env), ensure_ascii=False))
'@
$verifyFile = Join-Path $env:TEMP ("jcode-verify-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $verifyFile -Value $verifyPy -Encoding ASCII
& $py -s -P $verifyFile
Remove-Item $verifyFile -ErrorAction SilentlyContinue

Write-Host ""
if ($DryRun) {
    Write-Step "dry run complete; nothing was changed."
} else {
    Write-Step "install complete. Run the verifier, then a live turn:"
    Write-Host "    .\verify-kirocrew-jcode-backend.ps1"
    Write-Host "    .\verify-kirocrew-jcode-backend.ps1 -Live"
    Write-Host ""
    Write-Step "A NEW process is required before the override is visible:"
    Write-Host "    start a fresh shell / relaunch Kiro Crew (the GUI inherits the setx value)."
}
