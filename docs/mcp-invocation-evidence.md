# Evidence: does a real Kiro Crew agent turn invoke the `agent_sources` inventory tool?

Date: 2026-11-04 (UTC 08:34-08:41)
Host: this Mac (macOS, aarch64)
Gateway: already-running Kiro Crew Nightly backend, PID 81136, `127.0.0.1:5476`
Constraints honored: no config edits, no gateway restart, nothing installed, no process killed,
no tokens or message bodies printed.

## Verdict, plainly

**INVOCATION OBSERVED, EXECUTION FAILED.**

A real agent turn *did* call the tool: the session transcript contains a structured
`role: "tool"` row whose `meta.input` names `server: "kiro-inventory"`, `tool: "agent_sources"`.
The call did not succeed. The tool result was:

```
Error: MCP server 'kiro-inventory' not connected
```

So the previous state ("registered, listed as enabled, but never observed invoked") is now
resolved one level deeper: the agent *chooses and dispatches* the tool, and the MCP runtime in
that agent session has no connection for it.

## Evidence trail

### 1. Where the structured records actually live (discovered, not guessed)

- Gateway log: `~/.kiro/crew/gateway.log` (WARNING+ only; contains no `kiro-inventory` lines at all,
  before or after the turns - `grep -n "kiro-inventory\|agent_sources" ~/.kiro/crew/gateway.log`
  returns nothing). The gateway log is therefore **not** a usable source for tool invocation proof.
- Session transcripts: `~/.kiro/crew/sessions/*.jsonl` (one row per event). This is the authoritative
  structured store. Tool rows are `{"role": "tool", "meta": {"tool_call_id", "input", "output", "done"}}`.
- Structured event-name strings present in the installed source
  (`.../site-packages/kiro_crew`): `tool_call`, `tool_call_update`, `tool_name`, `tool_result`,
  `mcp_server_name`, `mcp_server_initialized`, `mcp_server_init_failure`. Those are internal
  identifiers, not lines in `gateway.log`, which is why log-grepping alone kept coming up empty.

### 2. Request path used

`POST http://127.0.0.1:5476/api/chat` with header `X-Internal-Secret` (read from
`~/.kiro/crew/.local_secret`, never printed), body `{"message": ...}`.
Route confirmed at `.../kiro_crew/dashboard/routes/chat.py:68` -> `chat.api_chat`
(`.../kiro_crew/dashboard/chat_api/chat_send.py:214`). Preflight `GET /api/chat/slots` -> `200`.
Helper: `scripts/probe_agent_sources_invocation.py`.

### 3. Turn 1 - 2026-11-04T08:34:01Z start, 58.3 s, HTTP 200

New transcript: `~/.kiro/crew/sessions/dashboard_chat-2-1793781242.jsonl`

Tool row (verbatim structure, `meta` only):

```
tool_call_id = call_74dca8db14ba4322afade95d
input  = {"arguments": {}, "intent": "Count all agent sources with no filters",
          "server": "kiro-inventory", "tool": "agent_sources"}
done   = true
output = "Error: MCP server 'kiro-inventory' not connected"
```

Probe exit code: `0` (its predicate is "a tool record naming agent_sources from kiro-inventory
exists"), which is why the *doc* - not the exit code - carries the success/failure distinction.

### 4. Turn 2 - 2026-11-04T08:38:20Z start, 147.9 s, HTTP 200 (retry)

New transcript: `~/.kiro/crew/sessions/dashboard_chat-3-1793781501.jsonl`

Three tool rows, none naming `agent_sources` directly:

```
input = {"intent": "Find the kiro-inventory agent_sources tool", "query": "agent_sources"}   -> []
input = {"intent": "List tools from kiro-inventory server", "server": "kiro-inventory"}      -> []
input = {"action": "list", "intent": "Check whether kiro-inventory MCP server exists"}        ->
        Connected MCP servers: 0 (0 tools)
        Configured but not connected:
          - agent-imessage, node_repl, pheno-gpu, phinbox-mcp, portal-bridge, workos-docs
```

Two facts follow, and they matter more than the error string:

1. The agent session's MCP runtime has **0 connected servers**.
2. `kiro-inventory` does not appear even in the "configured but not connected" list. The six servers
   listed there are not the servers in `~/.kiro/crew/mcp.json` either, so the agent session's MCP
   set is not the same projection as the host-level one that `GET /api/mcp/active` reported.

### 5. Corroborating gateway log lines during the same window

`~/.kiro/crew/gateway.log` grew 35180 -> 37540 bytes across turn 1. Only WARNING lines, e.g.:

```
00:34:32 WARNING kiro_crew.providers.mirrors.codex [PID 81136]: codex session MCP: withholding server 'kirocrew-*' ...
00:34:40 WARNING kiro_crew.acp.mcp_ref_guard [PID 81136]: agent-spec tool refs name no MCP server in Crew's projection for this session ... mcp_gateway=off
00:34:47 WARNING kiro_crew.agent_sdk.spec_hooks [PID 81136]: agent 'kirocrew': spec postToolUse matcher 'execute_bash' ...
```

`mcp_gateway=off` and the "withholding server" warnings are consistent with a session whose MCP
projection is narrower than the host's `/api/mcp/active` view.

## What is NOT claimed

- Not claimed: a successful `agent_sources` tool result was ever returned. It was not.
- Not claimed: `GET /api/mcp/active` is wrong. It reports the host projection; the agent session
  uses a different set.
- Not claimed: the stdio server binary is broken. A separate process is running
  `kiro_adapters.mcp_server` (PID 54714, started 17:54), and no handshake was attempted from the
  session because the session never had a configured entry to launch.

## Blocker requiring a restart-free fix (not performed)

The agent session's MCP projection must include `kiro-inventory` for anything to connect. That is a
gateway/session-configuration change, which this task's hard rails forbid. A gateway restart may or
may not be needed to pick up a projection change; that is unverified here because no restart was
attempted.

Suggested next probe, once projection is fixed: rerun
`python3 scripts/probe_agent_sources_invocation.py --timeout 150` and require a tool row whose
`meta.output` is a source list rather than an `Error:` string.

## Reproduction

```bash
python3 scripts/probe_agent_sources_invocation.py --timeout 150
# then read the newest transcript:
ls -t ~/.kiro/crew/sessions/*.jsonl | head -1
```
