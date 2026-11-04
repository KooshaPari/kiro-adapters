# Live ACP adoption: first attached run against an externally-started daemon

Date: 2026-11-04 (~08:26-08:41 UTC)
Host: macOS aarch64, jcode 0.99.1
Module under test: `src/jcode_bridge/acp_shim`
Binary used: `/Users/kooshapari/.jcode/builds/current/jcode` (`jcode.real`)
Prior evidence this replaces: `acceptance/shim_adoption_probe.py`, which is
synthetic and never starts a daemon.

Everything below ran inside two disposable scratch roots. No real daemon was
contacted, no `~/.jcode`, `~/.kcode` or `~/.kiro` path was written, and no
pre-existing process was signalled.

## Safety preconditions

| Check | Value |
|---|---|
| Scratch root A | `/tmp/acp-live-kiro2` |
| Scratch root B | `/tmp/acp-live-kiro3` |
| `HOME` | `<root>/home` |
| `JCODE_HOME` | `<root>/jcode_home` |
| Socket (run B) | `/tmp/acp-live-kiro3/home/.jcode/scratch/jcode.sock` |
| Socket inside scratch root | asserted by `case` before anything started |
| Telemetry | `JCODE_NO_TELEMETRY=1` |

The only PIDs ever signalled were `6384` and `90509`, each re-verified with
`ps -o command=` to contain `serve --socket <its own scratch root>` immediately
before `kill -TERM`.

## Result summary

| Claim | Verdict |
|---|---|
| Isolated daemon starts inside scratch and binds only the scratch socket | OBSERVED PASS |
| Exactly one session created on it, id captured | OBSERVED PASS (`session_palmtree_1793781570983_6ba126eb93a04965`) |
| `session/load` forwards the exact id upstream and returns the daemon's answer | OBSERVED PASS (workaround required, see defect) |
| `session/new` is never forwarded | OBSERVED PASS (`-32601`, local) |
| `session/close` is never forwarded and is not a detach | OBSERVED PASS (`{"closed": false, "detached": true}`) |
| Session file byte-identical immediately after detach | OBSERVED PASS (SHA-256 unchanged) |
| Detach does not cancel or terminate the session | OBSERVED PASS (fresh client re-loaded it twice afterwards) |
| Shipped CLI flags (`--session-dir`, `--socket`, `--upstream`) work | **FAILED** — silently ignored |
| A brand-new, never-persisted session is adoptable | **FAILED** by design (gate requires `<id>.json`) |

## Defect found: the CLI entry point drops all of its own flags

`src/jcode_bridge/acp_shim/__main__.py` ends with:

```python
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

`main(argv=None)` delegates to `split_upstream(argv)`, which returns
`([], ["kcode", "acp"])` for `None`. So under the shipped entry point:

```bash
python -m jcode_bridge.acp_shim --session-dir "$SD" --socket "$SOCK" \
    --upstream "$J" acp --socket "$SOCK"
```

`--session-dir`, `--socket` and `--upstream` are **all discarded**. Proven by the
gate's own error payload, which reported the *default* search list while
explicit dirs were on the command line:

```
"searched": ["/tmp/acp-live-kiro3/home/.jcode/sessions",
             "/tmp/acp-live-kiro3/home/.kcode/sessions"]
```

This also means the shim's child is always a bare `kcode acp`, not the argv the
operator supplied. In run A, pointing it at
`/tmp/acp-live-kiro2/jcode_home/sessions` (which did contain the target file)
still produced `unknown_session`:

```
{"jsonrpc":"2.0","id":2,"error":{"code":-32010,
 "message":"no session file for this id; the shim only adopts sessions that already exist on disk and never creates one",
 "data":{"precondition":"unknown_session","willSpawnDaemon":false,
 "searched":["/tmp/acp-live-kiro2/home/.jcode/sessions",
             "/tmp/acp-live-kiro2/home/.kcode/sessions"]}}}
```

Fix (one line, deliberately **not** applied here so this acceptance run describes
the shipped behaviour): pass `sys.argv[1:]` into `main()`. A strict-xfail
regression test is committed at `tests/test_acp_shim_cli_argv.py`; it starts
passing when the fix lands.

The `--session-dir`/`--socket` flags are also the only reason the documented
environment overrides were used for the successful run:

```bash
JCODE_ACP_SESSION_DIR=<root>/jcode_home/sessions
JCODE_ACP_SOCKET=<root>/home/.jcode/scratch/jcode.sock
```

## What was actually run (run B, the clean one)

```bash
export SCRATCH_ROOT=/tmp/acp-live-kiro3
export HOME=$SCRATCH_ROOT/home
export JCODE_HOME=$SCRATCH_ROOT/jcode_home
export JCODE_NO_TELEMETRY=1
SOCK=$HOME/.jcode/scratch/jcode.sock

