# CI triage: KooshaPari/KCode#216 — "Build & Test (desk-linux)"

Date: 2026-10-30
Repo: /Users/kooshapari/CodeProjects/Phenotype/repos/jcode-rebase-v0.90
PR branch: fix/code-size-ratchet-loc-neutral
CI run: 40963444190 / job 123761804736

## Failing command and error

The "Build & Test (desk-linux)" job runs the "Quality Guardrails" job's
ratchet gate:

```
python3 scripts/check_code_size_budget.py
```

Reproduced locally on the PR branch:

```
Code-size budget exceeded. Existing oversized Rust files must shrink or stay
flat, and new oversized production files are not allowed:
  - oversized file grew: src/cli/acp.rs (2200 -> 2447 LOC)
Run scripts/check_code_size_budget.py --update only after intentional cleanup.
```

(exit code 1)

Other ratchet gates (`check_test_size_budget.py`) pass: "Test-size budget OK:
tracked=49 threshold=1200LOC".

## Root cause

The PR's feature work (ACP read-only permission mode) grew the tracked
oversized production file `src/cli/acp.rs` from its baseline of 2200 LOC to
2447 LOC, violating the code-size ratchet's "existing oversized files may
not grow" rule.

## Causal file:line

- Baseline entry: `scripts/code_size_budget.json` -> `tracked_files["src/cli/acp.rs"] = 2200`
- Source file: `src/cli/acp.rs` (2447 lines after the change)
- New code responsible: the read-only permission-mode feature added in
  `src/cli/acp.rs` — `apply_permission_mode` (line ~1338), the
  `READ_ONLY_DENIED_TOOLS` list, `CONFIG_ID_MODE`/`MODE_READ_ONLY`/
  `MODE_UNRESTRICTED` constants, `permission_mode` field on `SessionUiState`,
  and `session_config_options` changes. The baseline 2200 predates this PR.

## Recommended fix (not applied)

Split the new permission-mode logic out of `src/cli/acp.rs` into a dedicated
module (e.g. `src/cli/acp_permission_mode.rs` or `src/cli/acp/permission_mode.rs`)
so `acp.rs` stays at or below its 2200-LOC baseline, OR perform an intentional
refactor and run `python3 scripts/check_code_size_budget.py --update` to bump
the baseline to the new size. Do not simply `--update` without cleanup, since
that erodes the ratchet.

## Where the fix belongs

In the PR branch (src/cli/acp.rs decomposition / baseline update), not in
fork CI config. The CI gate itself is working as designed.
