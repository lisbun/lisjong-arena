# AWS Offense Foundation execution (#332)

Issue #331 remains the scientific authority. This operator surface adds only
single-instance hanchan process parallelism and AWS lifecycle controls around
the canonical `lisjong_arena.offense_foundation` generator. Worker count,
instance metadata, AWS run-id, completion timing and cost remain operational
metadata and never enter corpus identity.

No P2 or scientific population is allocated by this repository change. Do not
run the examples until the implementation PR is merged and the required fresh
seed/evidence scan has been completed.

## Final merged-main prerequisite

On clean final merged `main`, create a new qualification artifact before any
billable Phase A execution:

```text
python -m lisjong_arena.offense_foundation qualify --output <retained-qualification.json>
```

It must report `OFFENSE TEACHER QUALIFIED` and
`SEMANTIC AUDIT PATH QUALIFIED`. The Phase A launcher requires this file and
materializes the operator-supplied Phase A protocol lock locally before its
billable section. The remote bootstrap regenerates qualification under the
installed exact revision and refuses execution unless its identity is exactly
the pre-billing identity.

The locked runtime contract is the current `pyproject.toml`: Python
`>=3.14,<3.15`, lisjong
`15799e5f0fe47f2e2b2c39060de804d99c51492d`, lisjong-engine
`8735e89e1aea000ab59368d0368d476787827741`, and RiichiEnv `0.4.10`.

## Phase A

The future operator supplies an explicit #331 P2 request containing exactly 20
ordered fresh seeds and current freshness evidence. Run preflight first:

```powershell
.\scripts\aws\start-offense-foundation-332.ps1 `
  -Phase A `
  -RequestPath <p2-request.json> `
  -QualificationPath <retained-qualification.json> `
  -AwsProfile <profile> `
  -PreflightOnly
```

Preflight performs AWS discovery, Phase/request validation, exact dependency
contract checks, canonical protocol-lock materialization, compute discovery,
pricing lookup and PLAN creation. It does not call `create-volume`,
`run-instances`, or scientific `send-command`.

Billable execution additionally requires an operator-supplied calibrated
scientific runtime range and basis. Defaults are `c7i.4xlarge`, 16 workers and
20 hanchan. The output is retained on a separate encrypted 8 GiB gp3 volume
tagged with the Phase A run-id. P2 support is absent from progress and is
published only after complete generation and strict readback. A complete
`OFFENSE SUPPORT NOT QUALIFIED` artifact is retained as the terminal negative
result, but never receives the Phase B PASS gate; no extension or replacement
is authorized.

## Phase B gate

Phase B uses a new run-id and new encrypted 8 GiB gp3 output volume. Its
launcher refuses billable creation unless the supplied Phase A volume is
detached, encrypted gp3 of exactly 8 GiB, and carries completion tags produced
only after strict readback with final `OFFENSE SUPPORT QUALIFIED`.

The Phase B instance is selected in the Phase A volume's Availability Zone.
The Phase A volume is attached as a separate input device and mounted
read-only; it is never retagged to the Phase B run-id. Before locking or
generating the 140-game scientific population, the bootstrap strict-reads the
complete Phase A corpus again. Defaults are `c7i.8xlarge`, 32 workers and 140
hanchan.

```powershell
.\scripts\aws\start-offense-foundation-332.ps1 `
  -Phase B `
  -RequestPath <scientific-request.json> `
  -PhaseAArtifactVolumeId <vol-phase-a> `
  -AwsProfile <profile> `
  -PreflightOnly
```

The canonical lock rejects P2/TRAIN/SELECT/OFFLINE-EVAL overlap, prior known
seed use, noncontiguous populations, replacement and extension. No
multi-instance sharding or AWS-specific scientific format exists.

## Progress, detach and reattachment

The generator writes only the shared operational progress schema: completed
and total hanchan, elapsed time, rolling throughput, ETA status/seconds,
estimated finish and workers. `status-run.ps1` adds AWS execution state,
instance/vCPU, fail-safe remaining, percentage and billing visibility:

```powershell
.\scripts\aws\status-run.ps1 -RunId <run-id> -AwsProfile <profile>
```

For a submitted or monitor-detached run, reattach to the existing SSM command:

```powershell
.\scripts\aws\start-offense-foundation-332.ps1 `
  -ReattachRunId <run-id> `
  -AwsProfile <profile>
```

Reattachment never submits a workload. A nonterminal command is reported
without termination or teardown. A completed command is verified, temporary
EC2 is terminated, the 8 GiB output volume is checked detached and retained,
and runtime/billable-runtime/cost calibration plus known residual-resource
evidence is recorded. Local monitor detach does not restart, resubmit,
terminate or otherwise mutate the remote scientific workload; the independent
instance-side fail-safe remains the last-resort compute bound.

Worker oversubscription is rejected unless the launcher's explicit
`-AllowWorkerOversubscription` mechanism is selected and propagated to the
remote bootstrap. The phase maximum of 16 or 32 is always enforced. A worker
failure aborts the whole staged phase, so no successful partial hanchan is
published or adopted.
