# Work DAG: KiroCrew / jcode adoption close-out

Status: ACTIVE. Machine-readable twin: `state/work_dag.json`.
Resume protocol: read this file, then `git log --oneline -15`, then `state/work_dag.json`.
If the session was killed, the DAG tells you where to restart. Nothing here depends on chat history.

## Blind-closure contract

This plan is designed so the operator can terminate the session at any instant and lose
at most **one in-flight node**. The rules that make that true:

1. **Every node's output is a committed file, not a chat message.** A finding that lives
   only in a transcript is lost on kill; a finding in git is not.
2. **No node may hold uncommitted state across a node boundary.** Commit before starting
   the next node.
3. **`state/work_dag.json` is updated in the same commit as the node's evidence.** The
   status and the proof travel together, so status can never drift from reality.
4. **DONE is only claimed with an evidence path and a verified commit hash.** A node with
   no observable hash is `UNKNOWN`, never `DONE`. This is the same rule the repo already
   uses for upstream mode.
5. **Re-running a DONE node is a no-op** that re-reads its evidence file. Idempotent by
   construction, so a confused successor cannot corrupt progress by retrying.
6. **`git log --oneline` alone is sufficient to see progress.** No side channel needed.

Corollary: a node that cannot be made durable should be split until it can.

## Status vocabulary

`DONE` (evidence + hash) · `NOT_RUN` · `BLOCKED_OPERATOR` (needs a human choice) ·
`BLOCKED_ENV` (needs a host or credential) · `UNKNOWN` (attempted, no verifiable result).

## Nodes

| ID | Node | Depends | O/M/P (10m blocks) | PERT | Status |
|---|---|---|---|---|---|
| P0 | Persistence spine: this DAG + `state/work_dag.json`, committed and pushed | - | 1/1/2 | 1.2 | DONE |
| P1 | Shim binary forensics: does the shipped KCode build carry `71f7c0c44`'s wider denial set? | P0 | 1/2/4 | 2.2 | NOT_RUN |
| P2 | **Operator gate**: pick `agent.sandbox_allow_unsandboxed_exec` = `true` or `false` on `desk` | P0 | 0/0/0 | 0 (human) | BLOCKED_OPERATOR |
| P3 | Apply the P2 value on `desk`; re-verify with a real `chat -m` invocation | P2 | 1/2/3 | 2.0 | BLOCKED_OPERATOR |
| P4 | Write `~/.kiro/crew/.env` with `CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim` | P0 | 1/1/2 | 1.2 | NOT_RUN |
| P5 | Full gateway stop/start (`.env` edits are only read at boot) | P4 | 1/2/4 | 2.2 | NOT_RUN |
| P6 | Verify read-only mode is actually active, by real chat, not by config inspection | P5 | 1/3/6 | 3.2 | NOT_RUN |
| P7 | Dispose of the stale `docs-5` branch `release-lifecycle-v2-scope-candidate-20260929` | P0 | 1/1/2 | 1.2 | NOT_RUN |
| P8 | Record the real log/disk trend (`gateway.err`, `diag/` ~3.4 MB/day, 53 GiB free) | P0 | 1/1/2 | 1.2 | NOT_RUN |
| P9 | Integration verify: full test suite + one live end-to-end chat | P1,P3,P6,P7,P8 | 2/3/5 | 3.2 | NOT_RUN |
| P10 | Close-out: update `07_PILOT_RESULTS.md` + `ACP-ADOPTION-SHIM.md` with measured outcomes | P9 | 1/1/2 | 1.2 | NOT_RUN |

PERT expectation = (O + 4M + P) / 6, in 10-minute blocks. These are **block counts sized
against the repo's 10-minute convention**, not measured wall-clock durations. Per the
no-fabricated-durations rule, treat them as effort units and not as a promise about time.

## Critical path

```
P0 -> P4 -> P5 -> P6 -> P9 -> P10   = 1.2 + 1.2 + 2.2 + 3.2 + 3.2 + 1.2 = 12.2 blocks
```

P2 is deliberately **off** the critical path. It is a human decision with unbounded
latency, so putting it on the spine would stall all Mac-side work behind a Windows
question. Instead the Mac lane (P4-P6) and the independent lanes (P1, P7, P8) run
regardless, and P3 joins at P9 when the operator answers.

If the operator never answers P2, every other node still completes and P9 can be
satisfied with P3 recorded as `BLOCKED_OPERATOR` rather than faked as `DONE`.

## Why this is the best path

- **P4-P6 before P1-P3.** The shim exists but nothing points KiroCrew at it, so the
  capability is built and unused. That is the shortest distance between "we wrote it" and
  "it does something", and it needs no external host.
- **P1 is forensics, not a prerequisite.** Whether the shipped binary carries the wider
  denial set is a bounded question. It informs the writeup; it must not gate the wiring.
- **P2 is isolated on purpose.** It is the only node needing an operator decision, and it
  concerns a different host. Confining it prevents it from being a global blocker.
- **P7-P8 are cheap and independent.** They cost one block each and remove two stale
  premises that previously consumed a full dispatch on wrong assumptions.

## Known UNKNOWNs carried forward

- Whether the installed Windows capability `interactionController` is advertised. Source
  says `cfg!(unix)`; the installed binary was not probed. UNKNOWN, not absent.
- `harness/_common.py` and Electron `app.asar` are unread.
- Windows GUI gateway respawn after a CLI stop is not proven linked.

None of these block the critical path; all are recorded so a successor does not re-derive
them from scratch or, worse, assert them.
