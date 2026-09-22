# AWS operational calibration and launch admission (#340)

Issue #340 adds three things on top of the #329 observability contract:

1. a **bounded calibration execution path** that can run the exact production
   shape on a dedicated calibration-only population, under its own approved
   budget, without requiring any pre-existing calibration;
2. a dedicated **operational calibration evidence** document produced by that
   run; and
3. a machine-readable **launch admission** record that must reach `GO` before
   any billable production resource is created (Phase 1), and again before the
   first scientific unit is submitted (Phase 2).

The implementation is `lisjong_arena.aws_operational_calibration`. It calls no
AWS API, executes no workload, and introduces no new cloud framework: it
consumes #329 pricing/runtime planning, #339 durable per-seed receipts for the
calibration path, and the #346 seed-ledger binding shape, and it is driven from
the existing #332 launcher. #351 deliberately does not require those per-seed
receipts from production scientific generation.

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

A calibration run measures the same performance-relevant production shape, plus the\ncalibration-only #339 timing receipt. The evidence document
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

### The calibration allocation is resolved, not just shaped

`calibration-allocation-authority` runs the #346 `require_allocation_binding`
contract against the canonical `seed-registry` ledger. A well-formed binding of
arbitrary SHA-256 values is never enough. Resolution requires:

```text
the immutable allocation identity is uniquely present in the canonical ledger
the record is still RESERVED or COMMITTED
its seed domain and membership identity match the binding
its membership is exactly the seeds the calibration executed
its population is "operational-calibration" and its protocol is
    "aws-operational-calibration-v1"
it declares no scientific split
its domain equals the production population's domain, so the ledger's
    same-domain non-overlap invariant applies
```

Phase 1 therefore takes `--seed-ledger`, and a missing ledger is a No-Go rather
than a skipped check. `calibration-seed-isolation` additionally intersects the
executed calibration seeds with the production seeds carried in the admission
requirement, so overlap is rejected explicitly and not only by inference.

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

The billable window is built from the **measured** calibration overhead,
combined conservatively with the operator's planned allowance:

```text
effective_setup     = max(planned setup,    measured setup_overhead_seconds)
effective_teardown  = max(planned teardown, measured teardown_overhead_seconds)
measured_residual   = measured ec2_billable_runtime
                      - measured setup - measured wall clock - measured teardown
billable_overhead   = effective_setup + planned post_processing
                      + effective_teardown + max(0, measured_residual)

billable_lower = billable_overhead + predicted_lower
billable_upper = billable_overhead + headroom
ec2 cost       = billable / 3600 * instance_hourly_rate_usd
total          = ec2 cost + known charges (+ bounded charges on the upper bound)
```

Two calibrations with identical per-task timings but different observed
billable runtime, setup or teardown therefore produce different predicted
costs. A planned allowance smaller than the measured basis never makes the
prediction cheaper, and the residual billable time the calibration observed but
did not attribute to setup or teardown -- launch, attach and SSM wait -- is
carried through rather than dropped. The whole decomposition is recorded in
`cost_prediction.overhead_basis`, including
`planned_allowance_below_measured`.

Without a matched calibration there is no measured basis, so the cost
prediction is unavailable and the launch is a No-Go.

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
actual-environment-identity       run id, prior admission identity, instance
                                  type, vCPU, workers and Arena revision match
hard-fail-safe-armed              the launch-time boot timer AND the SSM timer
                                  are both armed on this instance, deadline -
                                  arm epoch equals the admitted hard fail-safe,
                                  and the deadline is still in the future
retained-destination-writable     a real write probe on this instance
recovery-identity-persisted       recovery identity written on this instance
remaining-budget-sufficient       time AND cost, both re-evaluated after setup
```

`remaining-budget-sufficient` now carries two subchecks in one gate:

```text
time  elapsed since fail-safe arm + remaining workload + post_processing
      + effective teardown <= hard fail-safe

cost  realized billable since the EC2 launch epoch + remaining workload
      + post_processing + effective teardown, priced at the admitted rate,
      plus known and bounded charges <= cost_budget_usd
