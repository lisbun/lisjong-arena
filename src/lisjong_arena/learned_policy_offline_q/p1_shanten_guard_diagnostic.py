"""Shanten-constrained Q serving diagnostic — G vs U bounded rollout (Issue #173).

`lisbun/lisjong-arena #162`のexact P1 Q candidate（`P1 GATE B INCONCLUSIVE`）を
一切変更せず、serving時のaction-selection constraintだけを変えたarmと、
guardなしのexact same candidateをfresh development single-round population上で
paired比較する。

```text
U — UNGUARDED control        exact #162 serving semantics（verbatim）
G — SHANTEN-GUARDED candidate  同じcandidate binding + selection guardだけ追加
```

```text
require_fresh_seed_plan()   execution boundaryでmachine-enforced（caller discipline非依存）
        |
exact #162 P1 serving checkpoint
        |
        +-- U: HybridPolicy（無変更）
        +-- G: ShantenGuardedHybridPolicy（selectionだけ変更）
                |
                +----------- SingleRoundEvaluationPlan ----+
                             |
                  run_single_round_evaluation()   serial / workers = 1
                             |
                  save_single_round_artifact()    immutable
                             |
                  load_single_round_artifact()    strict readback
                             |
                  summarize_single_round_strength()  canonical re-derivation
                             |
                  build_diagnostic_result()
                             |
                  save_diagnostic_result()        write-once + strict readback
                             |
                  validate -> derive_classification()
                             |
                  record_classification() -> save_classified_result()  write-once
```

primary changed axisは1つだけである。

```text
CHANGE   learned Q ordinary-discard action-selection constraint

KEEP     exact #162 model weights / P1 8241 representation / Q values /
         TRAIN support set / hybrid activation semantics / legal action
         semantics / action vocabulary / yakuhai-call fallback / checkpoint
         load semantics / fresh Policy instance semantics /
         evaluation backend / aggregation
```

このmoduleが意図的に**しない**こと:

- 新しいgame runner / seat rotation / score aggregationの実装
- new training / retraining / extra epoch / different initialization
- reward / gamma / target update / support変更
- teacher変更 / FiniteHorizon curriculumへのsubstitution
- 新しいfeature / soft shanten penalty / Q値の補正
- resultを見たあとのseed extension / guard rule変更

## classification order

```text
1. STOP / INVALID
2. SHANTEN GUARD EVIDENCE BLOCKED
3. SHANTEN GUARD INACTIVE           guard-induced action change count == 0
4. SHANTEN GUARD ROLLOUT SIGNAL     normal_approx_95_interval_lower > 0
5. SHANTEN GUARD ROLLOUT NEGATIVE   normal_approx_95_interval_upper < 0
6. SHANTEN GUARD ROLLOUT INCONCLUSIVE   intervalが0を跨ぐ
```

`INACTIVE`はintervalより優先する。guard-induced action changeが1件も無い場合、
score directionをpositive / negativeへ解釈しない。

secondary Mahjong diagnostics、serving diagnosticsはこのladderを一切通らない。
"""

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import mkstemp

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundEvaluationPlan,
)
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    SingleRoundStrengthArtifact,
    collect_execution_provenance,
    execution_provenance_to_dict,
    load_single_round_artifact,
    parse_execution_provenance,
    save_single_round_artifact,
    summary_to_dict,
)
from lisjong_arena.single_round_evaluation import (
    aggregate_candidate_metrics,
    aggregate_seat_round_stats_metrics,
    run_single_round_evaluation,
    summarize_single_round_strength,
)

from .errors import OfflineQError
from .p1_candidate import (
    LOCKED_SELECTED_EPOCH,
    MATERIALIZATION_SOURCES,
    SERVING_CHECKPOINT_SCHEMA_VERSION,
    LoadedP1ServingCheckpoint,
    candidate_binding_document,
    load_p1_serving_checkpoint,
    require_candidate_identity,
    verify_locked_candidate_contract,
)
from .p1_serving import create_p1_hybrid_runtime
from .p1_shanten_guard import (
    GUARD_RULE,
    GuardDiagnostics,
    collect_guard_diagnostics,
    guard_binding_document,
    guard_candidate_identity,
    guarded_policy_factory,
    require_guard_candidate_identity,
)
from .protocol import PROTOCOL_ID
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

DIAGNOSTIC_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-shanten-guard-diagnostic-v1"
)
DIAGNOSTIC_ID = "arena-learned-policy-offlineq-p1-shanten-guard-diagnostic-173"
SOURCE_ISSUE = "lisbun/lisjong-arena#173"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#152",
    "lisbun/lisjong-arena#158",
    "lisbun/lisjong-arena#162",
    "lisbun/lisjong-arena#165",
)
PARENT_ISSUE = "lisbun/lisjong-project#45"

RETENTION_BACKEND = "operator-local-durable"
CANDIDATE_RETENTION_KEY = "offlineq-173-shanten-guard/candidate"
ARTIFACT_RETENTION_KEY = "offlineq-173-shanten-guard/strength-artifact"

PRIMARY_CHANGED_AXIS = "learned Q ordinary-discard action-selection constraint"

LOCKED_UNCHANGED_AXES = (
    "exact #162 model weights",
    "P1 8241 representation",
    "Q values",
    "TRAIN support set",
    "hybrid activation semantics",
    "legal action semantics",
    "action vocabulary",
    "yakuhai-call fallback",
    "checkpoint load semantics",
    "fresh Policy instance semantics",
    "evaluation backend / aggregation",
)

# --- Locked diagnostic population ------------------------------------------
#
# Issue #173がresult exposure前の第一候補としてlockしたfresh development
# population。`522..546`は`#165`のrollout population (`497..521`) 直後の
# contiguous rangeである。

ORDERED_SEEDS = tuple(range(522, 547))
SEED_BLOCK_COUNT = 25
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAME_COUNT = SEED_BLOCK_COUNT * ROTATIONS_PER_SEED
GAME_MODE = SINGLE_ROUND_GAME_MODE
MAX_WORKERS = 1
"""Learned runtimeのprocess serializationを本Issueへ持ち込まないためserial固定。"""

ROLE = "DEVELOPMENT SHANTEN-GUARD DIAGNOSTIC"
FORMAL_TEST = False

if (
    len(ORDERED_SEEDS) != SEED_BLOCK_COUNT
    or ORDERED_SEEDS
    != tuple(range(ORDERED_SEEDS[0], ORDERED_SEEDS[0] + SEED_BLOCK_COUNT))
    or GAME_COUNT != 100
    or ROTATIONS_PER_SEED != 4
    or GAME_MODE != "4p-red-single"
):
    raise RuntimeError("the locked Issue #173 seed plan shape drifted")


