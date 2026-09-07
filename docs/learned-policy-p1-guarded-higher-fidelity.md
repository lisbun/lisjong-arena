# Guarded P1 higher-fidelity single-round screen (Issue #175)

Issue #173 established a same-candidate causal diagnostic:

```text
U = exact #162 P1 Offline Q candidate
G = the same candidate + keep-shanten selection guard
```

Only selection-time guard semantics differed, and the locked fresh-development
comparison produced `SHANTEN GUARD ROLLOUT SIGNAL`. That evidence supports the
guard mechanism against the exact unguarded family member. It does not establish
strength against the current meaningful Policy baseline.

Issue #175 therefore changes only evaluation fidelity:

```text
candidate G = exact #173 guarded candidate
baseline C  = exact Stage 6 yakuhai-call Policy, three fresh instances per game
```

This is a higher-fidelity development single-round screen. A positive result is
not hanchan-strength confirmation and is not a promotion decision.

## Scientific boundary

The candidate is not changed or reconstructed as a new research candidate.

```text
KEEP
  #162 checkpoint schema and exact weights
  source dataset and TRAIN support
  P1 feature and action vocabulary
  hybrid activation and yakuhai-call fallback
  Q argmax and legal-action resolution
  #173 keep-shanten guard and empty-subset behavior

FORBID
  retraining / extra epochs / HPO
  teacher, reward, gamma, feature, support, or architecture changes
  soft shanten penalty / ukeire tie-break / new guard heuristic
  comparator changes after result exposure
  seed extension, replacement, or rescue rerun after result exposure
```

The exact derived identities are:

```text
base candidate
  learned-offlineq-p1-gateb:a779aea609aef0cbb0982f3f3d67b0095c7eab9ee8ef4414af488a295468eb5e

guarded candidate
  learned-offlineq-p1-shanten-guard:b2daad85781aafb53dc7364a89fb458b3e7174f7a86e5a8258688f757b0fdc78
```

`candidate_block()` re-derives the guarded identity from the strict-readback
checkpoint binding. Wrong weights, feature fingerprint, support digest,
vocabulary fingerprint, guard semantics, or base identity fail closed.

No training function is called by the Issue #175 implementation.

## Locked baseline

The comparator is the Stage 6 selection-time baseline:

```text
Arena identity       yakuhai-call
factory              lisjong_arena.policy_catalog.create_yakuhai_call
implementation       YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy
mutable alias        false
```

`baseline_block()` requires the exact curated catalog factory and exact class,
not a `current champion`, `best`, or `latest` alias. The pre-execution lock also
requires the selected lisjong and lisjong-engine revisions. A source revision or
factory/class drift is `BASELINE PLAN REFORMULATE` / `STOP`, not silent adoption
of a new baseline.

Every game/seat gets a fresh instance through the existing
`run_single_round_evaluation()` lifecycle. A completed result requires 100
candidate instances and 300 baseline instances.

## Planned evaluation shape

The default plan is:

```text
ordered seeds       547..571
seed blocks         25
rotations / seed    4
total games         100
mode                4p-red-single
workers             1 (serial API)
formal TEST         false
role                DEVELOPMENT HIGHER-FIDELITY SCREEN
```

Existing `SingleRoundEvaluationPlan` provides the ABBB order:

```text
[G, C, C, C]
[C, G, C, C]
[C, C, G, C]
[C, C, C, G]
```

`547..571` is a planned default, not an unconditional permanent allocation.
Immediately before the lock, review relevant open and closed Issues. If a
collision is found before any result exposure, select one different contiguous
25-seed range and build a new lock. `require_seed_plan()` preserves the 25 x 4
shape. After result exposure there is no replacement or extension.

## Required pre-execution lock

The real run must use reviewed/merged current main. Before running any game:

1. Strict-read the retained #162 checkpoint.
2. Recheck current baseline status and open/closed Issue seed allocations.
3. Build the machine-readable lock on a clean Arena revision with exact VCS
   dependency metadata.
4. Post `render_pre_execution_lock(lock)` to Issue #175.
5. Preserve the returned Issue comment URL.
6. Only then call `run_higher_fidelity_evaluation()` with that URL.

The execution entry point rejects URLs that are not Issue #175 comment URLs.
It also re-reads live source/runtime provenance and requires exact equality with
the posted lock before starting the existing evaluator. This does not prove the
remote comment contents by network lookup; review still confirms that the
rendered lock was posted unchanged. It prevents an ordinary accidental call
without a recorded Issue #175 lock reference.

Example lock generation after merge:

