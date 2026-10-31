# Shim binary forensics: does the shipped KCode build carry the widened read-only denial set?

**Question.** Does the shipped read-only KCode build actually carry the wider denial set
introduced by commit `71f7c0c44`? A prior session proved the shim binary carries
*read-only* strings, but whether it includes that later **widening** was left a bounded
UNKNOWN.

**Verdict: PRESENT.** The shipped binary contains the widened array, in source order,
anchored immediately after the parent commit's last member.

---

## 1. Verified repository state

| Item | Value |
|---|---|
| Forensics repo | `/Users/kooshapari/CodeProjects/Phenotype/repos/jcode-acp-native` |
| Branch | `fix/acp-session-list-window` (tracks `origin/fix/acp-session-list-window`) |
| **Verified HEAD** | **`e373d80ed`** — `fix(acp): decide session/list order from the directory listing, not by loading every session` |
| Working tree | clean |

The target commit is **not an ancestor of that HEAD**:

```
git merge-base --is-ancestor 71f7c0c44 HEAD   -> NO
git for-each-ref --contains 71f7c0c44         -> (empty: unreachable from every ref)
```

So `71f7c0c44` exists in the shared object store but sits on no branch in this tree. That
is stated as an observation about this repo only; it does not imply the commit is absent
from GitHub or from the operator's other checkouts.

## 2. Commits examined

| Role | Hash | Date (-0700) | Subject |
|---|---|---|---|
| Target (widening) | `71f7c0c44d53c7b4d95bb740504d1b340d2b05c1` | Thu Oct 29 03:33:33 2026 | `feat(acp): widen the read-only denial and prove the enforcement` |
| Parent (base read-only mode) | `132db5bd12d1b922438e6c5dedd129dc5e75f4b6` | Thu Oct 29 03:07:50 2026 | `feat(acp): advertise and enforce a read-only permission mode` |
| **Binary build commit** | **`71bfaa8d06c7564fe325672e25ff25d4b1dc0078`** | Wed Oct 28 02:44:24 2026 | `fix(ci): keep the code-size ratchet honest after the workspace clippy gate` |

`git show --stat 71f7c0c44`: `src/cli/acp.rs` +15, `crates/jcode-app-core/src/tool/tests/sdk.rs` +55 (70 insertions, 0 deletions).

### The concrete new denial behavior

Before (`132db5bd1`), the read-only denial array ended at:

```
"selfdev",
"desktop_selfdev",
```

`71f7c0c44` appends, with the comment *"Tools that run other tools, spawn agents, or open a
machine-driving UI."*:

```
"batch", "swarm", "applet", "panel", "side_panel",
"skill_manage", "integration_tools", "elicitate_mcp",
```

These are names that are state-changing without saying "write": `batch` runs other tools,
`swarm` spawns agents, `applet`/`panel`/`side_panel` open a machine-driving UI, and
`skill_manage`/`integration_tools`/`elicitate_mcp` change state by another route.

## 3. The binary

| Item | Value |
|---|---|
| Path | `~/.jcode/builds/versions/0.99.1-kcode-mode/jcode` |
| Size | 317,900,624 bytes |
| Type | Mach-O 64-bit executable arm64 |
| mtime | Oct 29 15:34 |
| **Observed identity** | **`kcode v0.0.0-dev (71bfaa8d0, dirty)`** |

Read via `--version` only. The binary was not run in any mode that mutates state.

**The lineage alone would say ABSENT, and that reading is wrong.** The build commit
`71bfaa8d0` is dated ~25 h *before* both `132db5bd1` and `71f7c0c44`, and neither is its
ancestor. But the identity string carries **`dirty`**: the tree that was compiled did not
equal commit `71bfaa8d0`. The hash therefore does not describe the built tree, which is
exactly why a binary-level test was required instead of trusting `git`.

## 4. Deciding literal

The discriminating bytes are one packed run starting at offset **106,261,142** and ending
at the first of three `elicitate_mcp` occurrences, which begins at offset **106,261,212**:

```
desktop_selfdev | batch | applet | panel | side_panel | skill_manage | integration_tools | elicitate_mcp | medium
```

(An earlier revision of this file rendered the last member as `elicit_mcp`. That was a
display truncation, not the observed bytes: the literal in the binary is the full
`elicitate_mcp`.)

`lfdev` is the tail of **`desktop_selfdev`**, the parent commit's *last* array member. The
run that follows matches the `71f7c0c44` array **in source order**, member for member. A
coincidental ordering of seven unrelated literals starting at that exact anchor is not a
credible explanation.

`swarm` does not appear inside that run; it is deduplicated elsewhere in the binary
because `swarm` is also a live tool name. That is why order matching is 7 of 8 members plus
the correct anchor, not 8 of 8.

## 5. Method, and two invalid measurements discarded

1. **`grep -P` results were invalid.** macOS BSD `grep` has no `-P`. A batch of
   raw-byte adjacency tests returned `0` for every pattern, including a control
   (`read-only`) that `strings` had already found 31 times. Those zeros were tool failure,
   not absence, and were discarded rather than reported.
2. **NUL-adjacency is the wrong test.** LLVM merged the consecutive string literals into a
   contiguous blob with no NUL separators, so `batch\0swarm\0` style probes return `0`
   even when the array is fully present.
3. **Validated method.** Python byte search with `read-only` and `bash` as positive
   controls, which returned 31 and 3,648 occurrences respectively, confirming the reader
   was correct before any negative was believed.

## 6. Evidence cells

| Dimension | Cell | Source |
|---|---|---|
| Build identity | `PASS` | `--version` -> `kcode v0.0.0-dev (71bfaa8d0, dirty)` |
| Commit contents | `PASS` | `git show 71f7c0c44` in `jcode-acp-native` |
| Lineage vs build commit | `FAIL` (expected) | `merge-base --is-ancestor` -> NO, both directions |
| Widened array present in binary | `PASS` | packed run at offset 106261212, anchored on `desktop_selfdev` |
| Standalone NUL-delimited literals | `NOT_APPLICABLE` | literals are packed, so the test cannot discriminate |
| Runtime enforcement | `NOT_RUN` | out of scope for this bounded task; no mutating run |

## 7. Residual caveat

Binary identity is established by embedded commit hash **plus observed bytes**, because the
build was flagged `dirty`. This proves the widened literals are compiled into this
artifact. It does **not** prove runtime enforcement: that the daemon drops those tools from
a live session's tool definitions is a separate claim, already covered by the test added in
`71f7c0c44` and by `acceptance/read-only-mode-verification.md`, and was not re-proven here.
