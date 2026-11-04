# Windows CIM probe assumptions, validated against `desk`

Live, read-only validation of the two Windows inventory probes
(`inventory/_windows_boot.py`, `inventory/_windows_table.py`) against the real
Windows host `desk` over `ssh -o BatchMode=yes -o ConnectTimeout=10`.

Date: 2026-11-04. Host: `Microsoft Windows [Version 10.0.28120.3393]`.

## Method and one transport caveat

`desk`'s SSH default shell is **cmd.exe**, not bash. A multi-statement string is
not split by `;`, so every script was staged locally (UTF-8), converted to
UTF-16LE, base64-encoded, and run as:

```
ssh -o BatchMode=yes -o ConnectTimeout=10 desk "powershell.exe -NoProfile -NonInteractive -EncodedCommand <B64>"
```

For the four probe claims this transport was replaced by the form the module
actually uses, feeding the identical script text on stdin:

```
ssh -o BatchMode=yes -o ConnectTimeout=10 desk 'powershell.exe -NoProfile -NonInteractive -Command -' < script.ps1
```

That distinction is not cosmetic. See the stderr finding at the end: the two
transport forms differ in whether PowerShell writes to stderr, and the module
fails closed on any non-empty stderr.

Nothing was installed, nothing on `desk` was written, no service was touched,
no process was signalled, and no command line, username, environment value or
message content was printed. Item 4 reports counts only. Item 6 reports byte
counts, not the path.

## 1. `Win32_OperatingSystem.LastBootUpTime` -> 7-digit UTC round-trip

Verdict: **CONFIRMED**

Script (byte-identical to `read_windows_boot_epoch`):

```powershell
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false } catch { }
try { $OutputEncoding = [Console]::OutputEncoding } catch { }
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
if ($null -eq $os -or $null -eq $os.LastBootUpTime) { exit 1 }
$os.LastBootUpTime.ToUniversalTime().ToString('o')
```

Observed stdout (30 bytes), exit 0, stderr 0 bytes:

```
2026-11-03T03:39:42.6681070Z
```

`LastBootUpTime` is non-null. The string has exactly seven fractional digits and
a literal `Z`, so `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$` matches.
`strptime(text[:19], "%Y-%m-%dT%H:%M:%S")` also parses. The value is stable
across separate invocations (observed identical on two runs ~3 minutes apart).

## 2. One matching pid -> a single JSON object with all three fields

Verdict: **CONFIRMED**

Filter was the PowerShell process's own pid (`ProcessId=$PID`); no user agent
session was targeted. Script identical to
`_read_windows_process_identity_chunk` with `$filter = "ProcessId=$PID"`.

Observed stdout (136 bytes), one line, first character `{`, exit 0, stderr 0:

```
{"pid":120044,"creation":"2026-11-04T08:33:20.9336850Z","executable":"c:\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe"}
```

A single match serialises as a JSON **object**, not a one-element array, on
Windows PowerShell 5.1 with `ConvertTo-Json -Compress`. All three keys are
present and non-null. `creation` has the same seven-digit `Z` shape; the
timestamp tracks the child's actual start. The module's `isinstance(data, dict)
-> [data]` normalisation is therefore exercised on real output, and the
`json.loads` path is reached with no BOM and no leading noise.

## 3. A filter matching nothing -> empty stdout, exit 0

Verdict: **CONFIRMED**

Same script with `$filter = "ProcessId=999999"`.

Observed: stdout **0 bytes**, stderr **0 bytes**, ssh exit **0**.

An empty match produces no output at all, not `null` and not an empty array, so
`text` is empty and the module returns `{}` (success, no bind) rather than
`None`. This is the distinction claim 3 asserts, and it holds.

## 4. `ExecutablePath` null for some processes

Verdict: **CONFIRMED**

```powershell
$all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
$nulls = @($all | Where-Object { $null -eq $_.ExecutablePath }).Count
"TOTAL=$($all.Count) NULL_EXEC=$nulls"
```

Observed:

```
TOTAL=1385 NULL_EXEC=28
```

`ExecutablePath` is non-null for the process inspected in item 2, and null for
28 of 1385 processes on this host (protected or access-denied images; CIM
returns null rather than raising). So the per-row
`if not isinstance(executable, str) or not executable.strip(): continue` guard
is necessary: without it those 28 rows would have to be either trusted with a
null path or raised. The module drops them, leaving the pid unbound, which is
the conservative outcome.