# 1. isolated daemon, explicit socket, inside scratch
$J serve --socket "$SOCK" >$SCRATCH_ROOT/serve.log 2>&1 &
# pid 6384

# 2. exactly one session, via the real ACP front end on that socket
python3 acp_helper.py new
# ACP_SESSION_ID=session_palmtree_1793781570983_6ba126eb93a04965
# exit 0

# 3. one prompt; the provider fails (no credentials in scratch HOME), but the
#    daemon persists the session at turn end
python3 acp_helper.py prompt
# error -32000 "No tokens/providers left", then:
# EVENT event=SESSION_PERSISTENCE ... phase=save_done result=ok
#   status=Closed path=$JCODE_HOME/sessions/session_palmtree_....json

# 4. adoption through the shipped entry point
JCODE_ACP_SESSION_DIR=$JCODE_HOME/sessions JCODE_ACP_SOCKET=$SOCK \
  python3 -m jcode_bridge.acp_shim
```

The session file only came into existence after step 3, not after `session/new`.
Before that, run A showed the daemon logging
`Retaining idle unsaved session ... for reconnect grace` and writing no JSON at
all, so the gate's `unknown_session` was correct rather than a bug: the ACP
session id from `session/new` is not yet an on-disk session id.

## Observed transcript

Target file:
`/tmp/acp-live-kiro3/jcode_home/sessions/session_palmtree_1793781570983_6ba126eb93a04965.json`

| Request | Reply |
|---|---|
| `initialize` | `loadSession: true`, `sessionCapabilities: {"resume":{},"list":{}}` (no `close`), one mode `read-only`, `spawnsDaemon: false` |
| `session/load` | `result.configOptions = [{"category":"permission","id":"mode","name":"Permission mode",...}]` — this payload is the daemon's answer, produced only after the gate passed |
| `session/new` | `error -32601 Unsupported ACP method: session/new` |
| `session/close` | `result {"closed": false, "detached": true, "reason": "the shim never closes an operator session; drop the connection"}` |
| `session/load` (bogus id) | `error -32010 unknown_session`, `willSpawnDaemon: false` |

File integrity:

```
SHA256_BEFORE=94625034fa4afc318538cf628746c135fe4293aaf4f1d4a5c9460d30567923ed
SHA256_AFTER =94625034fa4afc318538cf628746c135fe4293aaf4f1d4a5c9460d30567923ed
SESSION_FILE_UNMODIFIED=True
SESSION_FILES_AFTER=["...palmtree....bak","...palmtree....json"]
SERVE_PROCESSES_BEFORE=2   (kiro2 daemon + kiro3 daemon)
SERVE_PROCESSES_AFTER =2   (unchanged: adoption spawned no daemon)
```

Detach was stdin EOF. The shim exited (code 4), and two separate later ACP
clients re-loaded the same id successfully, replaying `user_message_chunk "hi"`,
which proves the session was neither closed nor cancelled. Note that those
*verification* loads themselves rewrote the file (final hash
`095dda4f...`), so the byte-identical claim is specifically about the window
between adoption and detach.

## Inferred, not observed

- Run B's first adoption attempt (before the env override was used) returned
  `configOptions` while the kiro3/kiro2 daemon log showed no load for the target
  id in that window. The only candidate is the upstream-spawned temporary
  daemon at the scratch default socket. This is INFERRED from the log gap plus
  the `jcode acp --socket` help text ("the first such tool starts a temporary
  server here that exits with the run, leaving no daemon behind"). No extra
  process survived, and every such path was inside scratch.
- Because `--upstream` is ignored, the shim's child is always bare `kcode acp`.
  With no live socket that child would call `ensure_daemon` and start a daemon,
  which is exactly what the gate exists to prevent. Run B is a near miss for
  that failure mode; it was contained only because the scratch default socket
  was live.

## Cleanup

```
STOPPING_OWN_CHILD tag=acp-live-kiro3 pid=6384  -> EXITED
STOPPING_OWN_CHILD tag=acp-live-kiro2 pid=90509 -> EXITED
daemons after cleanup: none
```

Stale socket files remain under the two scratch roots (harmless, inside
`/tmp`); no `~/.jcode`, `~/.kcode` or `~/.kiro` file was created or modified.

## Recommended follow-ups

1. Fix `__main__.py` to call `main(sys.argv[1:])`; flip
   `tests/test_acp_shim_cli_argv.py` from strict xfail to a plain test.
2. Treat "no on-disk session id yet" as a first-class adoption error. An ACP
   client that just called `session/new` has an id the gate can never resolve;
   the error message should say "this session has not been saved yet", not
   "does not exist".
3. Decide whether a `session/load` that passes the gate should be pinned to the
   socket the operator named. Today the upstream child re-derives its own
   socket, so the shim's gate and its child can disagree about which daemon
   they are talking to.
