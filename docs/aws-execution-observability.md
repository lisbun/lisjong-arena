# AWS execution observability

Issue #329 adds reusable operational observability for future bounded AWS
evaluation runs. It does not alter or reconnect to the historical Issue #326
execution. The whole #326 scientific allocation and protocol, including seeds
`2000..2095` and the no-prior-result-exposure attestation, is closed after result
exposure. A source revision change cannot make that attestation true again.

`start-wait-shape-326.ps1` therefore fails before AWS credentials, discovery,
PLAN, or resource creation, including with `-PreflightOnly`. It remains in the
repository only as an auditable record of the bounded executor integration. A
new study must have a reviewed purpose-specific executor and new allocation; it
may reuse `aws_execution_observability.py` and `status-run.ps1`.

Operational metadata is separate from scientific evidence. In particular,
`operational/progress.json`, local `calibration.json`, and its retained-volume
tag summary are not referenced by the execution lock, raw manifest, F1, F2, or
qualification identities. Progress contains counts and timing only; it cannot
contain scores, ranks, classification, model metrics, or per-hanchan scientific
results.

## PLAN before execution

A future purpose-specific executor must perform read-only AWS discovery, resolve vCPU and memory with
`describe-instance-types`, queries the current EC2 on-demand rate through the AWS
Pricing API, and writes `plan.json` before any `create-volume` or `run-instances`
call. `-HourlyPriceUsd` is an explicit injection intended for controlled tests or
an operator-supplied rate; its provenance is recorded as `explicit-injection`.
There is no built-in EC2 or public IPv4 price.

Scientific runtime and EC2 billable runtime are separate prediction windows.
Without matching scientific calibration, the plan deliberately reports:

```text
confidence = LOW
scientific runtime estimate = unavailable
reason = no matching historical evidence
```

Scientific runtime bounds never produce a predicted EC2 cost. That cost requires
separate EC2 billable runtime bounds, including modeled launch, setup, SSM wait,
and termination-confirmation overhead, plus an explicit calibration basis. Until
matching AWS evidence exists, the plan reports:

```text
scientific runtime estimate = available
EC2 billable runtime estimate = unavailable
predicted EC2 execution cost = unavailable
reason = no billable-overhead calibration
estimated fail-safe cost exposure = available
```

The plan distinguishes predicted EC2 cost, estimated fail-safe cost exposure,
and a finalized AWS invoice (out of scope). T-family surplus credits, public IPv4,
and data transfer are reported as `unknown / not hard-bounded`. A retained EBS
estimate may be supplied explicitly with `-RetainedEbsEstimateUsd`; otherwise it
remains unavailable instead of being fabricated. `workers > vCPU` requires
`-AllowWorkerOversubscription`; it is an operational warning, not a scientific
protocol invariant.

## Progress and calibration

A future purpose-specific executor writes progress atomically at:

```text
<artifact-root>/operational/progress.json
```

It starts at `0 / 96`, updates after each completed seed, and finishes at
`96 / 96`. Parallel completion order is not artifact order: receipts and
observations are always reconstructed in `PILOT_SEEDS` order. ETA remains in
`warming-up` state until two units have completed. Timestamps are timezone-aware,
the total cannot change, and the completed count cannot decrease.

The instance-side scientific process measures only the scientific runtime. It
must not label that interval as billable runtime or use it to calculate realized
EC2 cost. After termination is confirmed, the external executor writes:

```text
<local-run-root>/calibration.json
```

This records `scientific_runtime_seconds` separately from
`ec2_billable_runtime_seconds`. Throughput and scientific runtime prediction
error use the scientific interval. Estimated realized EC2 cost uses the billable
interval from the EC2 `LaunchTime` through confirmed termination, so clone,
environment setup, dependency installation, preflight, and teardown wait are
not omitted. Billable runtime and cost prediction errors are unavailable unless
the corresponding predicted EC2 billable runtime range was supplied; scientific
runtime bounds are never reused for them. The record also contains pricing
provenance. It is operational calibration, not a scientific result or a
finalized AWS invoice.

The finalized operational values are mirrored as tags on the retained artifact
volume. This makes COMPLETE status observable by RunId after the instance and
local state are gone; the tags do not become scientific evidence.

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
progress percentage, estimated fail-safe EC2 cost exposure, ETA headroom
warnings, instance sizing, worker count, and billing visibility. The deadline is
recorded from the epoch used immediately when the instance-side timer is armed,
then tagged; it is not predicted before EC2/EBS creation.

For a completed run, status reads local calibration when supplied with a state
path or reconstructs the operational summary from retained-volume tags. It
shows scientific runtime, EC2 billable runtime, throughput, estimated realized
cost, and runtime/cost prediction errors. `COMPLETE` does not mean the estimated
cost is a finalized AWS invoice.

Root and retained artifact volumes are both tagged for rediscovery. Status shows
volume state, size, encryption, attachments, whether billing continues, and root
deletion expectation. The encrypted 1 GiB artifact volume remains intentionally
retained and is never deleted by status or normal teardown.
