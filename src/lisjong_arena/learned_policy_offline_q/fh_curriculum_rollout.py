"""Fresh shared F-vs-Y rollout and primary classification (Issue #165).

```text
Arm F candidate (finite-horizon teacher)      Arm Y candidate (yakuhai-call teacher)
        |                                                  |
        +------------- SingleRoundEvaluationPlan -----------+
                             |
                  run_single_round_evaluation()   serial / workers = 1
                             |
                  save_single_round_artifact()    immutable
                             |
                  load_single_round_artifact()    strict readback
                             |
                  summarize_single_round_strength()  canonical re-derivation
                             |
                  build_rollout_result() -> validate -> derive_classification()
```

ABBB rotationは既存single-round protocol invariantであり、ここでは
`candidate = F` / `baseline = Y`を割り当てるだけである。

```text
rotation 0  [F, Y, Y, Y]
rotation 1  [Y, F, Y, Y]
rotation 2  [Y, Y, F, Y]
rotation 3  [Y, Y, Y, F]
```

新しいgame runner、seat rotation、score aggregation、statisticsは実装しない。

## primary classification

```text
seed-block F-vs-Y score delta
normal-approx 95% interval

lower > 0   -> FINITEHORIZON CURRICULUM ROLLOUT SIGNAL
upper < 0   -> FINITEHORIZON CURRICULUM ROLLOUT NEGATIVE
otherwise   -> FINITEHORIZON CURRICULUM ROLLOUT INCONCLUSIVE
```

secondary Mahjong diagnostics、serving diagnostics、offline mechanism
diagnosticsは`derive_classification()`を一切通らない。
`CURRICULUM EVIDENCE BLOCKED`と`STOP / INVALID`はresult documentが作られる
前のpre-result stateであり、intervalからは導出されない。

両candidateとも`#162`のP1 hybrid serving semanticsを使う。**Arm Fの
serving fallbackもyakuhai-callである**。fallbackまでFiniteHorizonへ変えると
teacher axisだけでなくserving axisも変わるため、`#165`では変更しない。
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
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

from .artifact import PROVENANCE_FIELDS
from .errors import OfflineQError
from .fh_curriculum import (
    CLASSIFICATION_RULE,
    CURRICULUM_LIMITATIONS,
    INTERPRETATION_BOUNDARY,
    LOCKED_UNCHANGED_AXES,
    NEXT_ACTION_BOUNDARY,
    PARENT_ISSUE,
    PREDECESSOR_ISSUES,
    PRIMARY_CHANGED_AXIS,
    ROLLOUT_GAME_COUNT,
    ROLLOUT_GAME_MODE,
    ROLLOUT_ORDERED_SEEDS,
    ROLLOUT_ROTATIONS_PER_SEED,
    ROLLOUT_SEED_BLOCK_COUNT,
    SOURCE_ISSUE,
    CurriculumArm,
    CurriculumOutcome,
    arm_block,
    candidate_retention_block,
    require_rollout_seed,
    result_retention_block,
    rollout_plan_block,
    teacher_block,
)
from .fh_curriculum_candidate import (
    CANDIDATE_SCHEMA_VERSION,
    SELECTED_EPOCH,
    LoadedArmCandidate,
    load_arm_candidate,
    require_candidate_identity,
    require_candidate_pair,
)
from .fh_curriculum_dataset import experiment_block
from .p1_serving import create_p1_hybrid_runtime
from .protocol import PROTOCOL_ID
from .strength import (
    ActivationDiagnostics,
    PolicyInstanceRegistry,
    collect_activation_diagnostics,
)

ROLLOUT_SCHEMA_VERSION = "arena-learned-policy-finite-horizon-curriculum-rollout-v1"

_RECORDABLE_OUTCOMES = (
    CurriculumOutcome.ROLLOUT_SIGNAL,
    CurriculumOutcome.ROLLOUT_NEGATIVE,
    CurriculumOutcome.ROLLOUT_INCONCLUSIVE,
)
_OUTCOME_VALUES = frozenset(outcome.value for outcome in CurriculumOutcome)


class CurriculumRolloutError(OfflineQError):
    """rolloutのplan / execution / artifact / result契約の違反。"""


def _error(message: str) -> CurriculumRolloutError:
    return CurriculumRolloutError(message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Plan -----------------------------------------------------------------


def build_rollout_plan(
    curriculum: LoadedArmCandidate, control: LoadedArmCandidate
) -> tuple[SingleRoundEvaluationPlan, PolicyInstanceRegistry, PolicyInstanceRegistry]:
    """F candidateとY controlでlocked rollout planを組み立てる。

    Policy instanceはgame・seatごとにruntime factoryから新規生成され、
    checkpointはruntime構築時に1回だけloadされる。
    """
    require_candidate_pair(control, curriculum)
    for seed in ROLLOUT_ORDERED_SEEDS:
        require_rollout_seed(seed)
    registries = []
    specs = []
    for candidate in (curriculum, control):
        runtime = create_p1_hybrid_runtime(
            candidate.model, supported_indices=candidate.supported_indices
        )
        registry = PolicyInstanceRegistry(runtime.create_policy)
        registries.append(registry)
        specs.append(
            PolicySpec(
                identity=candidate.candidate_identity, factory=registry.create_policy
            )
        )
    plan = SingleRoundEvaluationPlan(
        candidate=specs[0], baseline=specs[1], seeds=ROLLOUT_ORDERED_SEEDS
    )
    return plan, registries[0], registries[1]


# --- Artifact binding -----------------------------------------------------


def require_rollout_artifact(
    artifact: SingleRoundStrengthArtifact,
    *,
    candidate_identity: str,
    baseline_identity: str,
) -> SingleRoundStrengthArtifact:
    """readbackしたartifactがrollout protocol条件を満たすことを確認する。

    partial run、seed mismatch、rotation mismatch、arm mismatchをfail closed
    する。duplicate gameとrotation欠落は`SingleRoundStrengthArtifact`自身の
    raw result contractが既に拒否している。
    """
    if not isinstance(artifact, SingleRoundStrengthArtifact):
        raise TypeError("artifact must be a SingleRoundStrengthArtifact")
    plan = artifact.plan
    if plan.seeds != ROLLOUT_ORDERED_SEEDS:
        raise _error("the artifact was not produced on the locked rollout seeds")
    if plan.game_mode != ROLLOUT_GAME_MODE:
        raise _error("the artifact game mode is not the locked rollout game mode")
    if plan.rotation_count != ROLLOUT_ROTATIONS_PER_SEED:
        raise _error("the artifact rotation count is not the locked one")
    if len(artifact.game_results) != ROLLOUT_GAME_COUNT:
        raise _error(
            f"a rollout artifact must contain exactly {ROLLOUT_GAME_COUNT} games; "
            f"got {len(artifact.game_results)} -- a partial run is never accepted"
        )
    if plan.candidate_identity != candidate_identity:
        raise _error("the artifact candidate is not the served Arm F candidate")
    if plan.baseline_identity != baseline_identity:
        raise _error("the artifact baseline is not the served Arm Y control")
    seat_counts: dict[int, int] = {}
    for game_result in artifact.game_results:
        seat = int(game_result.candidate_seat)
        seat_counts[seat] = seat_counts.get(seat, 0) + 1
    if sorted(seat_counts) != [0, 1, 2, 3] or set(seat_counts.values()) != {
        ROLLOUT_SEED_BLOCK_COUNT
    }:
        raise _error(
            "the Arm F candidate did not occupy each seat exactly once per seed"
        )
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
        "retention": result_retention_block(),
    }


# --- Result document ------------------------------------------------------


def candidate_block(candidate: LoadedArmCandidate) -> dict[str, object]:
    """1 armのcandidate logical identityとcanonical bindingを記録する。"""
    if not isinstance(candidate, LoadedArmCandidate):
        raise TypeError("candidate must be a LoadedArmCandidate")
    manifest = candidate.manifest
    return {
        "arm": arm_block(candidate.arm),
        "teacher": teacher_block(candidate.arm),
        "identity": candidate.candidate_identity,
        "binding": manifest["candidate_binding"],
        "canonical_model_weights_digest": candidate.canonical_model_weights_digest,
        "checkpoint_schema_version": manifest["checkpoint_schema_version"],
        "source_dataset_identity": manifest["source_dataset_identity"],
        "supported_indices_digest": manifest["supported_indices_digest"],
        "support_size": manifest["support_size"],
        "selected_epoch": manifest["selected_epoch"],
        "source_revisions": manifest["source_revisions"],
        "retention": candidate_retention_block(candidate.arm),
    }


def baseline_mahjong_metrics(game_results) -> dict[str, object]:
    """control (Arm Y) が担当した3 seat分のraw statsからMahjong metricsを集計する。

    canonical formulaは`single_round_evaluation`が所有し、ここではseat選択
    だけを行う。母数はcandidate側（game数）ではなく`3 x game数`である。
    """
    stats = [
        game_result.seat_round_stats[seat]
        for game_result in game_results
        for seat in range(4)
        if seat != int(game_result.candidate_seat)
    ]
    metrics = aggregate_seat_round_stats_metrics(stats)
    return {
        "round_count": metrics.round_count,
        "population": "baseline seats (3 per game)",
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


def candidate_mahjong_metrics(summary) -> dict[str, object]:
    """candidate (Arm F) 側は既存canonical summaryのmetricsをそのまま写す。"""
    metrics = summary.candidate_metrics.mahjong_metrics
    return {
        "round_count": metrics.round_count,
        "population": "candidate seat (1 per game)",
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


def serving_diagnostics_block(
    diagnostics: ActivationDiagnostics,
) -> dict[str, object]:
    """serving activation / fallback診断。classificationには使わない。

    illegal selection / non-finite output / resolve failureはserving path上で
    fail closedであり、1件でも起きればrunがabortしてresult documentは作られ
    ない。したがってvalid resultではこれらは常に0である。
    """
    if not isinstance(diagnostics, ActivationDiagnostics):
        raise TypeError("diagnostics must be an ActivationDiagnostics")
    return {
        **diagnostics.to_document(),
        "illegal_selection_count": 0,
        "non_finite_model_output_count": 0,
        "resolve_failure_count": 0,
        "fail_closed_at_decision_time": True,
    }


def result_identity(document: dict) -> str:
    """classificationとidentity自身を除いたcanonical bytesのdigest。"""
    payload = {**document, "classification": None, "result_identity": None}
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def build_rollout_result(
    *,
    curriculum: LoadedArmCandidate,
    control: LoadedArmCandidate,
    artifact: SingleRoundStrengthArtifact,
    artifact_path: Path,
    summary,
    curriculum_diagnostics: ActivationDiagnostics,
    control_diagnostics: ActivationDiagnostics,
    offline_diagnostics: dict | None = None,
) -> dict[str, object]:
    """versioned rollout result documentを組み立てる。

    `classification`は常に`None`で作られる。outcomeはvalidation後に
    `record_classification()`でだけ付与する。
    """
    document = {
        "rollout_schema_version": ROLLOUT_SCHEMA_VERSION,
        "experiment": experiment_block(),
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "primary_changed_axis": PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(LOCKED_UNCHANGED_AXES),
        "curriculum_candidate": candidate_block(curriculum),
        "control_candidate": candidate_block(control),
        "plan": rollout_plan_block(),
        "strength_artifact": artifact_block(artifact, artifact_path),
        "canonical_summary": summary_to_dict(summary),
        "secondary_diagnostics": {
            "curriculum": candidate_mahjong_metrics(summary),
            "control": baseline_mahjong_metrics(artifact.game_results),
        },
        "serving_diagnostics": {
            "curriculum": serving_diagnostics_block(curriculum_diagnostics),
            "control": serving_diagnostics_block(control_diagnostics),
        },
        "offline_diagnostics": offline_diagnostics,
        "classification_rule": dict(CLASSIFICATION_RULE),
        "limitations": list(CURRICULUM_LIMITATIONS),
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
    "rollout_schema_version",
    "experiment",
    "source_issue",
    "predecessor_issues",
    "parent_issue",
    "protocol_id",
    "primary_changed_axis",
    "locked_unchanged_axes",
    "curriculum_candidate",
    "control_candidate",
    "plan",
    "strength_artifact",
    "canonical_summary",
    "secondary_diagnostics",
    "serving_diagnostics",
    "offline_diagnostics",
    "classification_rule",
    "limitations",
    "interpretation_boundary",
    "next_action_boundary",
    "provenance",
    "result_identity",
    "classification",
}
_CANDIDATE_FIELDS = {
    "arm",
    "teacher",
    "identity",
    "binding",
    "canonical_model_weights_digest",
    "checkpoint_schema_version",
    "source_dataset_identity",
    "supported_indices_digest",
    "support_size",
    "selected_epoch",
    "source_revisions",
    "retention",
}
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


def _validate_candidate(block: object, arm: CurriculumArm, context: str) -> dict:
    candidate = _require_fields(block, _CANDIDATE_FIELDS, context)
    if candidate["arm"] != arm_block(arm):
        raise _error(f"{context} is not the {arm.value} arm")
    if candidate["teacher"] != teacher_block(arm):
        raise _error(f"{context} teacher is not the curated teacher of its arm")
    if candidate["checkpoint_schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise _error(f"{context} checkpoint schema version is not the locked one")
    if candidate["selected_epoch"] != SELECTED_EPOCH:
        raise _error(
            f"{context} is not the locked fixed_final_iteration epoch "
            f"{SELECTED_EPOCH} checkpoint"
        )
    revisions = candidate["source_revisions"]
    if type(revisions) is not dict or set(revisions) != PROVENANCE_FIELDS:
        raise _error(f"{context}.source_revisions fields are invalid")
    for name in (
        "canonical_model_weights_digest",
        "source_dataset_identity",
        "supported_indices_digest",
    ):
        value = candidate[name]
        if type(value) is not str or len(value) != 64:
            raise _error(f"{context}.{name} must be a 64 character sha256 digest")
    binding = candidate["binding"]
    if type(binding) is not dict:
        raise _error(f"{context}.binding must be an object")
    for name, recorded in (
        ("canonical_model_weights_digest", "canonical_model_weights_digest"),
        ("source_dataset_identity", "source_dataset_identity"),
        ("support_set_digest", "supported_indices_digest"),
    ):
        if binding.get(name) != candidate[recorded]:
            raise _error(
                f"{context}.binding does not describe the identities this "
                "candidate records"
            )
    if binding.get("arm") != arm_block(arm) or binding.get("teacher") != teacher_block(
        arm
    ):
        raise _error(f"{context}.binding is not bound to its own arm and teacher")
    if binding.get("source_revisions") != candidate["source_revisions"]:
        raise _error(f"{context}.binding does not carry the recorded source revisions")
    require_candidate_identity(candidate["identity"], binding)
    _require_count(candidate["support_size"], f"{context}.support_size")
    if candidate["retention"] != candidate_retention_block(arm):
        raise _error(f"{context} retention target is not the locked one")
    return candidate


def _validate_artifact(block: object) -> None:
    artifact = _require_fields(block, _ARTIFACT_FIELDS, "strength_artifact")
    if artifact["schema_version"] != SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION:
        raise _error("the strength artifact schema version is not the locked one")
    if artifact["evaluation_protocol"] != SINGLE_ROUND_EVALUATION_PROTOCOL:
        raise _error("the strength artifact evaluation protocol is not ABBB")
    if artifact["game_count"] != ROLLOUT_GAME_COUNT:
        raise _error("the strength artifact does not record the locked game count")
    sha256 = artifact["sha256"]
    if type(sha256) is not str or len(sha256) != 64:
        raise _error("strength_artifact.sha256 must be a 64 character sha256 digest")
    filename = artifact["filename"]
    if type(filename) is not str or not filename or "/" in filename:
        raise _error("strength_artifact.filename must be a bare file name")
    if artifact["retention"] != result_retention_block():
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
    if metrics.get("game_count") != ROLLOUT_GAME_COUNT:
        raise _error(
            "the canonical summary was not aggregated over the locked game count"
        )
    statistics = _require_fields(
        block["seed_block_statistics"], _SEED_BLOCK_FIELDS, "seed_block_statistics"
    )
    if statistics["seed_block_count"] != ROLLOUT_SEED_BLOCK_COUNT:
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
    if sum(ordered) != ROLLOUT_SEED_BLOCK_COUNT:
        raise _error("the seed block sign counts do not partition the seed blocks")
    return statistics


def _validate_diagnostics(block: object, context: str) -> None:
    diagnostics = _require_fields(block, _DIAGNOSTIC_FIELDS, context)
    total = _require_count(diagnostics["total_decisions"], f"{context}.total_decisions")
    if total == 0:
        raise _error(f"{context} records no decision; a valid rollout has decisions")
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
            "do not partition the decisions"
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
                "before a rollout result exists"
            )
    if (
        _require_bool(
            diagnostics["fail_closed_at_decision_time"],
            f"{context}.fail_closed_at_decision_time",
        )
        is not True
    ):
        raise _error("rollout serving is fail closed at decision time")


def _validate_secondary(block: object) -> None:
    if type(block) is not dict or set(block) != {"curriculum", "control"}:
        raise _error("secondary_diagnostics must describe both arms")
    for name, expected_rounds in (
        ("curriculum", ROLLOUT_GAME_COUNT),
        ("control", 3 * ROLLOUT_GAME_COUNT),
    ):
        metrics = _require_fields(
            block[name], _MAHJONG_FIELDS, f"secondary_diagnostics.{name}"
        )
        if metrics["round_count"] != expected_rounds:
            raise _error(
                f"secondary_diagnostics.{name} was not aggregated over its own "
                "seat population"
            )


def validate_rollout_result(document: object) -> dict:
    """rollout result documentをlocked constantと再導出でfail closedに検証する。"""
    validated = _require_fields(document, _RESULT_FIELDS, "rollout result")
    for name, expected in (
        ("rollout_schema_version", ROLLOUT_SCHEMA_VERSION),
        ("experiment", experiment_block()),
        ("source_issue", SOURCE_ISSUE),
        ("predecessor_issues", list(PREDECESSOR_ISSUES)),
        ("parent_issue", PARENT_ISSUE),
        ("protocol_id", PROTOCOL_ID),
        ("primary_changed_axis", PRIMARY_CHANGED_AXIS),
        ("locked_unchanged_axes", list(LOCKED_UNCHANGED_AXES)),
        ("plan", rollout_plan_block()),
        ("classification_rule", dict(CLASSIFICATION_RULE)),
        ("limitations", list(CURRICULUM_LIMITATIONS)),
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
            raise _error(f"rollout result {name} is not the locked one")
    curriculum = _validate_candidate(
        validated["curriculum_candidate"],
        CurriculumArm.CURRICULUM,
        "curriculum_candidate",
    )
    control = _validate_candidate(
        validated["control_candidate"], CurriculumArm.CONTROL, "control_candidate"
    )
    for name in ("binding", "identity", "source_dataset_identity"):
        if curriculum[name] == control[name]:
            raise _error(
                f"the two candidates share the same {name}; the rollout compares "
                "two distinct arm candidates"
            )
    for name in ("model", "training", "hybrid_activation", "fallback_policy"):
        if curriculum["binding"].get(name) != control["binding"].get(name):
            raise _error(
                f"the two candidates do not share the same {name}; the teacher "
                "must be the only changed axis"
            )
    if curriculum["source_revisions"] != control["source_revisions"]:
        raise _error("the two candidates do not share the same source revisions")
    _validate_artifact(validated["strength_artifact"])
    _validate_summary(validated["canonical_summary"])
    _validate_secondary(validated["secondary_diagnostics"])
    serving = validated["serving_diagnostics"]
    if type(serving) is not dict or set(serving) != {"curriculum", "control"}:
        raise _error("serving_diagnostics must describe both arms")
    for name in ("curriculum", "control"):
        _validate_diagnostics(serving[name], f"serving_diagnostics.{name}")
    offline = validated["offline_diagnostics"]
    if offline is not None and type(offline) is not dict:
        raise _error("offline_diagnostics must be an object or null")
    parse_execution_provenance(validated["provenance"])
    if validated["result_identity"] != result_identity(validated):
        raise _error(
            "result_identity is not the digest of this result document; the "
            "recorded measurement was edited after the fact"
        )
    classification = validated["classification"]
    if classification is not None:
        if classification not in _OUTCOME_VALUES:
            raise _error(f"unknown curriculum outcome: {classification!r}")
        if classification != derive_classification(validated).value:
            raise _error(
                "the recorded classification is not the outcome that the locked "
                "Issue #165 rule derives from the canonical interval"
            )
    return validated


def classify_interval(lower: float, upper: float) -> CurriculumOutcome:
    """lockedなclassification ruleをcanonical intervalへ適用する。"""
    if lower > 0:
        return CurriculumOutcome.ROLLOUT_SIGNAL
    if upper < 0:
        return CurriculumOutcome.ROLLOUT_NEGATIVE
    return CurriculumOutcome.ROLLOUT_INCONCLUSIVE


def derive_classification(document: dict) -> CurriculumOutcome:
    """result documentのcanonical intervalからoutcomeを機械的に導出する。

    secondary metrics、serving diagnostics、offline diagnosticsはここを一切
    通らない。`CURRICULUM EVIDENCE BLOCKED`と`STOP / INVALID`はresult
    documentが作られる前のpre-result stateであり、ここからは導出されない。
    """
    statistics = document["canonical_summary"]["seed_block_statistics"]
    return classify_interval(
        statistics["normal_approx_95_interval_lower"],
        statistics["normal_approx_95_interval_upper"],
    )


def bind_recorded_candidates(
    validated: dict,
    *,
    curriculum: LoadedArmCandidate,
    control: LoadedArmCandidate,
) -> tuple[LoadedArmCandidate, LoadedArmCandidate]:
    """classification時に、result documentをactual checkpointへbindする。

    identity digestの自己整合性は「その checkpoint を実際にservingした」ことを
    証明しない。既知のdigest文字列を並べてmatching bindingとidentityを再生成
    すれば整合したdocumentは作れてしまう。

    そこでこのfunctionは、渡された両`LoadedArmCandidate`のbundleを**改めて
    diskからstrict readbackする**。`load_arm_candidate()`はweights bytesを読み、
    そのbytesからcanonical weights digestを再導出する。したがってこの境界を
    通れるのは、実際にそのweightsを持つbundleだけである。
    """
    reloaded = []
    for candidate, arm, block in (
        (curriculum, CurriculumArm.CURRICULUM, "curriculum_candidate"),
        (control, CurriculumArm.CONTROL, "control_candidate"),
    ):
        if not isinstance(candidate, LoadedArmCandidate):
            raise TypeError("candidates must be LoadedArmCandidate values")
        entry = load_arm_candidate(candidate.path, arm=arm)
        if candidate_block(entry) != validated[block]:
            raise _error(
                f"the recorded {block} does not match the serving checkpoint that "
                "was strict readback for this classification; a curriculum outcome "
                "binds to actually loaded checkpoints, never to identity strings"
            )
        reloaded.append(entry)
    require_candidate_pair(reloaded[1], reloaded[0])
    return reloaded[0], reloaded[1]


def record_classification(
    document: dict,
    outcome: CurriculumOutcome,
    *,
    curriculum: LoadedArmCandidate,
    control: LoadedArmCandidate,
) -> dict:
    """validated resultへexhaustive outcomeを1件だけ記録する。"""
    validated = validate_rollout_result(document)
    if not isinstance(outcome, CurriculumOutcome):
        raise TypeError("outcome must be a CurriculumOutcome")
    if validated["classification"] is not None:
        raise _error("this rollout result already records an outcome")
    if outcome not in _RECORDABLE_OUTCOMES:
        raise _error(
            f"{outcome.value!r} is a pre-result state, not an outcome derived "
            "from a valid rollout execution"
        )
    derived = derive_classification(validated)
    if outcome is not derived:
        raise _error(
            f"the locked Issue #165 rule derives {derived.value!r} from this "
            f"canonical interval, not {outcome.value!r}"
        )
    bind_recorded_candidates(validated, curriculum=curriculum, control=control)
    return validate_rollout_result({**validated, "classification": outcome.value})


# --- Execution ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurriculumRolloutMeasurement:
    """1回のrollout executionのimmutable artifactと再生成したcanonical summary。"""

    artifact: SingleRoundStrengthArtifact
    summary: object
    document: dict
    derived_outcome: CurriculumOutcome
    curriculum_diagnostics: ActivationDiagnostics
    control_diagnostics: ActivationDiagnostics
    wall_clock_seconds: float
    cpu_seconds: float


def run_curriculum_rollout(
    curriculum: LoadedArmCandidate,
    control: LoadedArmCandidate,
    artifact_path: str | Path,
    *,
    offline_diagnostics: dict | None = None,
    progress_callback=None,
) -> CurriculumRolloutMeasurement:
    """rolloutを1回実行し、artifactを保存してから読み直して検証する。

    新しいgame runner / rotation / aggregationを作らず、既存
    `run_single_round_evaluation()`をserial（workers = 1）でthin reuseする。
    """
    artifact_path = Path(artifact_path)
    plan, curriculum_registry, control_registry = build_rollout_plan(
        curriculum, control
    )

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    result = run_single_round_evaluation(plan, progress_callback=progress_callback)
    wall_clock_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    save_single_round_artifact(result, artifact_path)
    artifact = load_single_round_artifact(artifact_path)
    require_rollout_artifact(
        artifact,
        candidate_identity=curriculum.candidate_identity,
        baseline_identity=control.candidate_identity,
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
    curriculum_diagnostics = collect_activation_diagnostics(
        curriculum_registry.instances
    )
    control_diagnostics = collect_activation_diagnostics(control_registry.instances)
    document = validate_rollout_result(
        build_rollout_result(
            curriculum=curriculum,
            control=control,
            artifact=artifact,
            artifact_path=artifact_path,
            summary=summary,
            curriculum_diagnostics=curriculum_diagnostics,
            control_diagnostics=control_diagnostics,
            offline_diagnostics=offline_diagnostics,
        )
    )
    return CurriculumRolloutMeasurement(
        artifact=artifact,
        summary=summary,
        document=document,
        derived_outcome=derive_classification(document),
        curriculum_diagnostics=curriculum_diagnostics,
        control_diagnostics=control_diagnostics,
        wall_clock_seconds=wall_clock_seconds,
        cpu_seconds=cpu_seconds,
    )


__all__ = [
    "ROLLOUT_SCHEMA_VERSION",
    "CurriculumRolloutError",
    "CurriculumRolloutMeasurement",
    "artifact_block",
    "baseline_mahjong_metrics",
    "bind_recorded_candidates",
    "build_rollout_plan",
    "build_rollout_result",
    "candidate_block",
    "candidate_mahjong_metrics",
    "classify_interval",
    "derive_classification",
    "record_classification",
    "require_rollout_artifact",
    "result_identity",
    "run_curriculum_rollout",
    "serving_diagnostics_block",
    "validate_rollout_result",
]