def declared_allocated_seeds() -> frozenset[int]:
    """repositoryが宣言済みのseed populationを実constantから集める。

    generic seed registryを新設せず、既存protocol moduleのlocked constantsを
    そのまま読む。`#173`自身のpopulationは含めない（自分とのcollisionを報告
    しないため）。
    """
    from lisjong_arena.stage3_scale_learning_curve.protocol import (
        declared_occupied_seeds,
    )

    from .fh_curriculum import DATASET_ORDERED_SEEDS as FH_DATASET_SEEDS
    from .fh_curriculum import ROLLOUT_ORDERED_SEEDS as FH_ROLLOUT_SEEDS
    from .p1_gate_b import GATE_B_ORDERED_SEEDS

    return (
        frozenset(declared_occupied_seeds())
        | frozenset(GATE_B_ORDERED_SEEDS)
        | frozenset(FH_DATASET_SEEDS)
        | frozenset(FH_ROLLOUT_SEEDS)
    )


SEED_PLAN_REFORMULATE = "SEED PLAN REFORMULATE"
"""result exposure前にcollisionが判明した場合の唯一の許容rescue path。"""


def check_seed_freshness(*, result_exposed: bool = False) -> dict[str, object]:
    """locked Issue #173 seed planのfreshnessをpreflightする。

    result exposure前のcollisionだけが`SEED PLAN REFORMULATE`であり、result
    exposure後のcollisionはrescueできず`STOP / INVALID`である。
    """
    if type(result_exposed) is not bool:
        raise TypeError("result_exposed must be a bool")
    allocated = declared_allocated_seeds()
    collisions = sorted(allocated.intersection(ORDERED_SEEDS))
    fresh = not collisions
    if fresh:
        status = None
    elif result_exposed:
        status = ShantenGuardOutcome.STOP_INVALID.value
    else:
        status = SEED_PLAN_REFORMULATE
    return {
        "ordered_seeds": list(ORDERED_SEEDS),
        "collisions": collisions,
        "fresh": fresh,
        "result_exposed": result_exposed,
        "status": status,
    }


def require_fresh_seed_plan() -> dict[str, object]:
    """freshでない場合に`SEED PLAN REFORMULATE`としてfail closedする。"""
    report = check_seed_freshness()
    if not report["fresh"]:
        raise _error(
            f"{SEED_PLAN_REFORMULATE}: the locked Issue #173 seed plan collides "
            f"with already allocated populations (collisions={report['collisions']!r}); "
            "the plan is re-locked to a fresh contiguous range of the same shape "
            "before any generation, never silently replaced and never after "
            "result exposure"
        )
    return report


def require_diagnostic_seed(seed: int) -> int:
    """diagnosticで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in ORDERED_SEEDS:
        raise _error(f"seed {seed} is not part of the locked Issue #173 population")
    return seed


LIMITATIONS = (
    "This is a bounded selection-time diagnostic, not a new Q algorithm, not a "
    "production Policy, and not a final strength fix.",
    "The shanten guard preserves shanten only; it is not ukeire optimization, "
    "hand value optimization, or full Mahjong utility optimization.",
    "U is the exact #162 serving candidate verbatim (guard OFF); G is the exact "
    "same candidate binding with the selection guard added -- the only axis "
    "that may differ between the two arms is the ordinary-discard "
    "action-selection constraint.",
    "The guard never falls back to the yakuhai-call scaffold when the "
    "keep-shanten subset is empty; it returns the original legal argmax Q "
    "instead, so a guard outcome is never conflated with a "
    "learned-to-heuristic fallback change.",
    "Single-round 4p-red-single games are not hanchan; no hanchan strength "
    "improvement is claimed.",
    "The seeds are a development-only population; this is not a formal "
    "holdout and not generalization evidence.",
    "Secondary Mahjong and serving diagnostics are reported for interpretation "
    "only and never enter the primary classification.",
    "When guard-induced action change count is 0, the primary interval is not "
    "interpreted as positive or negative; the outcome is SHANTEN GUARD "
    "INACTIVE regardless of the score direction.",
)

INTERPRETATION_BOUNDARY = {
    "positive_claim_limit": (
        "under the exact #162 P1 Q values, selection-time restriction to the "
        "shanten-preserving legal discard subset showed a clear positive "
        "single-round score direction against the exact same unguarded "
        "candidate on fresh development seeds"
    ),
    "forbidden_claims": [
        "the Q objective is solved",
        "P1 is proven generally useful",
        "this is a strong Policy claim",
        "the candidate beats the Development Champion",
        "hanchan strength improved",
        "production Policy adoption is warranted",
    ],
    "negative_claim_limit": (
        "negative or inactive evidence applies to this exact bounded selection "
        "guard on this exact candidate, not to shanten-preserving structure as "
        "a general research direction"
    ),
}

NEXT_ACTION_BOUNDARY = (
    "Whatever the outcome, this Issue does not extend seeds, change the guard "
    "rule, add a soft shanten penalty, add a ukeire tie-break, redesign the "
    "reward, change the teacher, add a feature, or auto-escalate to Gate C, "
    "hanchan evaluation, or production adoption; the next action is "
    "re-selected by the parent roadmap Issue."
)

CLASSIFICATION_RULE = {
    "primary_metric": "seed-block G-vs-U score delta",
    "interval": "normal-approx 95% interval over seed blocks",
    "inactive": "guard-induced action change count == 0",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "the interval crosses zero",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
}


class ShantenGuardOutcome(Enum):
    """Issue #173がresult exposure前に固定したexhaustive outcome。"""

    ROLLOUT_SIGNAL = "SHANTEN GUARD ROLLOUT SIGNAL"
    ROLLOUT_NEGATIVE = "SHANTEN GUARD ROLLOUT NEGATIVE"
    ROLLOUT_INCONCLUSIVE = "SHANTEN GUARD ROLLOUT INCONCLUSIVE"
    INACTIVE = "SHANTEN GUARD INACTIVE"
    EVIDENCE_BLOCKED = "SHANTEN GUARD EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


_RECORDABLE_OUTCOMES = (
    ShantenGuardOutcome.INACTIVE,
    ShantenGuardOutcome.ROLLOUT_SIGNAL,
    ShantenGuardOutcome.ROLLOUT_NEGATIVE,
    ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE,
)
_OUTCOME_VALUES = frozenset(outcome.value for outcome in ShantenGuardOutcome)


class ShantenGuardDiagnosticError(OfflineQError):
    """diagnosticのplan / execution / artifact / result契約の違反。"""


def _error(message: str) -> ShantenGuardDiagnosticError:
    return ShantenGuardDiagnosticError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Plan -------------------------------------------------------------------


def plan_block() -> dict[str, object]:
    """result documentへ記録するlocked plan block。"""
    return {
        "ordered_seeds": list(ORDERED_SEEDS),
        "seed_block_count": SEED_BLOCK_COUNT,
        "rotation_count": ROTATIONS_PER_SEED,
        "game_count": GAME_COUNT,
        "game_mode": GAME_MODE,
        "max_workers": MAX_WORKERS,
        "role": ROLE,
        "formal_test": FORMAL_TEST,
        "candidate_arm": "G",
        "baseline_arm": "U",
        "seat_rotation": "ABBB",
    }


