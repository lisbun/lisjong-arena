# AWS operational calibration and launch admission (#340)

Issue #340 adds two things on top of the #329 observability contract:

1. a dedicated **operational calibration evidence** document produced by a
   bounded, calibration-only run; and
2. a machine-readable **launch admission** record that must reach `GO` before
   any billable production resource is created (Phase 1), and again before the
   first scientific unit is submitted (Phase 2).

The implementation is `lisjong_arena.aws_operational_calibration`. It calls no
AWS API, executes no workload, and introduces no new cloud framework: it
consumes #329 pricing/runtime planning, #339 durable per-seed receipts and the
#346 seed-ledger binding shape, and it is driven from the existing #332
launcher.

## Boundaries

```text
calibration evidence
  operational timing / resource metadata only
  never qualification, TRAIN, SELECT or OFFLINE-EVAL evidence
  calibration seeds are excluded from every scientific split, forever

admission GO
  authorizes this one launch under this one plan
  does not resume #326, and does not authorize a new scientific study
  a new scientific attempt still needs its own reviewed allocation/execution plan

calibration itself
  a separate bounded billable activity with its own plan and approval
  there is no circular requirement: calibrating needs no prior calibration
```

Every calibration and admission document is validated against a forbidden-key
set (`score`, `rank`, `f1`, `f2`, `support`, `p2_outcome`, …) at **every**
nesting depth, so a scientific result cannot leak into an operational record.

## Calibration evidence

A calibration run measures the exact production shape. The evidence document
(`arena-aws-calibration-evidence-v1`) binds:

```text
identity        calibration_identity (SHA-256 over every other field)
                calibration_run_id, calibrated_at
allocation      seed_allocation (Arena binding shape)
                seed_allocation_population, seed_ledger_revision
code            arena_revision, lisjong_revision, lisjong_engine_revision
                riichienv_version
workload        workload_identity, teacher_identity, game_mode
execution       instance_type, instance_family, vcpu
                worker_count_requested, workers_active_observed
                tasks[] (seed, started_at, completed_at, duration_seconds)
                task_count, task_durations_seconds
                peak_observed_concurrency, mean_observed_concurrency
                batch_scientific_wall_clock_seconds
                ec2_billable_runtime_seconds
                setup_overhead_seconds, teardown_overhead_seconds
derived         p50_seconds_per_unit, p90_seconds_per_unit
                throughput_units_per_hour, percentile_method
instrumentation durable_evidence_level, instrumentation_identity
                instrumentation_path
gaps            unavailable_fields, limitations
burstable       burstable_sustained_basis (required for t-family)
```

`calibration_identity` is recomputed on every read; a document whose derived
values disagree with its own observations fails closed.

### Percentiles are descriptive

`p50` / `p90` are **nearest-rank order statistics** over the observed durations:
`index = ceil(p / 100 * n) - 1` on the ascending sample. The record carries
`percentile_method` verbatim, and every admission record repeats the limitation:

```text
small-sample percentiles are descriptive, not guaranteed bounds
```

The sample size travels with the value (`task_count`, `sample_task_count`), so
a p90 from 20 observations is never presented as a statistical guarantee.

### Concurrency is observed, not assumed

`peak_observed_concurrency` is computed from the task intervals: the maximum
number of simultaneously open tasks. A task that ends exactly when another
starts is not counted as concurrent, so the value is conservative.

`mean_observed_concurrency` is `sum(durations) / batch wall clock`.

### Where the task intervals come from

Preferred: the #339 durable per-seed receipts.
`tasks_from_durable_receipts` runs `verify_seed_checkpoint_set` first, so an
incomplete calibration batch fails closed instead of producing a shorter,
faster-looking sample. A calibration path that has no per-seed receipts may
supply the intervals directly; `durable_evidence_level` records which one it
was, because the durable write path is part of the runtime being predicted.

```text
DURABLE_EVIDENCE_LEVELS = (
    "none",
    "atomic-operational-progress",     # #329 progress.json
    "per-seed-durable-receipt",        # #339 receipt + artifact
)
```

## Matching and freshness

A calibration is admissible for a production launch only when **all** of these
hold:

```text
identity        arena_revision, lisjong_revision, lisjong_engine_revision,
                riichienv_version, workload_identity, teacher_identity,
                game_mode, instance_type, vcpu, instrumentation_identity
                all match the target exactly
workers         worker_count_requested == target worker_count, exactly
concurrency     peak_observed_concurrency >= the workers it requested
tasks           task_count >= max(minimum_task_count,
                                  ceil(workers * minimum_tasks_per_worker))
durable level   calibration level >= required level
freshness       0 <= now - calibrated_at <= freshness_max_age_seconds
completeness    ec2_billable_runtime_seconds, setup_overhead_seconds and
                teardown_overhead_seconds are all measured
burstable       a t-family instance carries an explicit sustained/credit basis
isolation       the calibration allocation is a dedicated
                "operational-calibration" allocation, and neither its
                allocation identity nor its membership identity is a
                production one
```