```python
from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    load_p1_serving_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_higher_fidelity import (
    HigherFidelityArtifactLocations,
    build_pre_execution_lock,
    render_pre_execution_lock,
)

root = r"C:\Dev\lisjong-artifacts\offlineq-175-guarded-higher-fidelity"
checkpoint_path = r"C:\Dev\lisjong-artifacts\offlineq-162-p1-gate-b\candidate"
checkpoint = load_p1_serving_checkpoint(checkpoint_path)
locations = HigherFidelityArtifactLocations(
    candidate_checkpoint=checkpoint_path,
    strength_artifact=rf"{root}\strength.json",
    result=rf"{root}\result.json",
    classified_result=rf"{root}\classified.json",
)
lock = build_pre_execution_lock(
    checkpoint,
    locations=locations,
    external_freshness_confirmed=True,
    additional_allocated_seeds=(),  # populate if the live Issue review finds any
)
print(render_pre_execution_lock(lock))
```

The lock contains:

- Arena, lisjong, and lisjong-engine revisions
- Python, PyTorch, and RiichiEnv versions
- #162 checkpoint/base identity, weights, dataset, P1 feature, support, and
  vocabulary fingerprints
- #173 guard semantics, binding schema, and derived guarded identity
- baseline identity, factory, implementation, and source revisions
- ordered seeds, game mode, rotations, games, workers, and `formal_test=false`
- primary metric and exhaustive classification vocabulary
- artifact locations/retention keys and the no-rescue boundary

The canonical JSON is embedded in the rendered Markdown. `lock_identity` is the
SHA-256 of all semantic lock content except the identity field itself.

## Execution and artifacts

After the lock comment has been posted, pass its URL explicitly:

```python
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_higher_fidelity import (
    record_classification,
    run_higher_fidelity_evaluation,
    save_result,
)

measurement = run_higher_fidelity_evaluation(
    checkpoint,
    lock,
    pre_execution_comment_url=(
        "https://github.com/lisbun/lisjong-arena/issues/175#issuecomment-..."
    ),
)

classified = record_classification(
    measurement.document,
    measurement.derived_outcome,
    checkpoint=checkpoint,
)
save_result(locations.classified_result, classified)
```

The implementation thinly reuses:

```text
SingleRoundEvaluationPlan
run_single_round_evaluation()
save_single_round_artifact()
load_single_round_artifact()
aggregate_candidate_metrics()
summarize_single_round_strength()
```

It does not add a runner, rotation implementation, score aggregation framework,
generic Gate framework, or dependency on Issue #156.

The strength artifact and result documents are write-once. Before a
classification is recorded, the checkpoint and strength artifact are strict-read
again, their digests/identities are checked, and the canonical summary is
re-derived from the 100 raw games. Stdout is not the measurement source of truth.

Generated weights and result artifacts remain outside Git.

## Primary classification

The primary unit is one seed block (four rotations), not individual candidate or
baseline seat-rounds.

```text
lower > 0
  GUARDED CANDIDATE HIGHER-FIDELITY SIGNAL

upper < 0
  GUARDED CANDIDATE HIGHER-FIDELITY NEGATIVE

otherwise
  GUARDED CANDIDATE HIGHER-FIDELITY INCONCLUSIVE
```

The two pre-result states remain distinct:

```text
GUARDED CANDIDATE EVIDENCE BLOCKED
STOP / INVALID
```

They cannot be persisted as strength outcomes. An interval boundary exactly at
zero is inconclusive. Secondary metrics and mechanism diagnostics cannot change
classification.

## Diagnostics

Both candidate and baseline populations retain the existing Mahjong metrics:

```text
round count / mean round score
win count / rate / mean win points
tenpai reached count / rate / mean first tenpai turn
exhaustive draw count / exhaustive-draw tenpai rate
deal-in count / rate / mean deal-in loss
```

The guarded candidate additionally records:

```text
total decisions
learned activation count / rate
scaffold fallback count / rate
support fallback count / rate
keep-shanten guard opportunities
guard-induced action changes
unguarded-would-worsen count
guarded selected worsen count (must be zero when the guard is available)
illegal selections / non-finite Q outputs / resolve failures (all zero)
```

The guard implementation and its hidden-information, red-five, fallback,
non-finite-output, and illegal-action regressions remain owned and tested by the
Issue #173 modules. Issue #175 reuses them without modification.

## Interpretation boundary

A `SIGNAL` would mean only that the exact #173 guarded P1 candidate showed a clear
positive fresh-development single-round score signal against the exact locked
`yakuhai-call` baseline x3.

It would not mean:

```text
hanchan strength improved
the baseline should be replaced
production adoption is warranted
formal generalization is established
the Q objective is solved
the hard guard is the final architecture
```

Every valid or invalid outcome returns to parent `lisbun/lisjong-project #45`.
This Issue does not automatically file or execute a hanchan follow-up.
