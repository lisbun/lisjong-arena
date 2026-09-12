# Automated Strength Evaluation v0

## Purpose

Automated Strength Evaluation v0 composes the existing first-party ABBB
single-round evaluator into one portable, non-interactive run:

```text
machine-readable spec
  -> resolve / preflight
  -> deterministic lock
  -> existing ABBB evaluator
  -> immutable strength artifact
  -> strict readback
  -> canonical summary
  -> optional predeclared classification
  -> machine-readable result
```

The layers remain distinct:

```text
existing ABBB primitives
  low-level execution, rotation, measurement, artifact, aggregation

Automated Strength Evaluation v0
  one portable locked orchestration run

future Strength Loop
  candidate generation, scheduling, Champion–Challenger
  NOT IMPLEMENTED
```

This runner is not a generic experiment framework, seed allocator, retry/resume
system, or automatic promotion mechanism.

## Invocation

Prepare both durable parent directories before execution, place the spec outside
an uncommitted Arena worktree, and run:

```text
python -m lisjong_arena.strength_evaluation run spec.json
```

Exit status `0` means that both the strength artifact and strict machine-readable
result were completed and read back successfully. Invalid spec, preflight,
execution, artifact, provenance, or result failures return non-zero and emit a
small machine-readable failure object to stderr. A failure is never converted to
negative strength evidence.

On success, canonical JSON is written to stdout and a one-line human summary is
written to stderr, so automation can consume stdout without scraping presentation
text.

The runner never creates a missing output parent. Before game 1 it verifies both
locked final outputs:

- target does not exist;
- parent exists and is a directory;
- parent is observably writable; and
- the two outputs do not resolve to the same destination.

Preflight neither creates nor truncates a final target. Exclusive-create artifact
I/O remains the final write-once authority if the filesystem changes after
preflight.

## Spec v1

All fields are explicit. `identity` is `null` for a curated catalog alias and is
required for a `lisjong.package.module:attribute` reference.

```json
{
  "artifact_output": "/prepared/durable/run-001/strength.json",
  "baseline": {
    "identity": null,
    "reference": "mechanism-riichi-defense"
  },
  "candidate": {
    "identity": "candidate-checkpoint-abc123",
    "reference": "lisjong.policies.candidate:CandidatePolicy"
  },
  "classification_rule": {
    "inconclusive_label": "CANDIDATE INCONCLUSIVE",
    "negative_label": "CANDIDATE NEGATIVE",
    "positive_label": "CANDIDATE SIGNAL",
    "rule_type": "normal-approx-95-interval-threshold-v1",
    "threshold": 0.0
  },
  "execution_options": {
    "max_steps": 10000,
    "workers": 1
  },
  "expected_provenance_constraints": {
    "lisjong_arena_revision": "0123456789abcdef0123456789abcdef01234567",
    "lisjong_engine_revision": "89abcdef0123456789abcdef0123456789abcdef",
    "lisjong_revision": "fedcba9876543210fedcba9876543210fedcba98",
    "python_version": "3.14.6",
    "riichienv_version": "0.4.8"
  },
  "ordered_seeds": [700, 701],
  "protocol": "abbb-single-round-v1",
  "result_output": "/prepared/durable/run-001/result.json",
  "spec_version": 1
}
```

`classification_rule` may be `null`. Such a valid run completes as
`MEASURED / UNCLASSIFIED`. Interval classification requires at least two seed
blocks because the existing canonical interval is undefined for one block.

The runner executes exactly `ordered_seeds`. It does not allocate fresh seeds,
extend a population after seeing a result, replace failed seeds, or distinguish a
formal population on the consumer's behalf.

`protocol` is fixed to the existing `abbb-single-round-v1` contract. Its game
mode remains the existing `4p-red-single` invariant and is not another spec
option.

## Resolution, provenance, and lock

Before execution, the existing Policy resolver binds both references to concrete
`PolicySpec` identities. Mutable unknown aliases such as `latest`, `best`, or
`current champion` are not catalog entries and fail resolution. Explicit imports
use the existing first-party-only resolver and require a caller-supplied concrete
identity.

The lock binds:

- canonical spec semantics;
- resolved candidate and baseline reference/identity;
- protocol and ordered seeds;
- `max_steps` and worker count;
- optional classification rule;
- optional required provenance constraints;
- exact collected source, dependency, environment, and runtime provenance; and
- the exact clean Arena HEAD used as execution target.