`seed_ledger_revision` is recorded for audit but is deliberately **not**
required to equal the current ledger revision: an unrelated reservation or a
`RESERVED -> COMMITTED` transition must not invalidate a matching calibration.
This matches the binding semantics in [seed-registry.md](seed-registry.md).

### Worker counts are never extrapolated

There is no interpolation and no division by worker count. The admission record
reports `supported_worker_counts`: the worker counts for which admissible
evidence actually exists. A target outside that set makes the runtime
prediction unavailable, which is a No-Go.

In particular, a 4–8 hanchan batch at 2 workers can never support a 16- or
32-worker production run: it fails `worker-count-exact`, and it is reported as
a supported point at 2 workers only.

An explicit multi-point scaling calibration is therefore expressed as several
evidence documents, one per measured worker count. The supported range is the
measured set, never a curve fitted between them.

## Runtime prediction

```text
predicted_lower = batch wall clock * target_units / calibration task_count
waves           = ceil(target_units / workers)
predicted_upper = max(predicted_lower, waves * p90)
headroom        = predicted_upper * headroom_factor      (default 1.5)
```

The lower bound rescales the observed batch wall clock by the unit ratio **at
the same worker count**. The upper bound adds the scheduling tail: one p90 per
wave. Neither divides a single-unit duration by the worker count.

## Deadlines and clock origins

Three separate limits are checked, each with its own clock origin:

```text
runtime-headroom  headroom <= execution_budget_seconds
                  clock: scientific workload start

normal-deadline   setup + headroom + post_processing <= normal_deadline_seconds
                  clock: instance-side fail-safe arm epoch

hard-fail-safe    normal_deadline_seconds + teardown <= hard_fail_safe_seconds
                  clock: instance-side fail-safe arm epoch
```

`post_processing_seconds` covers saving every unit, final aggregation and
strict readback; `teardown_seconds` covers the confirmed teardown after the
normal deadline. The fail-safe clock starts when the instance-side timer is
armed, which is before the workload starts — that offset is `setup_seconds`,
not zero.

## Cost prediction

```text
billable_lower = setup + predicted_lower  + post_processing + teardown
billable_upper = setup + headroom         + post_processing + teardown
ec2 cost       = billable / 3600 * instance_hourly_rate_usd
total          = ec2 cost + known charges (+ bounded charges on the upper bound)
```

Every non-EC2 charge is declared explicitly:

```text
{ "label": ..., "usd": <known>, "max_usd": null,  "material": true,  "reason": null }
{ "label": ..., "usd": null,    "max_usd": <cap>, "material": true,  "reason": null }
{ "label": ..., "usd": null,    "max_usd": null,  "material": false, "reason": "why" }
```

A charge that is **material and unbounded** makes the cost prediction
unavailable — it is never treated as zero and never replaced by a hard maximum.
A charge declared immaterial must record why. A missing hourly rate is likewise
unavailable, not free. Cost prediction unavailable ⇒ `cost-prediction` and
`cost-budget` both fail ⇒ No-Go.

Predicted cost is an estimate. `is_finalized_aws_invoice` is always `false`.

### Burstable instances

For `t2` / `t3` / `t3a` / `t4g`, a short burst observation is not extrapolated
to sustained multi-hour performance. The evidence must carry a non-empty
`burstable_sustained_basis` describing the CPU-credit condition and the
sustained-performance basis (including surplus charging); without it the
calibration does not match.

## Admission gates

### Phase 1 — before any billable resource

```text
protocol-lock
seed-registry-allocation          ledger validated, allocations active
matching-calibration              fresh, matching, concurrency exercised
runtime-prediction
cost-prediction
cost-budget
runtime-headroom
normal-deadline
hard-fail-safe
durable-evidence-support
artifact-destination-retention
reattach-capability
```

Any failing gate makes the decision `NO-GO` with a reason per gate, sets
`billable_resource_creation_authorized = false`, and `scientific_submission_
authorized` is `false` in Phase 1 regardless.

### Phase 2 — after provisioning, before the first scientific seed

```text
actual-environment-identity       run id, phase 1 identity, instance type,
                                  vCPU, workers and Arena revision all match
hard-fail-safe-armed              armed on this instance, deadline - arm epoch
                                  equals the admitted hard fail-safe, and the
                                  deadline is still in the future
retained-destination-writable     a real write probe on this instance
recovery-identity-persisted       recovery identity written on this instance
remaining-budget-sufficient       elapsed since arm + headroom + post + teardown
                                  still fits the hard fail-safe
```

