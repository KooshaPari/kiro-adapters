# Final Verification — 2026-11-04

Bounded read-only verification run against `main` @ `ce68c67`.

| check | result | evidence |
|---|---|---|
| 1. Working tree clean | PASS | `git status --short` produced no output |
| 1b. HEAD sha | INFO | `ce68c67` |
| 1c. Unpushed commits | INFO (25) | `git rev-list --count origin/main..HEAD` = 25; branch `main` |
| 2. unittest suite | PASS — 478 tests, OK, 0 failures | `PYTHONPATH=src python3 -m unittest discover -s tests -t .` -> `Ran 478 tests in 27.452s` / `OK` |
| 3. pytest argv test | PASS — 1 passed | `PYTHONPATH=src python3 -m pytest tests/test_acp_shim_cli_argv.py -q` -> `1 passed in 0.02s` |
| 4. adoption probe | PASS — 27 checks passed, 0 failed | `acceptance/shim_adoption_probe.py --session-id final-verify-77` -> `checks passed: 27, failed: 0`; `PASS: synthetic isolated adoption probe satisfied every check` |
| 5a. argv passthrough | PRESENT | `src/jcode_bridge/acp_shim/__main__.py:114` `argv = sys.argv[1:]` (also `:135` `main(sys.argv[1:])`) |
| 5b. -32012 refusal path | PRESENT | `src/jcode_bridge/acp_shim/protocol.py:29` `SHIM_PROMPT_FORWARDING_DISABLED = -32012`; raised in `src/jcode_bridge/acp_shim/server.py:234` inside `_session_prompt` when `not self.config.allow_prompt_forwarding` (message: "read-only ... does not forward session/prompt"); also surfaced at `protocol.py:153` |
| 5c. started-thread tracking + guarded join | PRESENT | `src/kiro_adapters/inventory/_windows_table.py:184` `started = []`; `:213` `started.append(thread)`; `:249-251` loops only `started` threads with `thread.join(_READER_JOIN_SECONDS)` guarded (comment at `:246-248` explains unstarted join would raise RuntimeError) |
| 6. `_windows_boot.py` exists | PRESENT | `src/kiro_adapters/inventory/_windows_boot.py` (3221 bytes); `read_windows_boot_epoch()` returns `None` on every failure; `windows_boot_epoch()` is `lru_cache`d once-per-run |
| 6b. same_process refuses unknown boot | PRESENT | `src/kiro_adapters/inventory/processes.py:262-264` (`recorded_boot == UNKNOWN_START` -> `return False`) and `:265-267` (`current_boot == UNKNOWN_START` -> `return False`) |

## Notes / deviations

- **pytest invocation requires `PYTHONPATH=src`.** Run exactly as specified (`python3 -m pytest tests/test_acp_shim_cli_argv.py -q` without `PYTHONPATH=src`) it fails collection with `ModuleNotFoundError: No module named 'jcode_bridge'`. With `PYTHONPATH=src` it passes. This is a harness/PYTHONPATH issue, not a code defect; the unittest discovery (which sets `PYTHONPATH=src`) passes. Flagged, not fixed.
- `same_process` lives in `src/kiro_adapters/inventory/processes.py`, not in `_windows_boot.py`; the boot-epoch reader it depends on is in `_windows_boot.py`. Criterion 6 is satisfied across those two files.
- Working tree is clean; 25 commits are ahead of `origin/main` and remain unpushed (no push performed).
