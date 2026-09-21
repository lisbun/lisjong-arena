# AWS execution observability

Issue #329 adds operational observability for future bounded AWS evaluation
runs. It does not alter or reconnect to the historical Issue #326 execution at
Arena revision `13317bd85bc2f80a575d487cae9dda1bebd7f5f0`. The launcher rejects that
revision explicitly. Its seeds (`2000..2095`), `t3.small` instance, and one-or-two
worker lock remain unchanged.

Operational metadata is separate from scientific evidence. In particular,
`operational/progress.json` and `operational/calibration.json` are not referenced
by the execution lock, raw manifest, F1, F2, or qualification identities. Progress
contains counts and timing only; it cannot contain scores, ranks, classification,
model metrics, or per-hanchan scientific results.

## PLAN before execution

Run the launcher with `-PreflightOnly` first:

```powershell
./scripts/aws/start-wait-shape-326.ps1 `
  -AwsProfile <short-lived-profile> `
  -PreflightOnly
```

The launcher performs read-only AWS discovery, resolves vCPU and memory with
`describe-instance-types`, queries the current EC2 on-demand rate through the AWS
Pricing API, and writes `plan.json` before any `create-volume` or `run-instances`
call. `-HourlyPriceUsd` is an explicit injection intended for controlled tests or
an operator-supplied rate; its provenance is recorded as `explicit-injection`.
There is no built-in EC2 or public IPv4 price.

Without matching historical calibration, the plan deliberately reports:

```text
confidence = LOW
runtime estimate = unavailable
reason = no matching historical evidence
```

A billable run additionally requires both runtime bounds and their explicit
basis. This prevents a fail-safe horizon from being presented as a runtime
prediction:

```powershell
./scripts/aws/start-wait-shape-326.ps1 `
  -AwsProfile <short-lived-profile> `
  -ArenaRevision <reviewed-newer-full-sha> `
  -PredictedRuntimeMinHours <lower> `
  -PredictedRuntimeMaxHours <upper> `
  -RuntimeEstimateBasis '<matching calibration identity>'
```

The plan distinguishes predicted EC2 cost, estimated fail-safe cost exposure,
and a finalized AWS invoice (out of scope). T-family surplus credits, public IPv4,
and data transfer are reported as `unknown / not hard-bounded`. A retained EBS
estimate may be supplied explicitly with `-RetainedEbsEstimateUsd`; otherwise it
remains unavailable instead of being fabricated. `workers > vCPU` requires
`-AllowWorkerOversubscription`; it is an operational warning, not a scientific
protocol invariant.

## Progress and calibration

The future pilot writes progress atomically at:

```text
<artifact-root>/operational/progress.json
```

It starts at `0 / 96`, updates after each completed seed, and finishes at
`96 / 96`. Parallel completion order is not artifact order: receipts and
observations are always reconstructed in `PILOT_SEEDS` order. ETA remains in
`warming-up` state until two units have completed. Timestamps are timezone-aware,
the total cannot change, and the completed count cannot decrease.

After successful verification, the bootstrap writes:

```text
<artifact-root>/operational/calibration.json
```

This records the predicted range, actual runtime and throughput, pricing
provenance, estimated realized EC2 cost, and range-relative prediction errors.
It is operational calibration, not a scientific result or an AWS invoice.

## Status and reattachment

Use the durable RunId; local `state.json` is only an optional cache:

```powershell
./scripts/aws/status-run.ps1 `
  -RunId <run-id> `
  -AwsProfile <short-lived-profile> `
  -Region ap-northeast-1
```

The status command rediscovers EC2 and EBS resources from the
`lisjong-run-id` tag, reads the existing scientific SSM command state, and may
submit one read-only SSM `cat` probe for `operational/progress.json`. It never
submits the scientific workload. It reports EC2/SSM state, progress, elapsed
time, throughput, ETA, estimated finish, fail-safe deadline and remaining time,
instance sizing, worker count, and billing visibility.

Root and retained artifact volumes are both tagged for rediscovery. Status shows
volume state, size, encryption, attachments, whether billing continues, and root
deletion expectation. The encrypted 1 GiB artifact volume remains intentionally
retained and is never deleted by status or normal teardown.
