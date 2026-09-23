# AWS diagnostic sweep for #367 (L0.3 lisjong-engine backend throughput)

Issue: lisbun/lisjong-arena#367 (parent lisbun/lisjong-project#79, local run #366).

This is a **#367-specific** operator wrapper that reruns the #366 diagnostic population
on one On-Demand EC2 instance to measure cloud throughput. It is not a generic AWS
framework and not a scientific producer.

## Not scientific

- Diagnostic seeds `910000..910399` (not allocated in the Seed Registry). These seeds
  can never be used for TRAIN / SELECT / strength evaluation
- It creates no outcome source, builds no targets, and trains no model. The driver
  imports nothing from `seed_registry`, `build_outcome_targets`, or the #362 producer
- A PASS establishes AWS execution feasibility / throughput only

## Frozen workload

```text
lisjong        aed9c840bc120471e557fc0c8444965c0b81a9c3
lisjong-engine 96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b   (installed over the older Arena pin, as in #366)
lisjong-arena  668469910bf2e74de583c2b1ac00a77483e8b356   (clean detached checkout)

game_ordinal 0..399, seed 910000 + ordinal, focal seat = engine Seat(ordinal % 4)
focal        FocalExplorationPolicy(game_seed=seed, focal_seat)
others       fresh ConstantResidualRuntime().create_policy() per seat
execution    lisjong_arena.lisjong_engine.hanchan.run_policy_hanchan(..., rules=RuleSet.default())
```

`scripts/aws/sweep_l03_engine_367.py` sits outside the frozen checkout and is pinned by
SHA-256. `verify-environment` checks the Arena checkout HEAD and cleanliness, the
installed `direct_url.json` revisions of lisjong and lisjong-engine, and that
`lisjong_arena` is imported from the checkout.

## Steps

```powershell
# no-billing preflight: Pricing API / quota / offering / dry-run / cost plan
.\scripts\aws\run-l03-engine-367.ps1 -Action Preflight `
  -ToolingRevision <pushed commit of this wrapper> -AwsProfile lisbun-admin

# billable: bucket -> instance -> fail-safe arm -> SSM submit -> Collect
.\scripts\aws\run-l03-engine-367.ps1 -Action Launch `
  -ToolingRevision <same commit> -AwsProfile lisbun-admin

# progress / reattach (does not resubmit)
.\scripts\aws\status-run.ps1 -RunId <run-id> -AwsProfile lisbun-admin
.\scripts\aws\run-l03-engine-367.ps1 -Action Collect -RunId <run-id> -AwsProfile lisbun-admin
```

Defaults: `ap-northeast-1`, `c7i.4xlarge`, 16 workers, On-Demand (no Spot), fail-safe
7200 s from launch, budget USD 5.

## Evidence

- One JSONL row per game (`rows.jsonl`), appended in completion order with
  flush + fsync as each game finishes: ordinal, seed, focal seat, PASS / FAIL, started /
  completed timestamps, duration, and on PASS the round count, end reason, focal
  decision count, and focal multi-survivor (>= 2 survivors) decision count. On FAIL
  it records the exception class, message, and traceback. A game failure is a row, and
  the sweep continues. A worker-process crash is recorded as FAIL rows
- `summary.json`: attempted / PASS / FAIL, failures grouped by exception, focal seat
  counts, focal decision totals, workload wall-clock, hanchan/hour, p50 / p90 game
  duration, and peak / time-weighted mean concurrency from worker timestamps. The result
  is exactly one of `AWS ENGINE BACKEND SWEEP PASS` / `AWS ENGINE BACKEND SWEEP HAS FAILURES`
  / `STOP / INVALID`. Any deviation from the 400-game population is `STOP / INVALID`
- While the sweep runs, the bootstrap uploads rows and progress to the transfer bucket
  every minute. On any bootstrap failure, the exit trap uploads rows and the log and
  prints the log tail to SSM. The root disk is deleted when the instance terminates

`Collect` terminates the instance and downloads every object. On success it checks the
rows / summary SHA-256 against the remote completion record, then re-summarizes the rows
locally with the driver (stdlib only), which must give the same result. It deletes the
benchmark-only bucket (unless `-KeepBucket`) and runs a residual sweep over instances,
volumes, snapshots, ENIs, EIPs, and the bucket. `collection.json` records EC2 launch /
termination and sweep start / end timestamps, instance type, vCPU, nproc, worker count,
and the realized compute + public IPv4 + root EBS cost plus the S3 / transfer bound.

## Cost bound

Preflight prices the instance and gp3 with the current AWS Pricing API. The worst-case
exposure is the full fail-safe window x (instance + public IPv4 + root gp3) plus a fixed
S3 / transfer bound. It must be USD 5 or less including the safety margin. Preflight also
requires the window to be at least 1.5x the predicted billable maximum; the prediction is
based on #366's ~316 hanchan/hour at 4 workers, ~45.5 s/hanchan/worker. The boot user-data
timer and an SSM timer are armed on the same launch-clock deadline before the workload is
submitted.
