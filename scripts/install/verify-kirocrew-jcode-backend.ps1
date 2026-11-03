<#
.SYNOPSIS
    Verify, link by link, that Kiro Crew (Windows) will actually reach jcode.

.DESCRIPTION
    One green light is not enough. This install has already failed twice in ways
    that looked fine: a config value that named a slot whose binary was never
    spawned, and an override that existed in config but not in the process
    environment the resolver actually reads. So every link is reported
    separately and the script exits non-zero on any failure.

    Links checked:
      1. crew home and config exist
      2. agent.acp_backend names a slot
      3. that slot is not the enforced kind on a host with no sandbox backend
         (Kiro Crew refuses those, which is a hard failure, not a warning)
      4. the slot's override variable exists in THIS process environment
         (the resolver reads os.environ; agent.env alone does not reach it)
      5. the override points at a file, and it is runnable
      6. the jcode binary the shim references exists
      7. the shim completes a real ACP `initialize` handshake and reports jcode
      8. -Live: a real Kiro Crew turn answers a token

.PARAMETER Live
    Also run one real `kirocrew chat -m` turn. Slower, and it is the only check
    that proves Kiro Crew itself spawns the shim rather than something else.
#>
[CmdletBinding()]
param([switch] $Live)

$ErrorActionPreference = "Continue"

$script:failures = 0
$script:warnings = 0

function Pass($name, $detail) { Write-Host ("PASS  {0,-20} {1}" -f $name, $detail) -ForegroundColor Green }
function Fail($name, $detail) { Write-Host ("FAIL  {0,-20} {1}" -f $name, $detail) -ForegroundColor Red; $script:failures++ }
function Warn($name, $detail) { Write-Host ("WARN  {0,-20} {1}" -f $name, $detail) -ForegroundColor Yellow; $script:warnings++ }

Write-Host "Kiro Crew -> jcode backend (Windows)"
Write-Host "------------------------------------"

$crewHome  = Join-Path $env:USERPROFILE ".kiro\crew"
$configPath = Join-Path $crewHome "config.json"
$backendRoot = "C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend"
$py = Join-Path $backendRoot "python.exe"

# 1. crew home
if ((Test-Path $crewHome) -and (Test-Path $configPath)) {
    Pass "kiro-crew-home" $crewHome
} else {
    Fail "kiro-crew-home" "missing $configPath"
}

if (-not (Test-Path $py)) {
    Fail "bundled-python" "missing $py"
    Write-Host ""
    Write-Host "RESULT: FAIL ($($script:failures) failure(s))"
    exit 1
}

# One python call returns everything about config + sandbox, so the checks below
# do not each pay a cold import.
$statePy = @'
import json
out = {}
try:
    from kiro_crew.config import KiroCrewConfig
    c = KiroCrewConfig.load()
    out["acp_backend"] = getattr(c.agent, "acp_backend", None)
    out["agent_env"] = dict(getattr(c.agent, "env", {}) or {})
except Exception as exc:
    out["config_error"] = type(exc).__name__ + ": " + str(exc)
try:
    from kiro_crew.sandbox import detect_backend
    out["sandbox_backend"] = detect_backend("auto")
except Exception as exc:
    out["sandbox_error"] = type(exc).__name__ + ": " + str(exc)
try:
    from kiro_crew.acp_tool_gate import is_enforced
    out["enforced"] = {b: bool(is_enforced(b)) for b in
                       ("codex", "claude", "kas", "kiro", "opencode", "deepseek", "goose", "pi")}
