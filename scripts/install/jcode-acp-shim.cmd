@echo off
REM jcode ACP shim for Kiro Crew on Windows.
REM
REM Kiro Crew does not name jcode as a backend: every ACP slot resolves an
REM adapter binary through a documented environment override, and this file is
REM what that override points at. Kiro Crew spawns it with NO arguments (measured),
REM so the argv below is the whole command.
REM
REM Why a .cmd and not an .exe: Kiro Crew's resolver treats a .cmd on Windows as
REM "directly runnable" (platform_compat.is_executable_file returns True) and
REM spawns it as-is. Verified against the installed build, not assumed.
REM
REM The provider/model pin is deliberate. The ACP adapter's unpinned default
REM resolved to a Bedrock model many hosts cannot serve, which surfaces as a
REM misleading "transient error (HTTP 5xx)" while the handshake is perfectly
REM healthy. Pinning a jcode route also means turns never touch Kiro's API.
setlocal
set "JCODE_BIN=@JCODE_BINARY@"
set "JCODE_PROVIDER=@PROVIDER@"
set "JCODE_MODEL=@MODEL@"
"%JCODE_BIN%" acp -p "%JCODE_PROVIDER%" -m "%JCODE_MODEL%" %*