The complete Arena worktree must be clean at lock time and again immediately
before game 1. Live provenance and HEAD must still equal the locked values. For a
reviewed scientific run, the consumer should predeclare the reviewed merged-main
SHA as `expected_provenance_constraints.lisjong_arena_revision`. The generic
runner does not hard-code a purpose-specific Issue, PR, branch name, or remote
reference.

Semantic spec and lock identities intentionally exclude `artifact_output` and
`result_output`. Moving the same logical run between prepared local or remote
retention locations therefore does not change its scientific identity. The
in-memory locked plan still retains those exact physical destinations and checks
them at both preflight points.

## Execution and evidence

`workers == 1` calls the existing `run_single_round_evaluation()` API. Larger
values call the existing parallel API. Both preserve existing ordered-seed,
rotation, fresh-Policy-instance, failure, and canonical raw-result semantics. The
orchestrator does not implement its own game runner, seat assignment, score
aggregation, or strength metric.

A successful flow is strictly ordered:

```text
existing evaluator
  -> save_single_round_artifact (exclusive create)
  -> load_single_round_artifact (strict readback)
  -> validate exact lock binding
  -> use artifact canonical summary
  -> apply optional locked rule
  -> write result (exclusive create)
  -> strict result readback and artifact rebinding
```

The immutable `SingleRoundStrengthArtifact` remains the measurement source of
truth. The orchestration result stores its SHA-256 digest and logical reference,
not a machine-local path as scientific identity. Result readback requires the
artifact path explicitly, verifies the digest, strictly loads the artifact, and
re-derives the summary and classification.

## Classification

The one supported optional rule uses the existing normal-approximation 95%
seed-block interval and a consumer-supplied threshold and labels:

```text
lower > threshold  -> POSITIVE kind / consumer positive label
upper < threshold  -> NEGATIVE kind / consumer negative label
otherwise          -> INCONCLUSIVE kind / consumer inconclusive label
```

Equality at either threshold boundary is inconclusive. No candidate-only
secondary Mahjong metric can alter the classification.

```text
NEGATIVE
!= INCONCLUSIVE
!= INVALID
!= EXECUTION FAILED
```

If the interval needed by a declared rule is unavailable, classification is
invalid and the runner does not write a completed result.

## Result v1

The result contains the canonical semantic spec and complete logical lock so
their identities can be independently re-derived. It also records resolved
Policy identities, exact population, execution status/options/target,
provenance, game count, strength artifact schema/protocol/digest, canonical
summary, classification rule and rule identity, and its own result identity.

Representative shape:

```json
{
  "result_version": 1,
  "spec_identity": "<sha256>",
  "lock_identity": "<sha256>",
  "result_identity": "<sha256>",
  "spec": {"...": "canonical path-free spec semantics"},
  "lock": {"...": "resolved path-free lock and exact provenance"},
  "resolved_candidate": {
    "identity": "candidate-checkpoint-abc123",
    "reference": "lisjong.policies.candidate:CandidatePolicy"
  },
  "resolved_baseline": {
    "identity": "mechanism-riichi-defense",
    "reference": "mechanism-riichi-defense"
  },
  "protocol": "abbb-single-round-v1",
  "ordered_seeds": [700, 701],
  "execution_options": {"max_steps": 10000, "workers": 1},
  "execution_status": "COMPLETED",
  "execution_target": {
    "revision": "0123456789abcdef0123456789abcdef01234567",
    "target_type": "clean-arena-head-v1"
  },
  "game_count": 8,
  "strength_artifact": {
    "evaluation_protocol": "abbb-single-round-v1",
    "reference": "strength-artifact",
    "schema_version": 1,
    "sha256": "<sha256>"
  },
  "canonical_summary": {"...": "existing canonical ABBB summary"},
  "classification_rule": {"...": "predeclared rule or null"},
  "classification": {
    "kind": "POSITIVE",
    "label": "CANDIDATE SIGNAL",
    "rule_identity": "<sha256>"
  },
  "provenance": {"...": "existing SingleRoundExecutionProvenance"}
}
```

## Failure and scope boundary

Any Policy resolution, provenance, execution-target, destination, evaluator,
partial-result, artifact write/readback, digest, summary, classification, result
write, or result readback failure prevents a completed result. v0 has no retry,
resume, partial continuation, output repair, or result-driven rescue path.

This implementation does not add remote execution, cloud storage, GitHub Actions
jobs, candidate delivery, Champion state, scheduling, training, HPO, self-play,
HandBelief evaluation unification, Mortal/RiichiLab unification, or a generic
experiment/artifact framework.
