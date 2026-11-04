# MCP Integration Proof — kiro-inventory / agent_sources

Date: 2026-11-04
Scope: VERIFY ONLY. No config or launcher edits, gateway not restarted. One attempt.

## Verdict

**(c) NOT PROVEN — no tool row.**

No `role: "tool"` row exists because **no session transcript was written at all** for
this turn. The Kiro Crew session store gained no file, so there is nothing to inspect
for a tool row. Absence of a session row is not evidence that the tool executed, and
it is not evidence that it failed — it is absence of evidence.

## Turn execution

| Item | Value |
| --- | --- |
| Command | `kirocrew chat --agent kirocrew -m "Call the kiro-inventory tool agent_sources ..."` |
| Bound | 150 s (`timeout 150`) |
| Exit code | **0** |
| Duration | **103 s** (2026-11-04T01:14:50Z → 01:16:33Z) |
| Attempts | 1 |

Exit code 0 is recorded for completeness only and is not treated as proof.

## Session store delta

| Checkpoint | Count | Newest file | Newest mtime |
| --- | --- | --- | --- |
| Before turn (01:14:47Z) | 35 | `dashboard_chat-3-1793781501.jsonl` | 2026-11-04 00:40:51 |
| After turn (01:16:36Z) | 35 | `dashboard_chat-3-1793781501.jsonl` | 2026-11-04 00:40:51 |

**File this turn wrote: none.** Count unchanged (35 → 35) and the newest mtime
predates the turn by ~34 minutes, so no pre-existing file was appended to either.

Because the store gained no file, no attempt was made to read `role: "tool"` rows —
there are no rows to read. Per instruction, the run stops here rather than retrying.

## Connected-server metadata

Not available for this turn: connected-server metadata is carried in the session
record, and no session record was produced. A grep of the only other structured
artifact the turn touched, `~/.kiro/crew/diag/snapshots-20261104.jsonl`, returns 0
occurrences of both `kiro-inventory` and `agent_sources`. That snapshot is a
host-resource record (`ts`, `pid`, `load*`, `mem_*`, `mounts`, `gateway`, ...) and
was never a carrier of per-session MCP server lists.

`codex mcp list` reporting `kiro-inventory ... status enabled` therefore remains the
only positive signal, and it is configuration-level, not execution-level.

## What the stderr does establish (metadata only)

Relevant lines, redacted to metadata:

- `agent-spec tool refs name no MCP server in Crew's projection for this session:
  backend='codex' agent='kirocrew' unresolved=@kirocrew-computer, @kirocrew-guide,
  @kirocrew-mate mcp_gateway=off`
- Three `codex session MCP: withholding server 'kirocrew-*'` warnings for
  `kirocrew-computer`, `kirocrew-guide`, `kirocrew-mate` — note that **kiro-inventory
  is not among the withheld servers**, and is not among the unresolved refs either.

This is consistent with `kiro-inventory` being mounted rather than withheld on the
codex backend, but it says nothing about whether `agent_sources` was invoked. It
cannot substitute for a tool row.

## Interpretation

The chain that is proven: launcher survives an empty environment (`env -i` handshake
returns 4 tools including `agent_sources`), and `[mcp_servers.kiro-inventory]` is
registered in the config codex-acp reads. The chain that is **not** proven: that a
live Kiro Crew turn actually dispatched `agent_sources` and received a payload.

The blocking gap is transcript persistence, not the MCP server. Until a `kirocrew
chat` turn writes a `sessions/*.jsonl` record, every future run of this check will
land on verdict (c) regardless of whether the tool works. Resolving where this
invocation path persists transcripts (dashboard sessions write; this CLI path did
not) should precede another execution attempt; repeating the same turn would only
reproduce the same absence.

## Follow-up

1. Determine why the `kirocrew chat --agent kirocrew` path writes no session
   transcript while dashboard chat sessions do.
2. Once transcripts persist, rerun this exact bounded turn and inspect `role: "tool"`
   rows for `kiro-inventory` / `agent_sources`.
3. Only a tool row with `meta.done == true` and a non-error payload upgrades this to
   INTEGRATION PROVEN.
