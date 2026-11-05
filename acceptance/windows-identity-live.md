# Windows composed process identity, proved on the real host

Date: 2026-11-05
Host: `desk` (`kooshapari-desk`), Windows 11 + WSL2. Module run under
**native Windows CPython 3.14.5** (`C:\Python314\python.exe`). No stubs were
introduced in the module; no file outside the Windows `%TEMP%` scratch dir was
written, and that dir was removed afterwards (`ALL_SCRATCH_ABSENT`).

Method: `src/kiro_adapters` plus `tests/test_windows_boot_identity.py`,
`tests/test_process_identity_failclosed.py`, `tests/test_inventory_processes.py`
were tar-piped to `%TEMP%\kiro-idcheck-live`, exercised by launcher scripts that
only insert `src`/`tests` on `sys.path`, then the scratch dir was deleted.

## Verdict summary

| # | Claim | Verdict |
|---|---|---|
| 1 | Inventory module imports; Windows identity unit tests run there | **CONFIRMED** (56 tests ran; 3 failures, all test-portability) |
| 2 | `process_table.boot_id()` returns a real `cim-boot:` value, stable in one boot | **REFUTED** (returns `unknown`) |
| 3 | `describe_many` / `process_start` give stable subsecond `cim-creation:` identities | **MIXED**: `process_start` CONFIRMED; `describe_many` REFUTED (binds 0 of 2) |
| 4 | `same_process` composes True for a live handle, False when one component is perturbed | **REFUTED / NOT ESTABLISHED** (no handle binds on Windows, so no positive exists) |
| 5 | A pid with null `ExecutablePath` is refused, not bound | **NOT REACHED live** (probe died before this step); code path refuses, proof absent |

The composition the mocks asserted does **not** hold on the real host. That is
the point of running it here: two independent defects make it unreachable.

## 1. Import and unit tests on Windows: CONFIRMED

```
== python
3.14.5 Windows
== unit tests
LOADED test_windows_boot_identity 26
LOADED test_process_identity_failclosed 29
LOADED test_inventory_processes 1
Ran 56 tests in 2.766s
TOTALS run=56 fail=3 err=1 skip=0
```

* `test_windows_boot_identity` — **26/26 pass** on Windows.
* `test_process_identity_failclosed` — 26 pass, **3 fail**:
  `ExecutableWitnessTests.test_matching_handle_is_accepted`,
  `PidMatchTests.test_a_handle_for_the_same_pid_is_accepted`,
  `PidMatchTests.test_a_digit_string_pid_is_accepted`.
  Cause is a **test-portability defect, not a product defect**: `IdentityProbe`
  injects a POSIX-shaped start identity, and the Windows-only gate in
  `same_process` (`processes.py`, the `is_windows() and not
  start_identity.startswith(WINDOWS_START_PREFIX)` branch) correctly refuses a
  non-`cim-creation:` value. These three positives are unfalsifiable on Windows
  as written; they need a CIM-shaped fixture.
* `test_inventory_processes` did **not** run: `ModuleNotFoundError: No module
  named '_support'`. That is a **harness artifact** — my copy took only the
  three named test files and omitted the shared `tests/_support.py` helper. Not
  a repo defect, and not evidence about the module.

No stubbing was required to import the module on Windows. One harness
concession, stated plainly: the module was launched through
`run_tests.py`/`run_probe.py`, which only prepend `src`/`tests` to `sys.path`
(passing `PYTHONPATH` across the WSL interop boundary did not resolve for the
Windows interpreter). That affects discovery, not behaviour.

## 2. `boot_id()`: REFUTED — and the cause is a real defect

Two invocations in one process run, from the live probe:

```
{"step": "boot_id", "first": "unknown", "second": "unknown",
 "same": true, "is_cim": false, "is_unknown": true}
```

"Stable within a boot" is trivially true only because both reads are the
`unknown` sentinel. Direct capture of the same script through the same bounded
reader:

```
{"step": "boot_epoch_module", "value": null}
{"step": "boot_capture", "rc": 0, "ok": true, "trunc": false, "timeout": false,
 "stalled": false,
 "stdout": "[Console]::OutputEncoding = New-Object -TypeName S",
 "stderr_len": 0}
```

Root cause: `_POWERSHELL_PREAMBLE` in `_windows_table.py` is brace-doubled
(`try {{ ... }} catch {{ }}`) because it is written to be consumed by
`.format()`. `_read_windows_process_identity_chunk` does call `.format(...)`,
which collapses the braces — that path works. `_windows_boot.read_windows_boot_epoch`
**concatenates the preamble without `.format()`**, so PowerShell receives the
literal text `[Console]::OutputEncoding = New-Object ...` as its first
statement, the timestamp never reaches stdout, and the `^\d{4}-...\.\d{7}Z$`
match fails. `read_windows_boot_epoch()` therefore returns `None` on every
real Windows host.

Consequence: `boot_id()` is `unknown`, so `same_process` refuses everything on
Windows (`current_boot == UNKNOWN_START` → `False`), by design.

Sanity check that the host and shell are fine: the same expression run
directly through `powershell.exe` from the remote shell returned
`2026-11-03T03:39:42.6681070Z`, and `process_start` (the `.format()`ed sibling
script) returned real CIM values. The failure is in this one call site.

## 3. `process_start` and `describe_many`: MIXED

`process_start` — **CONFIRMED**, real subsecond CIM creation times for two live
pids, both `cim-creation:`-prefixed:

```
{"step": "process_start",
 "values": {"<pid A>": "cim-creation:2026-11-05T01:59:33.0498220Z",
            "<pid B>": "cim-creation:2026-11-05T01:59:39.3207370Z"},
 "all_cim_creation": true}
```

