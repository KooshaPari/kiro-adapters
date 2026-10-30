# Upstream mode port: verification record

Date: 2026-10-30
Upstream: https://github.com/1jehuang/jcode.git  (branch `master`)
Fork mirror of the port: https://github.com/KooshaPari/KCode branch `feat/upstream-acp-read-only-mode`
Local worktree: ~/.jcode/scratch/upstream-mode-port, branch `feat/acp-read-only-mode`

## What was ported

The fork's ACP `mode` config-option feature (KCode commits `4d0158a3c` and
`93d5e76fc`) into upstream jcode. Upstream did not have it.

Diff vs upstream master: 3 files, +216 / -10.

| File | Change |
|---|---|
| `src/cli/acp.rs` | +62 / -1 |
| `src/cli/acp/session_configuration.rs` | +86 / -8 |
| `src/cli/acp/tests.rs` | +78 / -1 |

## Behaviour

- The `mode` config option is advertised FIRST and UNCONDITIONALLY, with the
  single value `read-only`, before a host asks for it.
- Selecting `read-only` sends `ConfigureTools` disabling the write-capable and
  state-changing tools. The read-only tools stay mounted.
- Unimplemented modes are refused, and `currentValue` reports what was applied.
- `wait_for_ack` matches upstream's `Ack`, not `Done`: upstream's daemon answers
  `configure_tools` with `Ack` and never sends `Done` (see
  `jcode-app-core` `client_lifecycle.rs`, SDK-control branch).

## Verification

`origin/master` had moved 51 commits since the port was prepared. The branch
was rebased onto it with **no conflicts**.

```
$ cargo test --lib acp::tests
test result: ok. 25 passed; 0 failed; 0 ignored; 0 measured; 372 filtered out; finished in 0.02s
```

That line was produced by an independent verification run (a separate
`CARGO_TARGET_DIR` at `~/.jcode/scratch/upstream-port-target`), not by the
agent that prepared the branch.

## Why it is not pushed to upstream

Publishing to `1jehuang/jcode` from this account is forbidden by operator
policy (2026-10-15): no PRs, no issues, no discussions, no API writes against
someone else's repository from `KooshaPari/*`. Reading upstream and mirroring
work into the fork are both allowed; opening a PR against upstream is not.

So the port lives on the fork as `feat/upstream-acp-read-only-mode`, verified
and ready. If the operator wants it upstream, it has to go out under the
operator's own upstream credentials and decision.