def build_diagnostic_plan(
    checkpoint: LoadedP1ServingCheckpoint,
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry, PolicyInstanceRegistry]:
    """exact #162 candidateから、GとUのlocked diagnostic planを組み立てる。

    GとUは同じ``HybridRuntime``（同じmodel reference、同じsupport set）を共有
    し、GのPolicy classだけが``ShantenGuardedHybridPolicy``に変わる。
    """
    if not isinstance(checkpoint, LoadedP1ServingCheckpoint):
        raise TypeError("checkpoint must be a LoadedP1ServingCheckpoint")
    verify_locked_candidate_contract()
    for seed in ORDERED_SEEDS:
        require_diagnostic_seed(seed)
    runtime = create_p1_hybrid_runtime(
        checkpoint.model, supported_indices=checkpoint.supported_indices
    )
    guard_registry = PolicyInstanceRegistry(guarded_policy_factory(runtime))
    unguarded_registry = PolicyInstanceRegistry(runtime.create_policy)
    guard_identity = guard_candidate_identity(checkpoint.manifest["candidate_binding"])
    guard_spec = PolicySpec(
        identity=guard_identity, factory=guard_registry.create_policy
    )
    unguarded_spec = PolicySpec(
        identity=checkpoint.candidate_identity, factory=unguarded_registry.create_policy
    )
    plan = SingleRoundEvaluationPlan(
        candidate=guard_spec, baseline=unguarded_spec, seeds=ORDERED_SEEDS
    )
    return plan, guard_registry, unguarded_registry


# --- Artifact binding --------------------------------------------------------


def require_diagnostic_artifact(
    artifact: SingleRoundStrengthArtifact,
    *,
    guarded_identity: str,
    unguarded_identity: str,
) -> SingleRoundStrengthArtifact:
    """readbackしたartifactがdiagnostic protocol条件を満たすことを確認する。"""
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be a SingleRoundStrengthArtifact")
    plan = artifact.plan
    if plan.seeds != ORDERED_SEEDS:
        raise _error("the artifact was not produced on the locked Issue #173 seeds")
    if plan.game_mode != GAME_MODE:
        raise _error("the artifact game mode is not the locked one")
    if plan.rotation_count != ROTATIONS_PER_SEED:
        raise _error("the artifact rotation count is not the locked one")
    if len(artifact.game_results) != GAME_COUNT:
        raise _error(
            f"a diagnostic artifact must contain exactly {GAME_COUNT} games; "
            f"got {len(artifact.game_results)} -- a partial run is never accepted"
        )
    if plan.candidate_identity != guarded_identity:
        raise _error("the artifact candidate is not the served G (guarded) arm")
    if plan.baseline_identity != unguarded_identity:
        raise _error("the artifact baseline is not the served U (unguarded) arm")
    seat_counts: dict[int, int] = {}
    for game_result in artifact.game_results:
        seat = int(game_result.candidate_seat)
        seat_counts[seat] = seat_counts.get(seat, 0) + 1
    if sorted(seat_counts) != [0, 1, 2, 3] or set(seat_counts.values()) != {
        SEED_BLOCK_COUNT
    }:
        raise _error("the G arm did not occupy each seat exactly once per seed block")
    return artifact


def artifact_block(
    artifact: SingleRoundStrengthArtifact, path: Path
) -> dict[str, object]:
    """immutable strength artifactへのidentity / referenceだけを記録する。"""
    return {
        "schema_version": artifact.schema_version,
        "evaluation_protocol": artifact.evaluation_protocol,
        "filename": path.name,
        "sha256": _sha256_file(path),
        "game_count": len(artifact.game_results),
        "retention": {"backend": RETENTION_BACKEND, "key": ARTIFACT_RETENTION_KEY},
    }


# --- Result document ----------------------------------------------------------


def guarded_candidate_block(checkpoint: LoadedP1ServingCheckpoint) -> dict[str, object]:
    """G armのcandidate logical identityとbindingを記録する。"""
    manifest = checkpoint.manifest
    base_binding = manifest["candidate_binding"]
    return {
        "identity": guard_candidate_identity(base_binding),
        "binding": guard_binding_document(base_binding),
        "base_candidate_identity": checkpoint.candidate_identity,
        "canonical_model_weights_digest": checkpoint.canonical_model_weights_digest,
        "checkpoint_schema_version": manifest["checkpoint_schema_version"],
        "materialization_source": manifest["materialization_source"],
        "selected_epoch": manifest["selected_epoch"],
        "source_dataset_identity": manifest["source_dataset_identity"],
        "supported_indices_digest": manifest["supported_indices_digest"],
        "expected_identities": manifest["expected_identities"],
        "real_candidate_materialization": manifest["real_candidate_materialization"],
        "retention": {"backend": RETENTION_BACKEND, "key": CANDIDATE_RETENTION_KEY},
    }


def unguarded_candidate_block(
    checkpoint: LoadedP1ServingCheckpoint,
) -> dict[str, object]:
    """U armのcandidate logical identityとbindingを記録する（exact #162 identity）。"""
    manifest = checkpoint.manifest
    return {
        "identity": checkpoint.candidate_identity,
        "binding": manifest["candidate_binding"],
        "canonical_model_weights_digest": checkpoint.canonical_model_weights_digest,
        "checkpoint_schema_version": manifest["checkpoint_schema_version"],
        "materialization_source": manifest["materialization_source"],
        "selected_epoch": manifest["selected_epoch"],
        "source_dataset_identity": manifest["source_dataset_identity"],
        "supported_indices_digest": manifest["supported_indices_digest"],
        "expected_identities": manifest["expected_identities"],
        "real_candidate_materialization": manifest["real_candidate_materialization"],
        "retention": {"backend": RETENTION_BACKEND, "key": CANDIDATE_RETENTION_KEY},
    }