Stability of `process_start` across two invocations was **not** measured
separately (the probe called it once per pid), so that sub-claim is UNKNOWN.

`describe_many` — **REFUTED**. Two invocations, two live same-user pids,
including the interpreter's own pid:

```
{"step": "describe_many_run1", "n": 0, "detail": {}}
{"step": "describe_many_run2", "n": 0, "detail": {}}
```

Root cause, independent of (2): `windows_liveness` parses
`tasklist /FI "PID eq N" /NH` output with the **CSV** splitter
(`line.split('","')`). The filtered form in the default table format has no
`","` separators, so no line ever yields two fields and the function falls
through to `UNKNOWN`. Observed directly:

```
{"step": "tasklist_filtered", "rc": 0, "ok": true, ...,
 "stdout_head": "\r\npython.exe    <pid> Services   0   23,188 K\r\n"}
{"step": "liveness_self", "verdict": "unknown"}
{"step": "describe_many_self", "bound": false}
```

`describe_many` drops any pid whose liveness is not `ALIVE`, so on Windows it
binds nothing at all — the same refusal shows up in `describe()`.

A third, lesser issue: `read_windows_table()` (`tasklist /FO CSV /NH`,
20 s budget) **timed out** on this host (`rc: 1, timed_out: true` with 3036
bytes captured), so the bulk pid→image table is unavailable too.

## 4. Composed `same_process` match: REFUTED / NOT ESTABLISHED

```
{"step": "same_process", "error": "no handle bound, cannot compose a match"}
```

Because `describe_many` bound neither pid, there was no `ProcessInstance` to
compose against. The perturbations (wrong pid, wrong executable path, altered
creation time, altered boot) could not be exercised as a True/False contrast;
only refusals occur on this host, and they occur for the reasons in (2) and
(3) rather than because the composition logic is wrong. The composed-match
claim therefore remains **unproven on real hardware** — which is exactly what
the mocks could not establish, and what this run was meant to settle.

This is the substantive result: on the real host, Windows identity is inert.
Fix (3) and (2) and this item must be re-run; it cannot be closed as-is.

## 5. Null `ExecutablePath` refusal: NOT REACHED

The probe terminated before the null-`ExecutablePath` scan with
`AttributeError: module 'kiro_adapters.inventory.process_table' has no
attribute '_powershell_path'` — my probe imported the private helper from
`process_table`, where it is actually defined in `_windows_table`. That is a
defect in my probe, not in the module.

Status is therefore **UNKNOWN by live observation**. Code-level evidence only:
`_read_windows_process_identity_chunk` skips a row whose `executable` is not a
non-empty string, and `describe_many` independently requires a non-empty
executable before binding. Both refuse. Live confirmation is still owed, and
it is currently unobservable anyway because nothing binds on Windows at all
(item 3).

## What this changes

Live evidence contradicts the mock-only picture in three places:

1. `read_windows_boot_epoch` cannot work on Windows: brace-doubled preamble
   passed without `.format()`. `boot_id()` is permanently `unknown` there.
2. `windows_liveness` parses table-format `tasklist` output with a CSV
   splitter, so liveness is permanently `unknown` and nothing ever binds.
3. `read_windows_table` exceeds its 20 s budget on a real Windows 11 host.

Plus one test-suite defect: three `same_process` positives in
`test_process_identity_failclosed.py` use a POSIX-shaped start identity and
cannot pass on Windows.

None of these is fixed in this commit: the task was to prove the behaviour on
the real host, and the honest report is that it does not compose. The
follow-up is to fix (1) and (2), re-run this same probe, and only then accept
items 3, 4 and 5.

---

## Follow-up 2026-11-05: the refutation above was a real bug, now fixed and re-verified

The three REFUTED items shared one root cause, and it was a defect, not a gap.

`_POWERSHELL_PREAMBLE` is written with **doubled braces** (`try {{ ... }} catch {{ }}`)
because callers pipe it through `str.format()`. `_windows_boot.py` concatenated it
**without** calling `.format()`.

PowerShell does not error on that. It treats `{{ ... }}` as a nested script **block**
and emits the block's text to stdout, so the output becomes two lines:

```
 [Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
2026-11-03T03:39:42.6681070Z
```

`_windows_boot.read_windows_boot_epoch` has its own single-line guard (`"\n" in text`),
so it rejected the value and returned `None`. Exit code 0, empty stderr, nothing in any
log. `boot_id()` therefore returned `unknown`, which cascaded: `describe_many` binds no
pids without a shared boot, and `same_process` can never be True on Windows. One silent
two-line stdout disabled the entire Windows identity path.

Measured on the host, both forms over ssh, same query:

| Script form | stdout | Accepted? |
|---|---|---|
| unrendered (old) | block text + epoch = 2 lines | no -> `unknown` |
| rendered (new) | `2026-11-03T03:39:42.6681070Z` = 1 line | yes |

Fixed in `b8e51e9`: render with `.format()`, double this script's own literal braces, and
assert before dispatch that no escape sequence survived. Verified: 497 tests OK, plus the
one-line epoch above from the real host.

### Why the mocked tests could not have caught it

They returned a fabricated `CaptureResult` and never executed the script, so the only
observable they could check was the argv text - and they did not check it. The new
regression test asserts on that text (`assertNotIn("{{", script)`), which is the only
thing a non-Windows host can verify. This is the general lesson: on a foreign platform, a
mock that replaces the boundary proves the caller, never the command.

### Still open after this fix

- `same_process` returning True for a live Windows handle has not been re-verified on the
  host yet; the boot epoch is now available, so it is testable but not tested.
- 3 unit tests still fail on Windows for a test-portability reason (`IdentityProbe`
  injects a POSIX-shaped start identity that the Windows gate correctly refuses). Test
  defect, not a product defect, still unfixed.
