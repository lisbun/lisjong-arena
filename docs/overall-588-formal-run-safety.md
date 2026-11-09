# Overall v2 formal-run safety gates (#588)

Status: **safety engineering only**. This document and the supervisor do not
authorize new formal seeds, a paid launch, the 400-hanchan event or any Champion
decision. Those need a separate explicit user approval after every gate below
passes. The scientific protocol `arena-overall-champion-aabb-half-v1` (mean-rank
primary, four AABB rotations, 100 seed blocks) is unchanged; #587 kyoku metrics
stay diagnostic.

## Problem

`python -m lisjong_arena.overall_champion_aabb run` writes `comparison.json`, the
kyoku files and `overall-result.json` before the strict verification finishes. If
the run is killed (OOM, hang, disk, timeout) or a later check fails, partial files
remain next to the lock. The formal event therefore runs only under the #588
supervisor, which owns the event directory and decides whether anything is
published.

## Supervisor

`scripts/overall-588/supervisor.py` (standard library only; Linux, cgroup v2,
systemd user manager):

```text
supervisor.py prepare --event-dir NEW_DIR --plan PLAN.json   # write-once plan, empty staging/
supervisor.py run     --event-dir DIR                         # one shot
supervisor.py check   --event-dir DIR                         # consumer gate
```

- **Stages**: `lock` -> `run` -> `verify`. Each stage is a fresh process in its
  own transient scope `lo588-EVENT-STAGE.scope` with `MemoryMax` (plan),
  `MemorySwapMax=0`, `OOMPolicy=kill`, `RuntimeMaxSec` = stage wall + 60 s.
  `verify` is always a separate process from `run`.
- **Independent guard**: the supervisor process polls the scope's own cgroup
  (`memory.current`, `memory.peak`, `memory.events`, `memory.max`,
  `memory.swap.max`, `memory.swap.current`, `cgroup.procs`), free space of the
  event filesystem and the size of `staging/`. A completion barrier keeps the
  scope alive until a final readback after the command exits.
- **Fail closed**: any unreadable / malformed cgroup file, a missing scope, or
  `memory.max` / `memory.swap.max` different from the plan (the hard limit is
  not actually in force) stops the event. Host RSS of the scope's PIDs is a
  recorded diagnostic only; it never decides a STOP.
- **Whole-tree kill**: a breach stops the scope (SIGTERM to every process in the
  cgroup, SIGKILL after 3 s, then the process group). Spawn workers are in the
  same cgroup. Processes that outlive a finished command are killed and make the
  event INVALID (`cleanup-leftover`).
- **Write-once staging**: the lock's four destinations are inside `staging/`.
  Only after `run`, the output-set check, the fresh `verify` and a result-identity
  cross-check pass is `staging/` renamed to `published/` with `SHA256SUMS`
  (files read-only). Any other outcome renames `staging/` to `quarantine/`.
- **Receipt**: write-once `receipt.json` with status, trigger, per-stage exit
  code, elapsed time, cgroup peak / current / swap / OOM events, PIDs, free disk,
  staging size, scope state, cleanup, quarantine, plan / lock / supervisor hashes
  and host facts. The supervisor and the CLI (`--no-report`) never print a
  classification; on PASS only the result identity is printed.
- **One shot, no rescue**: an existing receipt or lock refuses `run`. There is no
  restart, retry, extra seed, worker change or partial adoption.

| Status | Meaning | Triggers |
| --- | --- | --- |
| PASS | published and valid (`check` must also pass) | none |
| STOP | a guard or budget ended the event | `wall`, `total-wall`, `memory-soft`, `memory-oom`, `swap`, `disk-low`, `disk-output`, `disk-unreadable`, `monitor-unreadable`, `monitor-isolation`, `child-exit` |
| INVALID | the event ran but its outputs cannot be trusted | `output-set`, `lock-mismatch`, `verify-failed`, `result-mismatch`, `publish-error`, `cleanup-leftover`, `cleanup-error`, `quarantine-error`, `supervisor-error` |

A non-null `quarantine_error` or `cleanup_error` is always INVALID. No receipt
(for example the host lost power) also means not valid. For a FORMAL plan the
seed population is consumed as soon as the lock stage starts
(`population_consumed`), and follows the existing ledger rules
(COMMITTED / RETIRED); the supervisor does not allocate seeds.

### Plan rules

`plan.json` fixes label (`DEVELOPMENT` / `FORMAL`), entry, workers, lock
arguments (seeds, participants, pins), budgets, seed-ledger reference and
approval reference. A FORMAL plan must use the real CLI entry, exactly 8
workers, no fault injection, `memory_soft_peak_bytes < memory_max_bytes`, stage
walls that fit the total wall, a declared `max_cost_usd`, a seed-ledger
reference and an approval reference. The supervisor supplies `--out`,
`--workers` and the four destinations itself; the plan cannot set them.

### Development entry

`scripts/overall-588/dev_entry.py` runs the real Overall CLI with the seed-block
count reduced to 1..8 in its own process, and skips only the "contained in main"
step of the lock (clean worktree and the exact head are still required). It lets
the whole supervised path run on already-used development seeds (472005 /
472006 = 8 games). Its outputs describe a reduced protocol and are never formal;
a FORMAL plan cannot use it.

## Runtime selection: WSL local vs Tokyo AWS

Measured on WSL2 Ubuntu 24.04 (kernel 6.18.33.2-microsoft-standard-WSL2, 8 CPUs,
16 GiB RAM), development event `dev2/normal` at `e9926b7`:

