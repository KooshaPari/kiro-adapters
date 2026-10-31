# Host Idle Baseline

**Purpose:** decide whether this host is quiet enough for a 90-second handshake
timing test to be meaningful, and record the measured baseline so a later run can
be compared against it.

**Measured:** 2026-10-31T02:43:30Z (local 2026-10-31 19:43, TZ -07:00)
**Host:** macOS, aarch64 (this workstation, not the `desk` remote)
**Mode:** READ-ONLY. Nothing was restarted, killed, or modified by the measurement.
**Repo at measurement:** `KooshaPari/kiro-adapters`, branch `main`,
HEAD `ffa1413a3cdbe221b18faf39da1136bfcaab31d7`
("docs(acceptance): correct a truncation in the P1 deciding-literal quote"),
clean working tree.

## Measurements

| # | Measure | Command | Result | Reading |
|---|---|---|---|---|
| 1 | Load averages (1 / 5 / 15 min) | `uptime` | **151.23 / 156.25 / 197.66**; up 11 days 11:05, 12 users | Saturated. 15-min average is the *highest* of the three, so load was still rising, not draining. |
| 2 | Process state counts | `ps -eo stat` | **767 S** (sleeping), **109 R** (running/runnable), **18 Z** (zombie), 1 `?` ; ~895 total | 109 runnable on a 10-core host is a deep run queue. 18 zombies are reaped-parent artifacts, not load, but they confirm process churn. |
| 3 | Top 5 CPU consumers | `ps -eo pid,%cpu,comm` | `sysmond` **95.4%**; `syspolicyd` **90.2%**; WorkflowKit XPC **52.3%**; `jcode` (stable build) **52.1%**; SkyLight WindowServer **52.0%**; (6th: rustup nightly toolchain **37.7%**) | Two macOS system daemons at ~90%+ are the dominant signal. `sysmond`/`syspolicyd` at that level is a symptom of a host-wide permission/signing/policy storm or an enormous process count, not of ordinary compile work. Parallel build activity is present but secondary. |
| 4 | `cargo-mutants` active? | `ps -eo pid,comm \| awk '$2 ~ /mutants/'` | **ACTIVE** — PID **78773** `/Users/kooshapari/.cargo/bin/cargo-mutants`, plus build child PID 14162 under `~/.jcode/scratch/cargo-mutants-agileplus-1165-iqYW7X.tmp/target/debug/build/agileplus-domain/...` | A real mutation-testing run is in flight against `agileplus`. This is a direct, self-inflicted load source and a decisive disqualifier. |
| 5 | Gateway health, 3 calls 5 s apart | `curl -s -o /dev/null -w 'code=%{http_code} total=%{time_total}' --max-time 30 http://127.0.0.1:5476/api/health` | **try1 code=200 total=0.795171**; **try2 code=200 total=0.364411**; **try3 code=200 total=0.009941** | All three answered HTTP 200 (the gateway is alive and reachable). Latency spread is **80x** (0.795 s -> 0.010 s) on an identical request. That spread is the real finding: it is the signature of a host under contention, and it is exactly why a single 90 s handshake measurement taken now would not be reproducible. |
| 6 | Disk, root volume | `df -h /` | `/dev/disk3s1s1 926Gi size, 24Gi used, 77Gi avail, 25% capacity` | Not a constraint for this test. Note `avail` (77Gi) is far below the earlier-recorded 124Gi, consistent with active build/mutation scratch growth. Only 25% capacity used, so no disk-pressure trigger applies. |

## Threshold used, and why

**Idle-enough threshold: 1-min load under 20 AND all three health samples under 0.5 s.**

Rationale: the earlier failing measurement of this handshake was taken under load
91-144. A threshold of load < 20 is roughly 4-7x below that band, which leaves
genuine headroom on a 10-core host rather than merely "less bad than last time".
The 0.5 s health gate exists because a handshake budget is measured in
sub-second increments: if a bare health check can consume 0.8 s, no 90-second
handshake result can be attributed to the code path under test.

## VERDICT

**NOT quiet enough — do not run the 90-second handshake test now and do not treat
its result as meaningful.** Measured load 151/156/198 against a <20 threshold
(7.5-10x over), a live `cargo-mutants` run (PID 78773) actively consuming the
host, 109 runnable processes, and a health-time spread of 0.795 s -> 0.010 s
(one sample over the 0.5 s gate). Disk is fine. Re-measure and re-run only after
the `cargo-mutants` run finishes and 1-min load is under 20 with three
consecutive health samples under 0.5 s.

### What to re-check before the next attempt

1. `ps -eo pid,comm | awk '$2 ~ /mutants/'` returns nothing.
2. `uptime` 1-min load < 20.
3. Three consecutive `curl ... /api/health` totals all < 0.5 s, with a spread
   under ~3x (a stable host does not swing 80x between identical requests).
4. `sysmond` / `syspolicyd` are no longer among the top CPU consumers.