```

Setup that consumed far more billable time than planned therefore fails the
gate on cost even when the time budget still fits. The observation carries
`instance_launch_epoch` for that reason: the billable clock starts at the EC2
launch, not at the fail-safe arm. The three epochs must be in order
(`launch <= arm <= observation`) or the gate fails.

None of these can pass for a nonexistent instance: every one requires an
`i-...` instance id, and the destination/recovery evidence must name that same
instance. On `NO-GO` the caller must submit no scientific seed and proceed to
bounded cleanup; the record says so in its limitations.

## Running the bounded calibration

`scripts/aws/start-operational-calibration-340.ps1` plus
`scripts/aws/bootstrap-operational-calibration-340.sh` execute the calibration
itself. The launcher has no population request, no protocol lock and no corpus
publication path, so it structurally cannot submit a scientific population; its
admission record's `scientific_submission_authorized` is always `false`.

```powershell
.\scripts\aws\start-operational-calibration-340.ps1 `
  -QualificationPath <retained-qualification.json> `
  -AllocationIdentity <calibration allocation identity> `
  -FirstSeed <first calibration seed> -UnitCount 20 `
  -InstanceType c7i.4xlarge -MaxWorkers 16 `
  -FailSafeHours 4 -PreArmAllowanceMinutes 20 -TeardownMinutes 15 `
  -CalibrationCostBudgetUsd <approved budget> `
  -ChargesPath <charges.json> `
  -AwsProfile <profile> `
  -PreflightOnly
```

### The pre-arm interval is bounded from launch

The SSM-armed fail-safe can only exist once SSM is reachable, which is after
`run-instances`, the instance-running wait, the volume attach and the SSM
Online wait. The instance is already billable in that window, and a local
launcher that dies inside it would leave nothing instance-side to stop the
spend.

Both launchers therefore arm a second, independent timer from **boot**, through
launch-time user data in the same `run-instances` request, with
`InstanceInitiatedShutdownBehavior = terminate`:

```text
boot fail-safe window = setup_seconds (pre-arm allowance)
                      + hard_fail_safe_seconds
                      + teardown_seconds
```

That window is a **launch-clock** window, and `systemd-run --on-active` counts
from the moment cloud-init runs the user data, which is some way into the first
boot cycle. Arming `--on-active=<window>` would therefore hold
`EC2 launch + cloud-init delay + window`, which is longer than the window the
admission record prices. The rendered script instead resolves the real launch
time and arms only what is left:

```text
IMDSv2 token
  -> /latest/dynamic/instance-identity/document
  -> pendingTime                       (the EC2 launch time)
  -> DEADLINE  = pendingTime + window
  -> REMAINING = DEADLINE - now
  -> REMAINING <= 0        power off immediately
  -> launch clock unknown  power off immediately (fail closed)
  -> otherwise             systemd-run --on-active="${REMAINING}s"
```

The script is rendered by `render_boot_fail_safe_user_data()` in the admission
core, not assembled inside the launchers, so there is one implementation and
the boundary cases are tested by executing it against a stubbed IMDS. It also
records its deadline to `/run/lisjong-boot-failsafe.env`.

Both timers stay armed. In a healthy run the SSM one fires first, because it is
armed at most `setup_seconds` after boot and runs for `hard_fail_safe_seconds`.
The boot timer is the outer guarantee that exists from the moment the instance
exists.

`setup_seconds` is therefore not decorative for a calibration: it is the
bounded provisioning allowance, it is what the boot fail-safe has to cover, and
the phase-0 worst case prices the **whole boot-clock window** rather than
starting at the later SSM arm epoch. A calibration requirement with
`setup_seconds = 0` is a No-Go, because nothing would bound the pre-arm
interval.

Phase 2 records `boot_fail_safe_armed` from an actual
`systemctl is-active lisjong-boot-failsafe.timer` check on the instance, plus
`boot_fail_safe_deadline_epoch` from that env file, and `hard-fail-safe-armed`
requires **both** timers *and*:

```text
0 < boot_fail_safe_deadline_epoch <= instance_launch_epoch + boot window
boot_fail_safe_deadline_epoch > observed_at_epoch
```

so a timer that was actually armed from cloud-init rather than from EC2 launch
is rejected.

On the instance, the bootstrap regenerates qualification, requires the same
cross-platform qualification-contract match as #332, and runs
`offense_foundation calibrate`. That runner executes the same per-hanchan
generation the production path executes -- the same teacher, the same paired
corpus and player-safe source-record writes -- and then:

```text
publishes  a #339 per-seed durable receipt whose payload is timing only
           #329 atomic operational progress
           a raw calibration observation document
seals      nothing: no manifest, no support aggregation, no P2 outcome
deletes    the scratch game artifacts before teardown
```

After the instance terminates, the launcher derives the measured billable
window from the EC2 clock (`LaunchTime` -> confirmed termination), splits out
setup and teardown around the instance-side workload interval, and calls
`aws_operational_calibration evidence`.

### The calibration admission (phase 0)

