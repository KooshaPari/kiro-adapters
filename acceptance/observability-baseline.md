# Observability baseline, and what actually starved the gateway

Measured 2026-10-30/31 on this Mac. This file replaces two **refuted** premises and
records the real cause of a gateway outage that was first mis-read as a regression.

## The refuted premises (do not re-investigate)

| Premise | Measured | Verdict |
|---|---|---|
| 150 MB `gateway-launch.log` needs preserve-and-truncate | **39,692 B**, 3 days stale, at `~/Library/Logs/kirocrew-desktop-nightly/` | REFUTED (off by ~3,964x) |
| ~7 minute gateway bind from a slow discovery root | port 5476 LISTEN, `/api/health` **200 in 4.37 s** cold and **0.009 s** warm on a quiet host | REFUTED |
| `security_events.lock` failure needing diagnosis | no such lock; `security_events.jsonl` is a **journal**, 11.9 MB, no lock involved | REFUTED |

## Current sizes

| Path | Size | Note |
|---|---|---|
| `~/.kiro/crew/security_events.jsonl` | 11.9 MB | appended, written today |
| `~/Library/Logs/KiroCrew/gateway.err` | 132 K | active |
| `~/.kiro/crew/gateway.log` | 67 K | active |
| `~/.kiro/crew/notifications.jsonl` | 25 K | |
| `~/.kiro/crew/diag/` | 4.0 MB | ~3.4 MB/day trend |
| Disk `/` | 926 Gi, **77 Gi available**, 25% used | up from 53 Gi earlier today |

Nothing here is unbounded. **No log rotation is needed**, and no cleanup was performed.

## What actually happened: the gateway was starved, not regressing

During a config test the gateway took 70-300 s to bind where it took ~8-14 s before,
and `/api/health` answered in 3-8 s instead of 0.009 s. That looked like a regression
from the config change. It was not. The host measured:

```
load averages: 327.34 331.18 261.38        (16 cores)
870 processes: 611 S, 171 R, 87 Z
```

171 runnable against 16 cores. The gateway's own log agrees:

```
kiro_crew.dashboard.server: event-loop heartbeat: lag 1.9s (loop was blocked)
kiro_crew.dashboard.loop_watchdog: event-loop heartbeat lag 1.9s
```

The consumers were **other sessions' work**, not KiroCrew:

| Consumer | Observed |
|---|---|
| `cargo-mutants` run for agileplus | `~/.jcode/scratch/cargo-mutants-agileplus-1165-XJ9cga.tmp` = **1.1 GB**, multiple `rustc` + `ld` |
| `ld` (linker) | 70.8 % CPU |
| `du` | 38.4 % CPU |
| `cargo metadata` | 37.7 % CPU |
| `knip.js --reporter json` | 36.1 % CPU |

These were **left running**. They belong to other sessions and were not killed.

## A real leak worth naming: 87 zombies

```
ps -eo ppid,stat | awk '$2 ~ /^Z/'  ->  72 under PID 40259
```

`PID 40259` is `~/.jcode/builds/stable/jcode serve`, up **2 days 21 hours**, and it is
not reaping its children. Two 9-day-old `jcode --resume` sessions hold 5 and 4 zombies.

Zombies do not burn CPU, so this is **not** the cause of the load. It is a separate
defect: a long-lived `jcode serve` accumulating unreaped children. Restarting that
daemon would clear 72 of them, but it is also the daemon serving live sessions, so a
restart would kill running agents. **Not done**, and not recommended without the
operator saying so.

## Verdict

- Is anything growing without bound? **NO** for logs and disk (77 Gi free, up from 53 Gi).
- Is `jcode serve` leaking process entries? **YES** — 72 zombies at 2d21h. Name it, do not silently reap it.
- Was the gateway slow because of a config change? **NO** — it was starved by a 1.1 GB
  mutation-test build and unrelated `du`/`cargo`/`node` work at load 327.
- Any measurement taken on this host while load is in the hundreds is **unreliable** and
  must be re-taken on an idle machine before being believed.

## Consequences for other nodes

`P6` (read-only mode) cannot be settled here right now: its decisive test is a 90-second
ACP `initialize` handshake, and a starved host produces the same timeout as a broken
backend. The earlier failure is recorded as UNKNOWN precisely because of this.