| Item | Value |
| --- | --- |
| lock stage | 5.0 s, cgroup peak 0.63 GB |
| run stage, 8 games, 8 workers | 51.9 s (first game after ~30 s of start-up), cgroup peak 3.18 GB, 11 PIDs, swap 0 |
| verify stage | 2.0 s |
| host RSS sum (diagnostic) | 3.84 GB (counts shared pages more than once) |

Estimate for 400 games: the #583 smoke gave ~40 s per 8 games with 8 workers, so
about 35-40 min plus start-up and a few minutes of verify. AWS `c7i.4xlarge`
(16 vCPU, 32 GiB) would cost about USD 0.91/h (reference price, not a live quote)
with 1.5 h smoke + 6 h formal TTL ≈ USD 6.8 of exposure; WSL costs nothing.

**Recommendation: WSL local**, chosen on cost and measured fit, not on any
strength result. Proposed FORMAL budgets for the WSL event:

| Budget | Value | Reason |
| --- | --- | --- |
| `total_wall_seconds` | 10800 (3 h) | ~4x the estimate |
| stage walls | lock 600, run 7200, verify 1200 | fits the total |
| `memory_soft_peak_bytes` | 6 GiB (6442450944) | #582 value; measured peak 3.18 GB |
| `memory_max_bytes` | 8 GiB (8589934592), swap 0 | #582 value; fits 16 GiB host |
| `min_free_disk_bytes` | 20 GiB | outputs are ~15 MB for 400 games |
| `max_staging_bytes` | 1 GiB | far above the expected size |
| `poll_seconds` | 1 | |
| `max_cost_usd` | 0 | local; nothing billed |

WSL limitations to accept before approval:

- Elapsed time is the max of monotonic and realtime, so a host sleep counts as
  elapsed time and may end the event as `total-wall`. Disable sleep, Windows
  Update restarts and WSL auto-shutdown for the run.
- If the host, WSL or the supervisor dies, no receipt is written: the event is
  not valid and the formal population is still consumed. Run the supervisor
  detached (for example `setsid nohup ... &`) so closing a terminal does not
  stop it; SIGTERM / SIGHUP to the supervisor ends the event as INVALID.
- CPU float32 scorer results can differ slightly from the #582 AWS CPU (max
  7.63e-6 on fixed inputs, #127). This is a predeclared portability limitation;
  it is not a reason to tune weights or mix runtimes.

If AWS is chosen instead, the same supervisor runs on the instance, with
the #582-style soft 6 GiB / hard 8 GiB / swap 0, instance TTL and the cost cap as
the outer bound; this needs its own approval and budget.

## Pinned execution environment (to fill in the FORMAL plan)

- OS / kernel / Python / torch CPU build, as recorded in the receipt `environment`
- Arena: the merged `main` commit that contains this supervisor; clean worktree
- `lisjong` a341257f199e284a3519e17b458e06ced9f04c67 and `lisjong-engine`
  91af75e3aa11520c3b0543719dc74bb4c517ee06 (the pyproject pins; the lock checks
  consistency), RiichiEnv as installed in the same environment
- 8 workers; M1/R1 manifest and weights SHA-256 from #583 / #127 (the v2 lock
  re-hashes them before and after the run)
- the supervisor SHA-256 (in the receipt)

## Operational proof on development seeds

All on already-used development seeds 472005 / 472006 through `dev_entry.py`;
none of these strengths are formal. Evidence is kept under
`C:\Dev\lisjong-artifacts\issue-588\`.

| Event | Injection / setting | Expected |
| --- | --- | --- |
| normal | none | PASS, `check` PASS |
| crash | SIGKILL one spawn worker | STOP `child-exit` |
| hang | SIGSTOP the runner, run wall 90 s | STOP `wall`, tree killed |
| soft | soft peak 512 MiB | STOP `memory-soft` |
| oom | MemoryMax 1 GiB | STOP `memory-oom` (kernel OOM kill) |
| unread | cgroup read fails | STOP `monitor-unreadable` |
| disklow | free space reads 0 | STOP `disk-low` |
| diskerr | free space unreadable | STOP `disk-unreadable` |
| mutate | one digit of kyoku records changed before verify | INVALID `verify-failed` |
| trunc | kyoku records truncated before verify | INVALID `verify-failed` |
| verifail | verifier given the wrong comparison file | INVALID `verify-failed` |
| cleanup | verifier failure and the quarantine target occupied | INVALID `quarantine-error` |

Result at `e9926b7` (events `dev2/normal`, `dev3/*`, 2026-11-10): all 12 ended
with the expected status and reason; `check` passed only for `normal`. In every
STOP case the run scope (11 PIDs incl. spawn workers) was gone afterwards, the
partial outputs were in `quarantine/`, and no `lo588-*` scope or worker process
remained. The `oom` event ended with scope `Result=oom-kill` (kernel hard limit),
the `hang` event was killed at 90.1 s. The `soft` event first saw 1.40 GB at a
1 s poll, so the soft threshold is detected at poll granularity; the kernel
`MemoryMax` is the hard bound.

## Before the formal event (needs explicit user approval)

1. Merge this work; run one DEVELOPMENT `normal` event at the merged commit.
2. Allocate a new, unused 100-seed population in the seed registry and record
   its allocation in the FORMAL plan (`seed_ledger`).
3. Approve the runtime (WSL recommended), the budgets above and the plan hash.
4. Run once. PASS + `check` PASS is the only valid outcome; anything else is
   reported as STOP / INVALID without rescue.