## 5. Which PowerShell binaries exist, and in what version

Verdict: **CONFIRMED**, with a caveat about where resolution happens.

| Path | Version | Edition |
| --- | --- | --- |
| `c:\Windows\System32\windowspowershell\v1.0\powershell.exe` | 5.1.28000.3393 | Desktop |
| `C:\Program Files\PowerShell\7\pwsh.exe` | 7.6.9 | Core |
| `C:\Program Files\PowerShell\7-preview\pwsh.exe` | 7.6.0-preview.4 | Core |
| `c:\Program Files (x86)\PowerShell\7\pwsh.exe` | present on PATH | Core |

`where.exe powershell.exe` resolves to exactly one path. `where.exe pwsh`
resolves to three. Since `_POWERSHELL_CANDIDATES = ("powershell.exe",
"powershell", "pwsh")` and `_powershell_path` uses `shutil.which`, the first
candidate wins and the probe runs under Windows PowerShell 5.1, never `pwsh`.
That ordering is correct for this host and the 5.1 script was the one exercised
in items 1-4.

Caveat: `shutil.which` must run **on the Windows host**. The check above proves
the binaries exist and resolve in that host's `PATH`; it does not exercise the
`shutil.which` call itself (that only runs inside the module on Windows).

## 6. Does the UTF-8 console preamble actually produce clean UTF-8?

Verdict: **CONFIRMED**, and the control shows the preamble is load-bearing.

A harmless, real non-ASCII path was located under `C:\Windows\System32`
(`SecureUxTheme.dll` plus eight non-ASCII characters; 46 chars, 54 UTF-8 bytes).
Its name is not reproduced here.

With the preamble (as in the module):

```
LEN=46
{"p":"C:\\Windows\\System32\\SecureUxTheme.dll.<16 high bytes>"}
END
```

- 72 bytes received, **16 bytes > 0x7F**, **zero** `?` characters.
- Strict UTF-8 decode succeeds; no U+FFFD.
- Python re-encode length equals the 54 bytes PowerShell reported.
- JSON parses and round-trips byte-identically.

Control, same path, identical script, preamble removed:

```
{"p":"C:\\Windows\\System32\\SecureUxTheme.dll.????????"}
END
```

- 64 bytes received, **0 bytes > 0x7F**, **eight** `?` characters.

So on this host the default output encoding mangles non-ASCII paths to `?`, and
the preamble's `[Console]::OutputEncoding = UTF8Encoding($false)` is what makes
the JSON decode cleanly. The BOM-less constructor matters too: no BOM was
observed. The module's `.lstrip("\ufeff")` is belt-and-braces, not load-bearing.

## Incidental finding: `-EncodedCommand` puts CLIXML on stderr

The module fails closed on any non-empty stderr (`if out.stderr.strip(): return
None`). Under the module's own `-Command` form, stderr was **0 bytes** on every
probe above, including the empty-result case.

Under `-EncodedCommand`, the same script produced **616 bytes on stderr**
starting `#< CLIXML`, containing a `"Preparing modules for first use."` progress
record, while stdout stayed clean. Nothing in the module uses
`-EncodedCommand`, so this is not a defect today. It is a trap: any future
switch to `-EncodedCommand` (or a host policy that serialises progress the same
way) would make every one of claims 1-4 return `None`, because the module treats
stderr as failure noise. A narrow fix, if that ever happens, is to append
`$ProgressPreference = 'SilentlyContinue'` to the preamble; it was verified to
produce 30 bytes on stdout and 0 on stderr.

## Summary

| # | Claim | Verdict |
| --- | --- | --- |
| 1 | `LastBootUpTime` non-null and 7-digit UTC round-trip | CONFIRMED |
| 2 | Single matching pid emits a single JSON object with 3 fields | CONFIRMED |
| 3 | No match emits empty stdout and exits 0 | CONFIRMED |
| 4 | `ExecutablePath` non-null for some, null for others | CONFIRMED (`28/1385` null) |
| 5 | `powershell.exe` then `powershell` then `pwsh` resolution | CONFIRMED (5.1 wins) |
| 6 | Preamble yields clean UTF-8 for non-ASCII path | CONFIRMED (control shows `????????` without it) |

No claim was refuted. The one thing worth carrying forward is the stderr
sensitivity: the probes are correct on this host only because the invocation
form keeps stderr empty.