def serving_diagnostics_block(
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    """serving activation / fallback診断。classificationには使わない。"""
    if not isinstance(diagnostics, ActivationDiagnostics):
        raise TypeError("diagnostics must be an ActivationDiagnostics")
    return {
        **diagnostics.to_document(),
        "illegal_selection_count": 0,
        "non_finite_model_output_count": 0,
        "resolve_failure_count": 0,
        "fail_closed_at_decision_time": True,
    }


def guarded_mahjong_metrics(summary) -> dict[str, object]:
    """G armはcandidate seat（1 per game）分の既存canonical metricsをそのまま写す。"""
    metrics = summary.candidate_metrics.mahjong_metrics
    return {
        "round_count": metrics.round_count,
        "population": "guarded (G) seat (1 per game)",
        "mean_round_score_delta": metrics.mean_round_score_delta,
        "win_count": metrics.win_count,
        "win_rate": metrics.win_rate,
        "mean_win_points": metrics.mean_win_points,
        "deal_in_count": metrics.deal_in_count,
        "deal_in_rate": metrics.deal_in_rate,
        "mean_deal_in_loss": metrics.mean_deal_in_loss,
        "exhaustive_draw_count": metrics.exhaustive_draw_count,
        "exhaustive_draw_tenpai_count": metrics.exhaustive_draw_tenpai_count,
        "exhaustive_draw_tenpai_rate": metrics.exhaustive_draw_tenpai_rate,
        "tenpai_reached_count": metrics.tenpai_reached_count,
        "mean_first_tenpai_turn": metrics.mean_first_tenpai_turn,
    }


def unguarded_mahjong_metrics(game_results) -> dict[str, object]:
    """U armが担当した3 seat分のraw statsから既存canonical formulaで集計する。"""
    stats = [
        game_result.seat_round_stats[seat]
        for game_result in game_results
        for seat in range(4)
        if seat != int(game_result.candidate_seat)
    ]
    metrics = aggregate_seat_round_stats_metrics(stats)
    return {
        "round_count": metrics.round_count,
        "population": "unguarded (U) seats (3 per game)",
        "mean_round_score_delta": metrics.mean_round_score_delta,
        "win_count": metrics.win_count,
        "win_rate": metrics.win_rate,
        "mean_win_points": metrics.mean_win_points,
        "deal_in_count": metrics.deal_in_count,
        "deal_in_rate": metrics.deal_in_rate,
        "mean_deal_in_loss": metrics.mean_deal_in_loss,
        "exhaustive_draw_count": metrics.exhaustive_draw_count,
        "exhaustive_draw_tenpai_count": metrics.exhaustive_draw_tenpai_count,
        "exhaustive_draw_tenpai_rate": metrics.exhaustive_draw_tenpai_rate,
        "tenpai_reached_count": metrics.tenpai_reached_count,
        "mean_first_tenpai_turn": metrics.mean_first_tenpai_turn,
    }


def guard_diagnostics_block(diagnostics: GuardDiagnostics) -> dict[str, object]:
    if not isinstance(diagnostics, GuardDiagnostics):
        raise TypeError("diagnostics must be a GuardDiagnostics")
    return diagnostics.to_document()


def result_identity(document: dict) -> str:
    """classificationとidentity自身を除いたcanonical bytesのdigest。"""
    payload = {**document, "classification": None, "result_identity": None}
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def build_diagnostic_result(
    *,
    checkpoint: LoadedP1ServingCheckpoint,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: Path,
    summary,
    guard_diagnostics: GuardDiagnostics,
    guarded_activation_diagnostics: ActivationDiagnostics,
    unguarded_activation_diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    """versioned diagnostic result documentを組み立てる。

    `classification`は常に`None`で作られる。outcomeはvalidation後に
    `record_classification()`でだけ付与する。
    """
    document = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic_id": DIAGNOSTIC_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "guarded_candidate": guarded_candidate_block(checkpoint),
        "unguarded_candidate": unguarded_candidate_block(checkpoint),
        "plan": plan_block(),
        "strength_artifact": artifact_block(artifact, artifact_path),
        "canonical_summary": summary_to_dict(summary),
        "guard_diagnostics": guard_diagnostics_block(guard_diagnostics),
        "secondary_diagnostics": {
            "guarded": guarded_mahjong_metrics(summary),
            "unguarded": unguarded_mahjong_metrics(artifact.game_results),
        },
        "serving_diagnostics": {
            "guarded": serving_diagnostics_block(guarded_activation_diagnostics),
            "unguarded": serving_diagnostics_block(unguarded_activation_diagnostics),
        },
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(LIMITATIONS),
        "interpretation_boundary": {
            "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
            "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
            "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
        },
        "next_action_boundary": NEXT_ACTION_BOUNDARY,
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "result_identity": None,
        "classification": None,
    }
    return {**document, "result_identity": result_identity(document)}


_RESULT_FIELDS = {
    "schema_version",
    "diagnostic_id",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
    "protocol_id",
    "primary_changed_axis",
    "locked_unchanged_axes",
    "guarded_candidate",
    "unguarded_candidate",
    "plan",
    "strength_artifact",
    "canonical_summary",
    "guard_diagnostics",
    "secondary_diagnostics",
    "serving_diagnostics",
    "classification_rule",
    "limitations",
    "interpretation_boundary",
    "next_action_boundary",
    "provenance",
    "result_identity",
    "classification",
}
_GUARDED_CANDIDATE_FIELDS = {
    "identity",
    "binding",
    "base_candidate_identity",
    "canonical_model_weights_digest",
    "checkpoint_schema_version",
    "materialization_source",
    "selected_epoch",
    "source_dataset_identity",
    "supported_indices_digest",
    "expected_identities",
    "real_candidate_materialization",
    "retention",
}
_UNGUARDED_CANDIDATE_FIELDS = _GUARDED_CANDIDATE_FIELDS - {"base_candidate_identity"}
_ARTIFACT_FIELDS = {
    "schema_version",
    "evaluation_protocol",
    "filename",
    "sha256",
    "game_count",
    "retention",
}
_DIAGNOSTIC_FIELDS = {
    "policy_instance_count",
    "total_decisions",
    "total_activations",
    "activation_rate",
    "total_scaffold_fallbacks",
    "scaffold_fallback_rate",
    "total_support_fallbacks",
    "support_fallback_rate",
    "illegal_selection_count",
    "non_finite_model_output_count",
    "resolve_failure_count",
    "fail_closed_at_decision_time",
}
_MAHJONG_FIELDS = {
    "round_count",
    "population",
    "mean_round_score_delta",
    "win_count",
    "win_rate",
    "mean_win_points",
    "deal_in_count",
    "deal_in_rate",
    "mean_deal_in_loss",
    "exhaustive_draw_count",
    "exhaustive_draw_tenpai_count",
    "exhaustive_draw_tenpai_rate",
    "tenpai_reached_count",
    "mean_first_tenpai_turn",
}
_GUARD_DIAGNOSTIC_FIELDS = {
    "learned_decision_count",
    "keep_shanten_available_count",
    "no_keep_shanten_available_count",
    "keep_shanten_available_rate",
    "unguarded_worsen_count",
    "unguarded_keep_count",
    "action_change_count",
    "action_change_rate",
    "guarded_worsen_among_available_count",
    "mean_baseline_post_discard_shanten",
    "mean_guarded_post_discard_shanten",
    "paired_lower_count",
    "paired_equal_count",
    "paired_higher_count",
}
_SEED_BLOCK_FIELDS = {
    "seed_block_count",
    "mean_seed_block_delta",
    "sample_standard_deviation",
    "standard_error",
    "normal_approx_95_interval_lower",
    "normal_approx_95_interval_upper",
    "positive_seed_block_count",
    "zero_seed_block_count",
    "negative_seed_block_count",
}


def _require_fields(value: object, fields: set[str], context: str) -> dict:
    if type(value) is not dict:
        raise _error(f"{context} must be an object")
    if set(value) != fields:
        missing = sorted(fields - set(value))
        extra = sorted(set(value) - fields)
        raise _error(f"{context} has missing {missing!r} or extra {extra!r} fields")
    return value


def _require_count(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise _error(f"{context} must be a non-negative int")
    return value


def _require_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{context} must be an exact bool")
    return value


def _require_number(value: object, context: str) -> float:
    if type(value) not in (int, float):
        raise _error(f"{context} must be a number")
    return float(value)


def _validate_guarded_candidate(block: object) -> dict:
    candidate = _require_fields(block, _GUARDED_CANDIDATE_FIELDS, "guarded_candidate")
    if candidate["checkpoint_schema_version"] != SERVING_CHECKPOINT_SCHEMA_VERSION:
        raise _error(
            "guarded_candidate checkpoint schema version is not the locked one"
        )
    if candidate["selected_epoch"] != LOCKED_SELECTED_EPOCH:
        raise _error(
            "guarded_candidate is not the locked fixed_final_iteration epoch "
            f"{LOCKED_SELECTED_EPOCH} checkpoint"
        )
    if candidate["materialization_source"] not in MATERIALIZATION_SOURCES:
        raise _error("unknown guarded_candidate materialization source")
    digest = candidate["canonical_model_weights_digest"]
    support_digest = candidate["supported_indices_digest"]
    for name, value in (
        ("canonical_model_weights_digest", digest),
        ("supported_indices_digest", support_digest),
        ("source_dataset_identity", candidate["source_dataset_identity"]),
    ):
        if type(value) is not str or len(value) != 64:
            raise _error(
                f"guarded_candidate.{name} must be a 64 character sha256 digest"
            )
    base_binding = candidate_binding_document(
        canonical_model_weights_digest=digest, support_set_digest=support_digest
    )
    if candidate["binding"] != guard_binding_document(base_binding):
        raise _error(
            "the recorded guarded_candidate binding is not the guard binding "
            "derived from this candidate's weights, feature, vocabulary, "
            "support set, activation semantics and fallback Policy"
        )
    require_guard_candidate_identity(candidate["identity"], base_binding)
    return candidate


def _validate_unguarded_candidate(block: object) -> dict:
    candidate = _require_fields(
        block, _UNGUARDED_CANDIDATE_FIELDS, "unguarded_candidate"
    )
    if candidate["checkpoint_schema_version"] != SERVING_CHECKPOINT_SCHEMA_VERSION:
        raise _error(
            "unguarded_candidate checkpoint schema version is not the locked one"
        )
    if candidate["selected_epoch"] != LOCKED_SELECTED_EPOCH:
        raise _error(
            "unguarded_candidate is not the locked fixed_final_iteration epoch "
            f"{LOCKED_SELECTED_EPOCH} checkpoint"
        )
    if candidate["materialization_source"] not in MATERIALIZATION_SOURCES:
        raise _error("unknown unguarded_candidate materialization source")
    digest = candidate["canonical_model_weights_digest"]
    support_digest = candidate["supported_indices_digest"]
    for name, value in (
        ("canonical_model_weights_digest", digest),
        ("supported_indices_digest", support_digest),
        ("source_dataset_identity", candidate["source_dataset_identity"]),
    ):
        if type(value) is not str or len(value) != 64:
            raise _error(
                f"unguarded_candidate.{name} must be a 64 character sha256 digest"
            )
    binding = candidate_binding_document(
        canonical_model_weights_digest=digest, support_set_digest=support_digest
    )
    if candidate["binding"] != binding:
        raise _error(
            "the recorded unguarded_candidate binding is not the exact #162 "
            "binding this candidate's weights, feature, vocabulary, support "
            "set, activation semantics and fallback Policy derive"
        )
    require_candidate_identity(candidate["identity"], binding)
    return candidate


def _validate_arms_share_base_identity(guarded: dict, unguarded: dict) -> None:
    """GとUがexact同じbase candidate identity / weights / support / fallbackを共有するか。"""
    if guarded["base_candidate_identity"] != unguarded["identity"]:
        raise _error(
            "guarded_candidate does not bind the exact unguarded_candidate identity "
            "as its base; G and U must be the same #162 candidate"
        )
    for name in (
        "canonical_model_weights_digest",
        "source_dataset_identity",
        "supported_indices_digest",
        "selected_epoch",
        "materialization_source",
        "real_candidate_materialization",
    ):
        if guarded[name] != unguarded[name]:
            raise _error(
                f"guarded_candidate.{name} does not match unguarded_candidate.{name}; "
                "the only changed axis must be the selection constraint"
            )
    if guarded["identity"] == unguarded["identity"]:
        raise _error("the guarded and unguarded arms must have distinct identities")


def _validate_artifact(block: object) -> None:
    artifact = _require_fields(block, _ARTIFACT_FIELDS, "strength_artifact")
    if artifact["schema_version"] != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise _error("the strength artifact schema version is not the locked one")
    if artifact["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise _error("the strength artifact evaluation protocol is not ABBB")
    if artifact["game_count"] != GAME_COUNT:
        raise _error("the strength artifact does not record the locked game count")
    sha256 = artifact["sha256"]
    if type(sha256) is not str or len(sha256) != 64:
        raise _error("strength_artifact.sha256 must be a 64 character sha256 digest")
    filename = artifact["filename"]
    if type(filename) is not str or not filename or "/" in filename:
        raise _error("strength_artifact.filename must be a bare file name")
    if artifact["retention"] != {
        "backend": RETENTION_BACKEND,
        "key": ARTIFACT_RETENTION_KEY,
    }:
        raise _error("the strength artifact retention target is not the locked one")


def _validate_summary(block: object) -> dict:
    if type(block) is not dict:
        raise _error("canonical_summary must be an object")
    if set(block) != {
        "candidate_metrics",
        "mean_baseline_score",
        "mean_candidate_game_delta",
        "seed_block_statistics",
    }:
        raise _error("canonical_summary is not the canonical aggregation shape")
    for name in ("mean_baseline_score", "mean_candidate_game_delta"):
        _require_number(block[name], f"canonical_summary.{name}")
    metrics = block["candidate_metrics"]
    if type(metrics) is not dict:
        raise _error("canonical_summary.candidate_metrics must be an object")
    if metrics.get("game_count") != GAME_COUNT:
        raise _error(
            "the canonical summary was not aggregated over the locked game count"
        )
    statistics = _require_fields(
        block["seed_block_statistics"], _SEED_BLOCK_FIELDS, "seed_block_statistics"
    )
    if statistics["seed_block_count"] != SEED_BLOCK_COUNT:
        raise _error(
            "the canonical summary was not aggregated over the locked seed blocks"
        )
    for name in (
        "normal_approx_95_interval_lower",
        "normal_approx_95_interval_upper",
    ):
        if statistics[name] is None:
            raise _error(
                "the primary metric requires a defined normal-approx 95% interval"
            )
        _require_number(statistics[name], f"seed_block_statistics.{name}")
    ordered = [
        _require_count(statistics[f"{name}_seed_block_count"], name)
        for name in ("positive", "zero", "negative")
    ]
    if sum(ordered) != SEED_BLOCK_COUNT:
        raise _error("the seed block sign counts do not partition the seed blocks")
    return statistics


def _validate_diagnostics(block: object, context: str) -> None:
    diagnostics = _require_fields(block, _DIAGNOSTIC_FIELDS, context)
    total = _require_count(diagnostics["total_decisions"], f"{context}.total_decisions")
    if total == 0:
        raise _error(f"{context} records no decision; a valid run has decisions")
    _require_count(
        diagnostics["policy_instance_count"], f"{context}.policy_instance_count"
    )
    parts = [
        _require_count(diagnostics[name], f"{context}.{name}")
        for name in (
            "total_activations",
            "total_scaffold_fallbacks",
            "total_support_fallbacks",
        )
    ]
    if sum(parts) != total:
        raise _error(
            f"{context} activation, scaffold fallback and support fallback counts "
            "do not partition the candidate decisions"
        )
    for name, count in zip(
        ("activation_rate", "scaffold_fallback_rate", "support_fallback_rate"),
        parts,
        strict=True,
    ):
        if diagnostics[name] != count / total:
            raise _error(f"{context}.{name} is not derivable from counts")
    for name in (
        "illegal_selection_count",
        "non_finite_model_output_count",
        "resolve_failure_count",
    ):
        if diagnostics[name] != 0:
            raise _error(
                f"{context}.{name} must be 0; any occurrence fails the run closed "
                "before a result exists"
            )
    if (
        _require_bool(
            diagnostics["fail_closed_at_decision_time"],
            f"{context}.fail_closed_at_decision_time",
        )
        is not True
    ):
        raise _error(f"{context} serving is fail closed at decision time")


def _validate_secondary(block: object) -> None:
    if type(block) is not dict or set(block) != {"guarded", "unguarded"}:
        raise _error("secondary_diagnostics must describe both arms")
    for name, expected_rounds in (
        ("guarded", GAME_COUNT),
        ("unguarded", 3 * GAME_COUNT),
    ):
        metrics = _require_fields(
            block[name], _MAHJONG_FIELDS, f"secondary_diagnostics.{name}"
        )
        if metrics["round_count"] != expected_rounds:
            raise _error(
                f"secondary_diagnostics.{name} was not aggregated over its own "
                "seat population"
            )


def _validate_guard_diagnostics(block: object) -> dict:
    diagnostics = _require_fields(block, _GUARD_DIAGNOSTIC_FIELDS, "guard_diagnostics")
    total = _require_count(
        diagnostics["learned_decision_count"],
        "guard_diagnostics.learned_decision_count",
    )
    if total == 0:
        raise _error(
            "a valid diagnostic run must have at least one learned-path decision"
        )
    keep_available = _require_count(
        diagnostics["keep_shanten_available_count"],
        "guard_diagnostics.keep_shanten_available_count",
    )
    no_keep_available = _require_count(
        diagnostics["no_keep_shanten_available_count"],
        "guard_diagnostics.no_keep_shanten_available_count",
    )
    if keep_available + no_keep_available != total:
        raise _error(
            "guard_diagnostics keep-shanten availability counts do not partition "
            "the learned decisions"
        )
    if diagnostics["keep_shanten_available_rate"] != keep_available / total:
        raise _error("guard_diagnostics.keep_shanten_available_rate is not derivable")
    unguarded_worsen = _require_count(
        diagnostics["unguarded_worsen_count"],
        "guard_diagnostics.unguarded_worsen_count",
    )
    unguarded_keep = _require_count(
        diagnostics["unguarded_keep_count"], "guard_diagnostics.unguarded_keep_count"
    )
    if unguarded_worsen + unguarded_keep != total:
        raise _error(
            "guard_diagnostics unguarded worsen/keep counts do not partition the "
            "learned decisions"
        )
    action_change = _require_count(
        diagnostics["action_change_count"], "guard_diagnostics.action_change_count"
    )
    if action_change > keep_available:
        raise _error(
            "guard_diagnostics.action_change_count cannot exceed the "
            "keep_shanten_available_count"
        )
    if diagnostics["action_change_rate"] != action_change / total:
        raise _error("guard_diagnostics.action_change_rate is not derivable")
    if (
        _require_count(
            diagnostics["guarded_worsen_among_available_count"],
            "guard_diagnostics.guarded_worsen_among_available_count",
        )
        != 0
    ):
        raise _error("guard_diagnostics.guarded_worsen_among_available_count must be 0")
    for name in (
        "mean_baseline_post_discard_shanten",
        "mean_guarded_post_discard_shanten",
    ):
        value = diagnostics[name]
        if type(value) is not float or value < 0:
            raise _error(f"guard_diagnostics.{name} must be a non-negative float")
    lower = _require_count(
        diagnostics["paired_lower_count"], "guard_diagnostics.paired_lower_count"
    )
    equal = _require_count(
        diagnostics["paired_equal_count"], "guard_diagnostics.paired_equal_count"
    )
    higher = _require_count(
        diagnostics["paired_higher_count"], "guard_diagnostics.paired_higher_count"
    )
    if lower + equal + higher != total:
        raise _error(
            "guard_diagnostics paired post-discard shanten counts do not "
            "partition the learned decisions"
        )
    if higher != 0:
        raise _error("guard_diagnostics.paired_higher_count must be 0")
    return diagnostics


def validate_diagnostic_result(document: object) -> dict:
    """diagnostic result documentをlocked constantと再導出でfail closedに検証する。"""
    validated = _require_fields(document, _RESULT_FIELDS, "diagnostic result")
    for name, expected in (
        ("schema_version", DIAGNOSTIC_SCHEMA_VERSION),
        ("diagnostic_id", DIAGNOSTIC_ID),
        ("source_issue", SOURCE_ISSUE),
        ("predecessor_issues", list(PREDECESSOR_ISSUES)),
        ("parent_issue", PARENT_ISSUE),
        ("protocol_id", PROTOCOL_ID),
        ("primary_changed_axis", PRIMARY_CHANGED_AXIS),
        ("locked_unchanged_axes", list(LOCKED_UNCHANGED_AXES)),
        ("plan", plan_block()),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("limitations", list(LIMITATIONS)),
        ("next_action_boundary", NEXT_ACTION_BOUNDARY),
        (
            "interpretation_boundary",
            {
                "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
                "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
                "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
            },
        ),
    ):
        if validated[name] != expected:
            raise _error(f"diagnostic result {name} is not the locked one")
    guarded = _validate_guarded_candidate(validated["guarded_candidate"])
    unguarded = _validate_unguarded_candidate(validated["unguarded_candidate"])
    _validate_arms_share_base_identity(guarded, unguarded)
    _validate_artifact(validated["strength_artifact"])
    _validate_summary(validated["canonical_summary"])
    _validate_guard_diagnostics(validated["guard_diagnostics"])
    _validate_secondary(validated["secondary_diagnostics"])
    serving = validated["serving_diagnostics"]
    if type(serving) is not dict or set(serving) != {"guarded", "unguarded"}:
        raise _error("serving_diagnostics must describe both arms")
    for name in ("guarded", "unguarded"):
        _validate_diagnostics(serving[name], f"serving_diagnostics.{name}")
    parse_execution_provenance(validated["provenance"])
    if validated["result_identity"] != result_identity(validated):
        raise _error(
            "result_identity is not the digest of this result document; the "
            "recorded measurement was edited after the fact"
        )
    classification = validated["classification"]
    if classification is not None:
        if classification not in _OUTCOME_VALUES:
            raise _error(f"unknown shanten guard outcome: {classification!r}")
        if classification != derive_classification(validated).value:
            raise _error(
                "the recorded classification is not the outcome that the locked "
                "Issue #173 rule derives from the guard diagnostics / canonical "
                "interval"
            )
    return validated


# --- Durable result persistence ---------------------------------------------
#
# `SingleRoundStrengthArtifact`はwrite-once / strict readbackだが、guard
# diagnostics（`action_change_count`等）とexhaustive classificationはこの
# result documentにしか存在しない。したがってresult document自体も
# strength artifactと同じ規律（write-once、staging + atomic rename、
# strict readback）でoperator-local durable storageへ保存する。


def save_diagnostic_result(path: str | Path, document: dict) -> dict:
    """diagnostic result document（unclassified / classified）をwrite-onceで保存する。

    既存fileを上書きせず、公開後は必ず`load_diagnostic_result()`で読み直した
    結果を返す。stdoutやin-memory documentをmeasurementのsource of truthに
    しないための境界である。
    """
    path = Path(path)
    if path.exists():
        raise _error(
            "diagnostic result destination already exists; results are write-once"
        )
    validated = validate_diagnostic_result(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging_name = mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".staging"
    )
    staging = Path(staging_name)
    published = False
    try:
        with open(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json_text(validated))
        staging.rename(path)
        published = True
    finally:
        if not published:
            staging.unlink(missing_ok=True)
    return load_diagnostic_result(path)


def load_diagnostic_result(path: str | Path) -> dict:
    """diagnostic result documentをdiskから読み、strict validateして返す。

    document自身が名乗る値をauthorityにしない。bytesそのものがcanonical
    JSONであることと、`validate_diagnostic_result()`の全条件を要求する。
    """
    path = Path(path)
    if not path.is_file():
        raise _error("diagnostic result path does not exist or is not a file")
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise _error("diagnostic result file is not valid JSON") from error
    if canonical_json_text(document) != text:
        raise _error("diagnostic result file is not canonical JSON")
    return validate_diagnostic_result(document)


def classify_interval(lower: float, upper: float) -> ShantenGuardOutcome:
    """lockedなone-way classification ruleをcanonical intervalへ適用する。"""
    if lower > 0:
        return ShantenGuardOutcome.ROLLOUT_SIGNAL
    if upper < 0:
        return ShantenGuardOutcome.ROLLOUT_NEGATIVE
    return ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE


def derive_classification(document: dict) -> ShantenGuardOutcome:
    """result documentからoutcomeを機械的に導出する。

    `guard_diagnostics.action_change_count == 0`は、intervalの符号より優先する
    `SHANTEN GUARD INACTIVE`である。secondary metricsとserving diagnosticsは
    ここを一切通らない。`SHANTEN GUARD EVIDENCE BLOCKED`と`STOP / INVALID`は
    result documentが作られる前のpre-result stateであり、ここからは導出され
    ない。
    """
    if document["guard_diagnostics"]["action_change_count"] == 0:
        return ShantenGuardOutcome.INACTIVE
    statistics = document["canonical_summary"]["seed_block_statistics"]
    return classify_interval(
        statistics["normal_approx_95_interval_lower"],
        statistics["normal_approx_95_interval_upper"],
    )


def bind_recorded_candidate(
    validated: dict, checkpoint: LoadedP1ServingCheckpoint
) -> LoadedP1ServingCheckpoint:
    """classification時に、result documentをactual serving checkpointへbindする。

    `p1_gate_b.bind_recorded_candidate()`と同じ理由で、渡された
    `LoadedP1ServingCheckpoint`のbundleを改めてdiskからstrict readbackする。
    """
    if not isinstance(checkpoint, LoadedP1ServingCheckpoint):
        raise TypeError("checkpoint must be a LoadedP1ServingCheckpoint")
    reloaded = load_p1_serving_checkpoint(checkpoint.path)
    if reloaded.real_candidate_materialization is not True:
        raise _error(
            "the strict readback did not resolve to the exact retained #162 candidate"
        )
    if guarded_candidate_block(reloaded) != validated["guarded_candidate"]:
        raise _error(
            "the recorded guarded_candidate does not match the serving checkpoint "
            "that was strict readback for this classification"
        )
    if unguarded_candidate_block(reloaded) != validated["unguarded_candidate"]:
        raise _error(
            "the recorded unguarded_candidate does not match the serving "
            "checkpoint that was strict readback for this classification"
        )
    return reloaded


def bind_recorded_artifact(
    validated: dict, artifact_path: str | Path
) -> SingleRoundStrengthArtifact:
    """classification時に、result documentをactual strength artifactへbindする。

    `result_identity`はdocument自身のself-consistencyでしかなく、external
    source of truthであるstrength artifactへのbindingにはならない。documentの
    `canonical_summary`を書き換えてから`result_identity`を計算し直せば、raw
    100 gamesと一致しないsummaryでも自己整合的なdocumentは作れてしまう。

    そこでartifact fileを改めてdiskからstrict readbackし、そのbytesのsha256、
    protocol条件、そしてraw gamesから再導出したcanonical summaryをresult
    documentと突き合わせる。この境界を通れるのは、実際にそのraw 100 gamesを
    持つartifactだけである。
    """
    artifact_path = Path(artifact_path)
    recorded = validated["strength_artifact"]
    if not artifact_path.is_file():
        raise _error(
            "the strength artifact this classification binds to does not exist "
            "at the given path; a shanten guard outcome binds to an actually "
            "readback artifact, never to a recorded digest alone"
        )
    if artifact_path.name != recorded["filename"]:
        raise _error(
            "the strength artifact file name is not the one the result records"
        )
    if _sha256_file(artifact_path) != recorded["sha256"]:
        raise _error(
            "the strength artifact bytes do not match the digest the result "
            "records; the recorded measurement and the retained artifact are "
            "not the same run"
        )
    artifact = load_single_round_artifact(artifact_path)
    if artifact.schema_version != recorded["schema_version"]:
        raise _error("the strength artifact schema version is not the recorded one")
    if artifact.evaluation_protocol != recorded["evaluation_protocol"]:
        raise _error(
            "the strength artifact evaluation protocol is not the recorded one"
        )
    if len(artifact.game_results) != recorded["game_count"]:
        raise _error("the strength artifact game count is not the recorded one")
    require_diagnostic_artifact(
        artifact,
        guarded_identity=validated["guarded_candidate"]["identity"],
        unguarded_identity=validated["unguarded_candidate"]["identity"],
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise _error(
            "the canonical summary regenerated from the retained raw games "
            "differs from the summary stored in the artifact"
        )
    if summary_to_dict(summary) != validated["canonical_summary"]:
        raise _error(
            "the recorded canonical summary is not the one the retained raw "
            "games regenerate; a shanten guard outcome is classified from the "
            "artifact's own 100 games, never from a summary written into the "
            "result document"
        )
    return artifact


def record_classification(
    document: dict,
    outcome: ShantenGuardOutcome,
    *,
    checkpoint: LoadedP1ServingCheckpoint,
    artifact_path: str | Path,
) -> dict:
    """validated resultへexhaustive outcomeを1件だけ記録する。

    `checkpoint`と`artifact_path`はいずれも必須である。classificationは
    identity文字列やdocument内のself-consistencyではなく、diskからstrict
    readbackしたcheckpoint bytesとstrength artifact bytesの両方へbindされる
    （`bind_recorded_candidate()` / `bind_recorded_artifact()`）。
    """
    validated = validate_diagnostic_result(document)
    if not isinstance(outcome, ShantenGuardOutcome):
        raise TypeError("outcome must be a ShantenGuardOutcome")
    if not isinstance(checkpoint, LoadedP1ServingCheckpoint):
        raise TypeError("checkpoint must be a LoadedP1ServingCheckpoint")
    if validated["classification"] is not None:
        raise _error("this diagnostic result already records an outcome")
    if outcome not in _RECORDABLE_OUTCOMES:
        raise _error(
            f"{outcome.value!r} is a pre-result state, not an outcome derived "
            "from a valid diagnostic execution"
        )
    if validated["guarded_candidate"]["real_candidate_materialization"] is not True:
        raise _error(
            "a shanten guard outcome may only be recorded for the exact retained "
            "#162 candidate; a fixture or substitute candidate is never real "
            "evidence"
        )
    derived = derive_classification(validated)
    if outcome is not derived:
        raise _error(
            f"the locked Issue #173 rule derives {derived.value!r} from this "
            f"guard diagnostics / canonical interval, not {outcome.value!r}"
        )
    bind_recorded_candidate(validated, checkpoint)
    bind_recorded_artifact(validated, artifact_path)
    return validate_diagnostic_result({**validated, "classification": outcome.value})


def save_classified_result(
    classified_result_path: str | Path,
    document: dict,
    outcome: ShantenGuardOutcome,
    *,
    checkpoint: LoadedP1ServingCheckpoint,
    artifact_path: str | Path,
) -> dict:
    """review後のexhaustive outcomeを1件だけ記録し、別のwrite-once fileへ保存する。

    unclassified resultは上書きせず、classified resultは常に新しいpathへ
    write-onceで公開する。返り値は保存後に`load_diagnostic_result()`で
    読み直した結果である。
    """
    classified = record_classification(
        document, outcome, checkpoint=checkpoint, artifact_path=artifact_path
    )
    return save_diagnostic_result(classified_result_path, classified)


# --- Execution ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShantenGuardDiagnosticMeasurement:
    """1回のdiagnostic executionのimmutable artifactと再生成したcanonical summary。"""

    artifact: SingleRoundStrengthArtifact
    summary: object
    document: dict
    result_path: Path
    derived_outcome: ShantenGuardOutcome
    guard_diagnostics: GuardDiagnostics
    guarded_activation_diagnostics: ActivationDiagnostics
    unguarded_activation_diagnostics: ActivationDiagnostics
    wall_clock_seconds: float
    cpu_seconds: float


def run_shanten_guard_diagnostic(
    checkpoint: LoadedP1ServingCheckpoint,
    artifact_path: str | Path,
    result_path: str | Path,
    *,
    progress_callback=None,
) -> ShantenGuardDiagnosticMeasurement:
    """diagnosticを1回実行し、artifactとresult documentを保存してから読み直して検証する。

    real execution前に`require_fresh_seed_plan()`をmachine-enforcedに実行する。
    seed freshnessはpre-execution protocol conditionであり、caller discipline
    ではなくこのexecution boundary自体がfail closedにする。

    新しいgame runner / rotation / aggregationを作らず、既存
    `run_single_round_evaluation()`をserial（workers = 1）でthin reuseする。
    """
    require_fresh_seed_plan()
    artifact_path = Path(artifact_path)
    result_path = Path(result_path)
    plan, guard_registry, unguarded_registry = build_diagnostic_plan(checkpoint)

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    save_single_round_artifact(result, artifact_path)
    artifact = load_single_round_artifact(artifact_path)
    require_diagnostic_artifact(
        artifact,
        guarded_identity=plan.candidate.identity,
        unguarded_identity=plan.baseline.identity,
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    if summary != artifact.summary:
        raise _error(
            "the canonical summary regenerated from raw results differs from the "
            "stored artifact summary"
        )

    guard_diagnostics = collect_guard_diagnostics(guard_registry.instances)
    guarded_activation = collect_activation_diagnostics(guard_registry.instances)
    unguarded_activation = collect_activation_diagnostics(unguarded_registry.instances)

    document = save_diagnostic_result(
        result_path,
        build_diagnostic_result(
            checkpoint=checkpoint,
            artifact=artifact,
            artifact_path=artifact_path,
            summary=summary,
            guard_diagnostics=guard_diagnostics,
            guarded_activation_diagnostics=guarded_activation,
            unguarded_activation_diagnostics=unguarded_activation,
        ),
    )
    return ShantenGuardDiagnosticMeasurement(
        artifact=artifact,
        summary=summary,
        document=document,
        result_path=result_path,
        derived_outcome=derive_classification(document),
        guard_diagnostics=guard_diagnostics,
        guarded_activation_diagnostics=guarded_activation,
        unguarded_activation_diagnostics=unguarded_activation,
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "ARTIFACT_RETENTION_KEY",
    "CANDIDATE_RETENTION_KEY",
    "CLASSIFICATION_RULE",
    "DIAGNOSTIC_ID",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "GAME_COUNT",
    "GAME_MODE",
    "GUARD_RULE",
    "INTERPRETATION_BOUNDARY",
    "LIMITATIONS",
    "LOCKED_UNCHANGED_AXES",
    "MAX_WORKERS",
    "NEXT_ACTION_BOUNDARY",
    "ORDERED_SEEDS",
    "PARENT_ISSUE",
    "PREDECESSOR_ISSUES",
    "PRIMARY_CHANGED_AXIS",
    "RETENTION_BACKEND",
    "ROLE",
    "ROTATIONS_PER_SEED",
    "SEED_BLOCK_COUNT",
    "SEED_PLAN_REFORMULATE",
    "SOURCE_ISSUE",
    "ShantenGuardDiagnosticError",
    "ShantenGuardDiagnosticMeasurement",
    "ShantenGuardOutcome",
    "artifact_block",
    "bind_recorded_artifact",
    "bind_recorded_candidate",
    "build_diagnostic_plan",
    "build_diagnostic_result",
    "check_seed_freshness",
    "classify_interval",
    "declared_allocated_seeds",
    "derive_classification",
    "guard_diagnostics_block",
    "guarded_candidate_block",
    "guarded_mahjong_metrics",
    "load_diagnostic_result",
    "plan_block",
    "record_classification",
    "require_diagnostic_artifact",
    "require_diagnostic_seed",
    "require_fresh_seed_plan",
    "result_identity",
    "run_shanten_guard_diagnostic",
    "save_classified_result",
    "save_diagnostic_result",
    "serving_diagnostics_block",
    "unguarded_candidate_block",
    "unguarded_mahjong_metrics",
    "validate_diagnostic_result",
]
