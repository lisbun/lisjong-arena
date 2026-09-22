# AWS Offense Foundation execution (#332)

Issue #331 remains the scientific authority. This operator surface adds only
single-instance hanchan process parallelism and AWS lifecycle controls around
the canonical `lisjong_arena.offense_foundation` generator. Worker count,
instance metadata, AWS run-id, completion timing and cost remain operational
metadata and never enter corpus identity. The #342 player-safe source record is
retained beside the corpus as an independent artifact and likewise never enters
the locked #331 corpus identity.

No live P2 or scientific population is stored on Arena `main`. After the
registry implementation is merged, use the **Arena Seed Registry** workflow to
reserve populations on the dedicated `seed-registry` authority branch. Normal
reserve/commit/retire operations require no PR or merge and do not change the
Arena scientific code revision.

## Final merged-main prerequisite

After #346 is merged, freeze the exact reviewed Arena execution revision
(`X`). Reserve the P2 QUALIFICATION population on the `seed-registry` branch
with `arena_revision = X`; this does not move `main`.

On a clean checkout of X, create a new qualification artifact before any
billable Phase A execution:

```text
python -m lisjong_arena.offense_foundation qualify --output <retained-qualification.json>
```

It must report `OFFENSE TEACHER QUALIFIED` and
`SEMANTIC AUDIT PATH QUALIFIED`. The Phase A launcher requires this file and
materializes the operator-supplied Phase A protocol lock locally before its
billable section; this local qualification is the required pre-billing
prerequisite and its scientific/runtime contract is retained as evidence.

The remote bootstrap regenerates qualification under the installed exact
revision on Amazon Linux. It does not require the full local qualification
identity to match: that identity also binds platform-dependent runtime
representation (exact Python patch version, an imported-source byte digest)
that can legitimately differ between the Windows operator environment and
Amazon Linux even when scientific semantics are identical. Instead, the
launcher derives a local `qualification-contract` (Arena/lisjong/lisjong-engine
revisions, RiichiEnv version, teacher identity, feature/vocabulary
dimension and fingerprint, and P0/P1 PASS) and the bootstrap fails closed
unless the remote qualification's own contract matches it exactly. The
AWS-generated qualification — not the local one — is the qualification
actually embedded in the Phase A protocol lock/corpus, since it is the
qualification of the environment that actually executes the games. Both the
local and remote qualification identities are retained as operational
evidence (tagged on the retained Phase A volume and recorded in the
completion summary).

The locked runtime contract is the current `pyproject.toml`: Python
`>=3.14,<3.15`, lisjong
`15799e5f0fe47f2e2b2c39060de804d99c51492d`, lisjong-engine
`8735e89e1aea000ab59368d0368d476787827741`, and RiichiEnv `0.4.10`.

## Phase A

The operator supplies an explicit #331 P2 request containing exactly 20 ordered
fresh seeds plus the canonical allocation binding emitted from the live
`seed-registry` authority. Run preflight first:

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

Billable execution additionally requires a `GO` from the #340 Phase 1 launch
admission gate, which derives the calibrated scientific runtime range from a
matching dedicated calibration rather than from an operator assertion. Pass
`-CalibrationEvidencePath`, a priced `-ChargesPath` and `-CostBudgetUsd`; see
[AWS operational calibration / launch admission](aws-operational-calibration.md).
No matching calibration exists yet, so billable Phase A is currently No-Go by
construction. Defaults are `c7i.4xlarge`, 16 workers and 20 hanchan.

The output is retained on a separate encrypted 8 GiB gp3 volume
tagged with the Phase A run-id. The volume retains both `p2-corpus` and the
strict-read `p2-source-record`; completion evidence/tagging includes the
source-record identity. P2 support is absent from progress and is
published only after complete generation and strict readback. A complete
`OFFENSE SUPPORT NOT QUALIFIED` artifact is retained as the terminal negative
result, but never receives the Phase B PASS gate; no extension or replacement
is authorized.

## Phase B gate

Phase B uses a new run-id and new encrypted 8 GiB gp3 output volume. Its
launcher refuses billable creation unless the supplied Phase A volume is
detached, encrypted gp3 of exactly 8 GiB, and carries completion tags produced
only after strict readback with final `OFFENSE SUPPORT QUALIFIED`.

Phase A completion also tags the retained volume with the exact Arena
revision and remote qualification identity that generated it. Before
`create-volume` or `run-instances`, the Phase B launcher requires the
selected `-ArenaRevision` to match that tag exactly. If `main` has advanced
after Phase A, pass the retained Phase A revision explicitly; do not regenerate
the scientific code identity merely because repository head moved.

Live seed-registry commits are intentionally independent and do **not** change
that Arena revision. Phase B first reserves TRAIN / SELECT / OFFLINE-EVAL on
the dedicated authority branch, then the launcher fetches that branch read-only
and passes the exact ledger snapshot to the AWS bootstrap. A missing/mismatched
Arena revision or invalid allocation authority fails before billable execution.

The Phase B instance is selected in the Phase A volume's Availability Zone.
The Phase A volume is attached as a separate input device and mounted
read-only; it is never retagged to the Phase B run-id. Before locking or
generating the 140-game scientific population, the bootstrap strict-reads both
the complete Phase A corpus and its player-safe source record again. Phase B
retains a new `scientific-source-record` beside the 140-game scientific corpus.
Defaults are `c7i.8xlarge`, 32 workers and 140
hanchan.

After the Phase B instance terminates, the collector explicitly verifies the
retained Phase A input volume in addition to the Phase B output volume: it
confirms detachment, encryption, size/type, and that its original Phase A
provenance tags (run-id, completion, strict-readback and P2 outcome) are
unchanged. The Phase A volume is never deleted or retagged; verification
results and its continued billing are recorded in the Phase B completion
evidence.

```powershell
.\scripts\aws\start-offense-foundation-332.ps1 `
  -Phase B `
  -RequestPath <scientific-request.json> `
  -PhaseAArtifactVolumeId <vol-phase-a> `
  -AwsProfile <profile> `
  -PreflightOnly
```

The canonical lock rejects P2/TRAIN/SELECT/OFFLINE-EVAL overlap, invalid or
retired allocation bindings, noncontiguous populations, replacement and
extension. Freshness authority comes from the live `seed-registry` ledger. No
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
evidence is recorded. Completion is not accepted unless source-record strict
readback also passes; the retained volume is tagged with its source-record
identity. Local monitor detach does not restart, resubmit,
terminate or otherwise mutate the remote scientific workload; the independent
instance-side fail-safe remains the last-resort compute bound.

After the instance is running and its hard fail-safe is armed, the #340
Phase 2 gate runs one non-scientific SSM probe that mounts the retained volume,
persists the recovery identity under `.lisjong-admission/`, verifies a real
write/read probe and unmounts. A Phase 2 `NO-GO` submits no scientific seed and
falls through to the existing bounded cleanup: the instance is terminated and
the unused output volume is deleted. A retained Phase A input volume is never
deleted or retagged.

Worker oversubscription is rejected unless the launcher's explicit
`-AllowWorkerOversubscription` mechanism is selected and propagated to the
remote bootstrap. The phase maximum of 16 or 32 is always enforced. A worker
failure aborts the whole staged phase, so no successful partial hanchan is
published or adopted.