```text
calibration-allocation-authority   resolved against canonical ledger authority
calibration-scope-bounded          units == allocated seeds, within the caps
calibration-cost-budget            worst case fits the approved budget
hard-fail-safe                     independent, bounded, teardown reserved
durable-evidence-support           per-seed receipts plus atomic progress
artifact-destination-retention
teardown-confirmation
```

There is deliberately **no matching-calibration gate**: requiring calibration
evidence in order to calibrate would be circular. The monetary bound is
therefore the worst case -- the whole boot-clock window at the current rate,
plus every declared charge -- rather than a prediction. The record's
`runtime_prediction` is explicitly unavailable with that reason, which is what
makes the absence of circularity auditable.

Phase 0 authorizes **creating the bounded billable resources only**:

```text
billable_resource_creation_authorized   decision == "GO"
workload_submission_authorized          false
scientific_submission_authorized        false
```

This matches production, where Phase 1 never authorizes submission. The same
Phase 2 gate then runs before the calibration workload is submitted, and it is
the first record that can set `workload_submission_authorized`. A caller that
reads only the phase-0 record therefore cannot bypass Phase 2. Because the
prior record is a calibration admission, Phase 2's remaining-budget check uses
the worst-case remaining fail-safe window, and its
`scientific_submission_authorized` stays `false` however it turns out.

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

`-PreflightOnly` creates no billable resource, but it is still the same
mechanical gate: a NO-GO decision exits non-zero after the PLAN and the
admission record have been saved, so a machine caller never reads a No-Go as a
successful preflight.

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

### Production and calibration use different durability minima

#351 separates the durability requirement by purpose.

Production #332 requires the capability the canonical generator already
provides:

```text
python -m lisjong_arena.offense_foundation durable-evidence

durable_evidence_level             atomic-operational-progress
required_durable_evidence_level    atomic-operational-progress
per_seed_durable_receipt_supported false
status                             PASS
follow_up                          null
```

Calibration remains stronger:

```text
calibration durable evidence       per-seed-durable-receipt
calibration required level         per-seed-durable-receipt
```

That split is intentional. A production interruption does not become
recoverable scientific data merely because per-seed receipts exist: partial
output cannot be adopted, resumed, selectively rerun, or used to classify P2.
An authorized retry reruns the same locked phase in full. Calibration, by
contrast, needs a complete per-task timing set to prevent an incomplete fast
sample from becoming runtime evidence, so #339 receipts remain mandatory
there.

Matching calibration accepts evidence whose durable level is **at least** the
production minimum. A per-seed calibration therefore validly supports an
atomic-progress production target when every performance-relevant identity
still matches exactly. The shared
`GENERATION_INSTRUMENTATION_IDENTITY` continues to bind the paired
corpus/source-record write path; a real change to that path still makes old
calibration stale.

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

python -m lisjong_arena.aws_operational_calibration admit-calibration \
    --requirement <calibration-requirement.json> \
    --seed-ledger <canonical-seed-ledger.json> \
    --output <admission-calibration.json>

python -m lisjong_arena.aws_operational_calibration admit-phase-1 \
    --requirement <requirement.json> --calibration <calibration.json> \
    --seed-ledger <canonical-seed-ledger.json> \
    --output <admission-phase-1.json>

python -m lisjong_arena.aws_operational_calibration admit-phase-2 \
    --admission <admission-phase-1.json> --observation <observation.json> \
    --output <admission-phase-2.json>

python -m lisjong_arena.aws_operational_calibration historical-observation

python -m lisjong_arena.aws_operational_calibration boot-fail-safe-user-data \
    --window-seconds <launch-clock window> [--base64]
```

Exit codes: `0` = GO, `3` = NO-GO, `2` = invalid input. `--calibration` may be
repeated to supply several measured worker points. Output files are never
overwritten.

## Status of live calibration

No live AWS calibration has been performed for this contract, and no
calibration seeds have been reserved yet. A synthetic fixture is never a
fresh calibration: fixtures exercise the decision logic
only, and the admission gate requires evidence whose identity, worker count,
concurrency and freshness are all real measurements of the production shape,
resolved against canonical seed-registry authority.

The ordering that follows from this contract is:

```text
freeze the final Arena execution revision (including #351)
  -> regenerate / strict-read the applicable P0/P1 qualification
  -> reserve the dedicated calibration population on the seed-registry branch
  -> calibration preflight and explicit cost review
  -> bounded calibration execution with per-seed timing receipts
  -> calibration evidence
  -> reserve / bind the production population
  -> #332 Phase 1 admission (atomic production progress is sufficient)
  -> billable production execution only after GO
```