except Exception as exc:
    out["enforced_error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(out))
'@
$stateFile = Join-Path $env:TEMP ("jcode-state-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $stateFile -Value $statePy -Encoding ASCII
$raw = (& $py -s -P $stateFile 2>&1 | Select-Object -Last 1)
Remove-Item $stateFile -ErrorAction SilentlyContinue
$state = $null
try { $state = $raw | ConvertFrom-Json } catch { Fail "config-load" "could not read state: $raw" }

if ($state) {
    if ($state.config_error) { Fail "config-load" $state.config_error }

    # 2. slot
    $slot = [string]$state.acp_backend
    if ($slot) { Pass "acp_backend" "agent.acp_backend = `"$slot`"" }
    else { Fail "acp_backend" "unset" }

    # 3. slot vs sandbox reality
    $sandbox = [string]$state.sandbox_backend
    $isEnforced = $false
    if ($state.enforced -and $slot) {
        $isEnforced = [bool]$state.enforced.$slot
    }
    if ($isEnforced -and $sandbox -eq "none") {
        Fail "slot-usable" ("slot `"$slot`" is enforced but this host has no sandbox backend " +
                            "(detect_backend=none); Kiro Crew REFUSES to spawn it")
    } elseif ($isEnforced) {
        Pass "slot-usable" "enforced slot `"$slot`" with sandbox backend `"$sandbox`""
    } else {
        Pass "slot-usable" "slot `"$slot`" is not enforced (spawns without a sandbox `"$sandbox`")"
    }

    # 4/5. the override the resolver reads
    $envName = if ($slot -eq "codex") { "CODEX_ACP_BIN" }
               elseif ($slot -eq "claude") { "CLAUDE_AGENT_ACP_BIN" }
               else { $null }

    if (-not $envName) {
        Warn "override-var" "slot `"$slot`" has no known ACP override; nothing to check"
    } else {
        $configured = $null
        if ($state.agent_env) { $configured = $state.agent_env.$envName }

        $inProcess = [Environment]::GetEnvironmentVariable($envName, "Process")
        $persisted = [Environment]::GetEnvironmentVariable($envName, "User")

        if ($inProcess) {
            Pass "override-var" "$envName=$inProcess (process)"
        } elseif ($persisted) {
            Warn "override-var" ("$envName is set for the user but NOT in this process; " +
                                 "the resolver reads os.environ, so this shell would fail " +
                                 "while the relaunched GUI works")
        } else {
            Fail "override-var" ("$envName is absent from the process environment. agent.env " +
                                 "does not reach the resolver, so the spawn fails with " +
                                 "'codex-acp not found' while config looks correct")
        }

        if ($configured) {
            if ($configured -eq $inProcess -or $configured -eq $persisted) {
                Pass "agent-env" "agent.env.$envName agrees"
            } else {
                Warn "agent-env" "agent.env.$envName = $configured (child env only; not what the resolver reads)"
            }
        }

        $target = if ($inProcess) { $inProcess } else { $persisted }
        if ($target) {
            if (Test-Path $target) { Pass "override-file" $target }
            else { Fail "override-file" "$target does not exist" }

            if ($target -match "\.cmd$|\.exe$") { Pass "override-runnable" "directly runnable on Windows" }
            else { Warn "override-runnable" "$target is not .cmd/.exe; may be wrapped with node" }

            # 6. the jcode binary the shim references
            if (Test-Path $target) {
                $shimText = Get-Content $target -Raw
                if ($shimText -match '"([^"]*jcode[^"]*\.exe)"') {
                    $jcodeBin = $matches[1]
                    if (Test-Path $jcodeBin) { Pass "jcode-binary" $jcodeBin }
                    else { Fail "jcode-binary" "$jcodeBin does not exist" }
                } else {
                    Warn "jcode-binary" "could not parse a jcode path out of the shim"
                }
                if ($shimText -match "-p\s+\S+\s+-m\s+\S+") {
                    Pass "provider-model" ($matches[0].Trim())
                } else {
                    Warn "provider-model" "no pinned -p/-m; the ACP default may be unservable"
                }
            }
        }
    }
}

# 7. real ACP handshake through the shim, independent of Kiro Crew
$handshakePy = @'
import asyncio, json, os, sys

for name in ("CODEX_ACP_BIN", "CLAUDE_AGENT_ACP_BIN"):
    v = os.environ.get(name)
    if v and os.path.isfile(v):
        target = v
        break
else:
    print("RESULT: NO_SHIM")
    raise SystemExit(3)

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": 1,
                   "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                          "terminal": False},
                   "clientInfo": {"name": "jcode-verify", "version": "0.0.1"}}}

async def main():
    proc = await asyncio.create_subprocess_exec(
        target, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        proc.stdin.write((json.dumps(INIT) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=90)
    except asyncio.TimeoutError:
        print("RESULT: TIMEOUT")
        return
    except Exception as exc:
        # On Windows a .cmd shim can exit while the pipe is still settling, and
        # a closed-pipe error here is a transport detail, not a verdict. Report
        # it rather than letting the traceback become the "result".
        print("RESULT: ERROR %s: %s" % (type(exc).__name__, exc))
        return
    finally:
        try:
            if proc.returncode is None:
                proc.kill()
            # Reap the child so its pipe transports close while the loop is still
            # alive. Without this, Windows asyncio prints "unclosed transport" /
            # "I/O operation on closed pipe" from __del__ AFTER the verdict, and a
            # caller reading the last line sees the noise instead of the result.
            # Bounded: an unbounded wait() here is itself a hang, which is the
            # failure this verifier exists to avoid.
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass
    text = line.decode(errors="replace").strip()
    if not text:
        print("RESULT: EMPTY")
        return
    try:
        msg = json.loads(text)
    except Exception:
        print("RESULT: NON_JSON " + text[:200])
        return
    info = (msg.get("result") or {}).get("agentInfo") or {}
    if info.get("name"):
        print("RESULT: ACP_OK name=%s version=%s" % (info.get("name"), info.get("version")))
    else:
        print("RESULT: ACP_ERROR " + json.dumps(msg)[:300])

asyncio.run(main())
'@
$hsFile = Join-Path $env:TEMP ("jcode-hs-" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $hsFile -Value $handshakePy -Encoding ASCII
$overrideForHs = [Environment]::GetEnvironmentVariable("CODEX_ACP_BIN", "Process")
if (-not $overrideForHs) { $overrideForHs = [Environment]::GetEnvironmentVariable("CLAUDE_AGENT_ACP_BIN", "Process") }
if (-not $overrideForHs) { $overrideForHs = [Environment]::GetEnvironmentVariable("CLAUDE_AGENT_ACP_BIN", "User") }
$env:CLAUDE_AGENT_ACP_BIN = if ($overrideForHs) { $overrideForHs } else { $env:CLAUDE_AGENT_ACP_BIN }
$hsRaw = (& $py -s -P $hsFile 2>&1 | Out-String)
Remove-Item $hsFile -ErrorAction SilentlyContinue
# Match the verdict across ALL output, never the last line: asyncio on Windows can
# emit shutdown noise after it, and taking the tail once turned a passing
# handshake into a bogus ValueError failure.
if ($hsRaw -match "RESULT: ACP_OK name=(\S+) version=(\S+)") {
    Pass "acp-handshake" "$($matches[1]) $($matches[2]) speaks ACP through the shim"
} elseif ($hsRaw -match "RESULT: ([A-Z_]+[^\r\n]*)") {
    Fail "acp-handshake" "$($matches[1])"
} else {
    Fail "acp-handshake" "no verdict in probe output"
}

# 8. a real Kiro Crew turn
#
# Bounded on purpose. `chat -m` normally answers in ~2 minutes here, but piping it
# through `Out-String` waits for exit with NO timeout, and when the turn stalls the
# verifier hangs forever instead of failing. A watchdog turns a stall into a FAIL.
if ($Live) {
    $launcher = Join-Path $backendRoot "bin\kirocrew.cmd"
    if (-not (Test-Path $launcher)) {
        Fail "live-turn" "launcher not found at $launcher"
    } else {
        $token = "VERIFY_" + ([guid]::NewGuid().ToString("N").Substring(0, 8).ToUpper())
        $outFile = Join-Path $env:TEMP ("jcode-live-" + [guid]::NewGuid().ToString("N") + ".log")
        $errFile = $outFile + ".err"
        # Generate a .bat instead of passing a quoted command through cmd /c.
        # Kiro Crew's launcher lives under "C:\Program Files\...", and nesting
        # quotes inside Start-Process -ArgumentList split it into "C:\Program",
        # which is not a command. A file has no quoting to get wrong.
        $batFile = Join-Path $env:TEMP ("jcode-live-" + [guid]::NewGuid().ToString("N") + ".bat")
        $batBody = @"
@echo off
"$launcher" chat -m "Reply with exactly this token and nothing else: $token"
"@
        Set-Content -Path $batFile -Value $batBody -Encoding ASCII
        $proc = Start-Process -FilePath "cmd.exe" `
            -ArgumentList "/c", "`"$batFile`"" `
            -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $timeoutSec = 240
        if (-not $proc.WaitForExit($timeoutSec * 1000)) {
            try { $proc.Kill() } catch { }
            Fail "live-turn" "no answer within ${timeoutSec}s (turn stalled; killed)"
        } else {
            $text = ""
            if (Test-Path $outFile) { $text += (Get-Content $outFile -Raw) }
            if (Test-Path $errFile) { $text += (Get-Content $errFile -Raw) }
            if ($text -match [regex]::Escape($token)) {
                Pass "live-turn" "Kiro Crew answered $token (exit $($proc.ExitCode))"
            } else {
                $tail = if ($text.Length -gt 400) { $text.Substring($text.Length - 400) } else { $text }
                Fail "live-turn" ("no token in output (exit $($proc.ExitCode)); tail: " + $tail)
            }
        }
        Remove-Item $outFile, $errFile, $batFile -ErrorAction SilentlyContinue
    }
}

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host ("RESULT: FAIL ({0} failure(s), {1} warning(s))" -f $script:failures, $script:warnings) -ForegroundColor Red
    exit 1
}
Write-Host ("RESULT: PASS ({0} warning(s))" -f $script:warnings) -ForegroundColor Green
exit 0