None of these can pass for a nonexistent instance: every one requires an
`i-...` instance id, and the destination/recovery evidence must name that same
instance. On `NO-GO` the caller must submit no scientific seed and proceed to
bounded cleanup; the record says so in its limitations.

## Using it from the #332 launcher

`start-offense-foundation-332.ps1` is the first concrete consumer. The earlier
operator-supplied `-PredictedScientificRuntime*` /
`-ScientificRuntimeEstimateBasis` / `-PredictedBillableRuntime*` parameters are
**removed**: billable admission is no longer an operator assertion.

```powershell
.\scripts\aws\start-offense-foundation-332.ps1 `
  -Phase A `
  -RequestPath <p2-request.json> `
  -QualificationPath <retained-qualification.json> `
  -CalibrationEvidencePath <calibration.json> `
  -ChargesPath <charges.json> `
  -CostBudgetUsd 25 `
  -AwsProfile <profile> `
  -PreflightOnly
```

Defaults, all fixed in the pre-launch plan:

```text
-HeadroomFactor          1.5
-SetupOverheadMinutes    20
-PostProcessingMinutes   20
-TeardownMinutes         15
-NormalDeadlineHours     FailSafeHours - teardown
-ExecutionBudgetHours    normal deadline - setup - post-processing
```

`-CostBudgetUsd` has no default: an omitted budget is evaluated as `0 USD`, so
the `cost-budget` gate fails and the launch is No-Go until a budget is declared.

Without `-ChargesPath` the launcher emits the fail-closed default charge list:
the retained EBS volume (priced only if `-RetainedEbsEstimateUsd` is supplied),
public IPv4 hours and data transfer, all material and unbounded. That is a
deliberate No-Go until the operator prices or explicitly declares them.

The run directory then contains:

```text
admission-requirement.json    the exact machine-readable requirement
admission-phase-1.json        pre-billing decision, gates, reasons
operational-plan.json         the #329 PLAN, CALIBRATED only on a Phase 1 GO
plan.json / preflight.json    #332 evidence, now carrying the admission identity
recovery-identity.json        local copy of what is persisted on the volume
phase-2-observation.json      what was actually observed on the instance
admission-phase-2.json        pre-submission decision, gates, reasons
```

Phase 2 runs one non-scientific SSM probe that mounts the retained volume,
writes `/mnt/lisjong-332-output/.lisjong-admission/recovery-identity.json`,
verifies a write/read probe, and unmounts. That directory is outside the
`issue-332/` artifact root, so the bootstrap's artifact-root freshness check is
unaffected.

### #332 Phase A durable-evidence limitation

The #331 corpus generator publishes atomic operational progress but no #339
per-seed receipt, so an interrupted Phase A retains no completed hanchan. The
launcher declares `durable_evidence_level = "atomic-operational-progress"` and
records that limitation in the `durable-evidence-support` gate detail, rather
than claiming a durability the execution path does not have.

## Historical #326 observation

`historical_incomplete_observation()` returns the #326 run as planning history
with its gaps intact:

```text
run id          20260921T061727Z-184f3b4c
teacher         targeted-honor-release-terminal-progression x4
mode            4p-red-half
instance        t3.small, 2 workers
target          96 hanchan (seeds 2000..2095)
hard fail-safe  8h
status          INCOMPLETE AT HARD FAIL-SAFE
completed_units null
per-unit times  null
p50 / p90       null
throughput      null
```

It uses a different schema version from calibration evidence, so it can never
be selected as a matching calibration. It also carries
`forbidden_derivations`, which name explicitly the conversions that must not be
made: "96 hanchan completed within 8 hours", "12 hanchan per hour", any
per-seed p50/p90 for that run, and any scientific FAIL for #322.

## CLI

```text
python -m lisjong_arena.aws_operational_calibration evidence \
    --input <calibration-input.json> --output <calibration.json>

python -m lisjong_arena.aws_operational_calibration validate-evidence \
    --calibration <calibration.json>

python -m lisjong_arena.aws_operational_calibration admit-phase-1 \
    --requirement <requirement.json> --calibration <calibration.json> \
    --output <admission-phase-1.json>

python -m lisjong_arena.aws_operational_calibration admit-phase-2 \
    --admission <admission-phase-1.json> --observation <observation.json> \
    --output <admission-phase-2.json>

python -m lisjong_arena.aws_operational_calibration historical-observation
```

Exit codes: `0` = GO, `3` = NO-GO, `2` = invalid input. `--calibration` may be
repeated to supply several measured worker points. Output files are never
overwritten.

## Status of live calibration

No live AWS calibration has been performed for this contract. A synthetic
fixture is never a fresh calibration: fixtures exercise the decision logic
only, and the admission gate requires evidence whose identity, worker count,
concurrency and freshness are all real measurements of the production shape.
