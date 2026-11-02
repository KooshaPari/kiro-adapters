# Codex backend activation: why the jcode backend was inert

Measured 2026-11-02 on this host. This closes DAG node P6 (read-only mode
actually active, by real chat) and explains the HTTP 5xx that had been recorded
as an unrelated blocker.

## The two defects, in the order they were hiding each other

### 1. `agent.acp_backend` was unset, so the codex slot was never selected

`CODEX_ACP_BIN` was set correctly, both in `~/.kiro/crew/.env` and in the live
gateway's environment:

```
$ ps eww -p <gateway-pid> | tr ' ' '\n' | grep CODEX_ACP_BIN
CODEX_ACP_BIN=/Users/kooshapari/bin/jcode-acp-shim
```

It was nevertheless inert. Kiro Crew resolves the codex ACP harness only when
that backend is the selected one, and the config had no backend selected:

```
$ python3 -c "import json;print(json.load(open('$HOME/.kiro/crew/config.json'))['agent'].get('acp_backend'))"
None
```

The selectable values come from the application itself, not from guessing:

```
$ <KiroCrew.app python> -c "from kiro_crew.agent_sdk.backends import selectable_backend_values; print(selectable_backend_values())"
selectable: ['', 'claude', 'codex', 'deepseek', 'goose', 'kas', 'opencode', 'pi']
```

`''` (kiro-cli) is the default, and `agent.kas_bundle: true` routes KAS to
Crew's own engine, so the live agent was served by KAS.

**Proof that the shim was previously never spawned.** The shim was instrumented
to append a line on every invocation, then a real turn was run:

```
$ kirocrew ask "Reply with exactly: SHIM_PROBE"
ok: True   answer: 'SHIM_PROBE'
$ cat /tmp/jcode-shim-probe.log
*** SHIM WAS NEVER INVOKED ***
```

The turn answered successfully without the shim ever running. That is why
earlier "Kiro Crew running on jcode" evidence measured the shim in isolation
rather than in the live path.

**Fix.** Set the backend, then restart, because config is read at boot:

```
$ python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.kiro/crew/config.json')
d = json.load(open(p))
d.setdefault('agent', {})['acp_backend'] = 'codex'
json.dump(d, open(p, 'w'), indent=1)
PY
$ kirocrew restart
```

After the restart the same instrumentation fired:

```
2026-11-02T09:20:02Z invoked argc=0 argv=[]
```

`argc=0` confirms the codex slot spawns its binary with no argv, which is why
the shim has to supply the `acp` subcommand itself.

### 2. The ACP adapter's default model was unservable here

With the shim finally spawning, the turn failed:

```
ok: False | code: ERROR | detail: no text returned
❌ The model backend hit a transient error (HTTP 5xx).
```

The handshake itself was healthy; `session/new` advertised a Bedrock model:

```
"models": { ... "currentModelId": "anthropic.claude-3-5-sonnet-20241022-v2:0" }
```

That model is not servable on this host, so the 5xx was **misconfiguration, not
a provider outage**. It had been recorded as "provider HTTP 5xx" and treated as
an external blocker.

`jcode acp` accepts `-p/--provider` and `-m/--model`, so the shim pins a route
verified serving here, with env overrides:

```bash
PROVIDER="${JCODE_ACP_SHIM_PROVIDER:-opencode-go}"
MODEL="${JCODE_ACP_SHIM_MODEL:-deepseek-v4.1-flash}"
if [ "${1:-}" = "acp" ]; then
  exec "$JCODE" "$@" -p "$PROVIDER" -m "$MODEL"
fi
exec "$JCODE" acp "$@" -p "$PROVIDER" -m "$MODEL"
```

## What was observed after both fixes

Turn 1, with the shim still instrumented, which proves the jcode path served it:

```
$ kirocrew ask "Reply with exactly: JCODE_BACKEND_OK"
ok: True | code: OK | detail: 16 chars
answer: 'JCODE_BACKEND_OK'
$ cat /tmp/jcode-shim-probe.log
2026-11-02T09:24:09Z invoked argc=0 argv=[]
```

Turn 2, after removing the instrumentation, so this is the shipped state:

```
$ kirocrew ask "Reply with exactly: CLEAN_VERIFY_OK"
ok: True | code: OK | answer: 'CLEAN_VERIFY_OK' | ms: 28616
```

## Read-only enforcement, measured rather than assumed

P6 asked whether read-only mode is actually active. Rather than reading the
capability advertisement, the agent was asked to write a file:

```
$ kirocrew ask "Create a file at /tmp/readonly_probe.txt containing the word WROTE. Then reply DONE."
ok: True
answer: 'I could not complete this, so I am not replying DONE.
- This session has no write/bash/edit tool: only `read`, `ls`, `agentgrep`, and MCP servers.
- The only write-capable path, `node_repl`, is dead in this environment: ...'
$ ls /tmp/readonly_probe.txt
(absent)
```

The write-capable tools are absent from the session's tool set and the file was
not created. Read-only is enforced, not merely advertised.

## Reproduce

```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.kiro/crew/config.json')))['agent'].get('acp_backend'))"
# expect: codex
kirocrew restart
kirocrew ask "Reply with exactly: JCODE_BACKEND_OK"
# expect: ok True, answer JCODE_BACKEND_OK
```

## Reversal

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.kiro/crew/config.json')
d = json.load(open(p))
d['agent']['acp_backend'] = ''
json.dump(d, open(p, 'w'), indent=1)
PY
kirocrew restart
```

KAS is unaffected: `agent.kas_bundle` stays `true`, and the KAS engine remains
installed. Only the harness the ACP seam spawns changes.

## Caveat worth keeping

This host's Kiro Crew now runs the jcode ACP adapter for the default agent. Two
consequences are real and should not be discovered later by surprise:

- The jcode route draws on the `opencode-go` pool, not on Kiro's own backend.
  When that pool is exhausted the turn fails with an HTTP error that looks like
  an ACP fault but is not.
- `kirocrew doctor` reports on the KAS route. A green doctor does not mean the
  codex/jcode path is healthy, and vice versa.
