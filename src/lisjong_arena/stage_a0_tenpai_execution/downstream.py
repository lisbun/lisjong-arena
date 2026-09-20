"""Phase E3 bounded A-vs-T downstream evaluation for #262."""

from __future__ import annotations

import hashlib
import json
import os
from functools import cache
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.learned_policy_stage3.policy import create_serving_runtime
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.paired_evaluation import (
    arm_diagnostics,
    artifact_file_digest,
    focal_seed_block_means,
    load_arm_artifact,
    paired_deltas_from_block_means,
    summarize_paired_deltas,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_artifact import save_single_round_artifact
from lisjong_arena.single_round_evaluation import (
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

from .errors import StageA0DownstreamError
from .gate import load_gate
from .protocol import (
    Arm,
    DOWNSTREAM_INCONCLUSIVE,
    DOWNSTREAM_NEGATIVE,
    DOWNSTREAM_POSITIVE,
    EXPECTED_LOCK_B_IDENTITY,
    GATE_PASS,
    policy_identity,
)
from .training import load_checkpoint

A_CHECKPOINT_ENV = "LISJONG_STAGE_A0_A_ANCHOR_CHECKPOINT"
T_CHECKPOINT_ENV = "LISJONG_STAGE_A0_T_ANCHOR_CHECKPOINT"


@cache
def _a_runtime():
    path = os.environ.get(A_CHECKPOINT_ENV)
    if not path:
        raise StageA0DownstreamError(f"{A_CHECKPOINT_ENV} is not set")
    return create_serving_runtime(path)


@cache
def _t_runtime():
    path = os.environ.get(T_CHECKPOINT_ENV)
    if not path:
        raise StageA0DownstreamError(f"{T_CHECKPOINT_ENV} is not set")
    return create_serving_runtime(path)


def create_a_anchor_policy():
    """Spawn-safe top-level Policy factory for the frozen A anchor checkpoint."""
    return _a_runtime().create_policy()


def create_t_anchor_policy():
    """Spawn-safe top-level Policy factory for the frozen T anchor checkpoint."""
    return _t_runtime().create_policy()


def _run(plan: SingleRoundEvaluationPlan, workers: int, progress_callback=None):
    if workers == 1:
        return run_single_round_evaluation(plan, progress_callback=progress_callback)
    return run_single_round_evaluation_parallel(
        plan,
        max_workers=workers,
        progress_callback=progress_callback,
    )


def _validate_arm_artifact(
    artifact,
    *,
    candidate_identity: str,
) -> None:
    if artifact.plan.candidate_identity != candidate_identity:
        raise StageA0DownstreamError("downstream candidate identity drifted")
    if artifact.plan.baseline_identity != locked.DOWNSTREAM_OPPONENT_IDENTITY:
        raise StageA0DownstreamError("downstream opponent identity drifted")
    if artifact.plan.seeds != locked.DOWNSTREAM_SEEDS:
        raise StageA0DownstreamError("downstream ordered seeds drifted")
    if artifact.plan.rotation_count != locked.DOWNSTREAM_ROTATIONS:
        raise StageA0DownstreamError("downstream rotation count drifted")
    if artifact.plan.game_mode != locked.DOWNSTREAM_GAME_MODE:
        raise StageA0DownstreamError("downstream game mode drifted")
    if artifact.plan.max_steps != locked.DOWNSTREAM_MAX_STEPS:
        raise StageA0DownstreamError("downstream max_steps drifted")
    if len(artifact.game_results) != locked.DOWNSTREAM_GAMES_PER_ARM:
        raise StageA0DownstreamError("downstream arm game count drifted")
    provenance = artifact.provenance
    if provenance.lisjong_revision != locked.DOWNSTREAM_LISJONG_REVISION:
        raise StageA0DownstreamError("downstream lisjong revision drifted")
    if provenance.lisjong_engine_revision != locked.DOWNSTREAM_LISJONG_ENGINE_REVISION:
        raise StageA0DownstreamError("downstream lisjong-engine revision drifted")
    if provenance.riichienv_version != locked.DOWNSTREAM_RIICHIENV_VERSION:
        raise StageA0DownstreamError("downstream RiichiEnv version drifted")


def evaluate_downstream(
    *,
    gate_path,
    a_checkpoint_path,
    t_checkpoint_path,
    output_root,
    workers: int,
    progress_callback=None,
) -> dict[str, object]:
    """Run exactly 824 A + 824 T games after a locked Gate A0.5 PASS."""
    if type(workers) is not int or workers < 1:
        raise StageA0DownstreamError("workers must be a positive int")
    gate = load_gate(gate_path)
    if gate["classification"]["label"] != GATE_PASS:
        raise StageA0DownstreamError("downstream is forbidden before Gate A0.5 PASS")
    if locked.DOWNSTREAM_STATUS != "A0 DOWNSTREAM ENABLED":
        raise StageA0DownstreamError("the locked downstream status is not ENABLED")

    a_checkpoint = load_checkpoint(a_checkpoint_path)
    t_checkpoint = load_checkpoint(t_checkpoint_path)
    for arm, checkpoint in ((Arm.A, a_checkpoint), (Arm.T, t_checkpoint)):
        if checkpoint.arm is not arm:
            raise StageA0DownstreamError(f"anchor checkpoint is not arm {arm.value}")
        if checkpoint.seed != locked.INTERACTIVE_ANCHOR_SEED:
            raise StageA0DownstreamError("anchor checkpoint training seed drifted")

    root = Path(output_root)
    if root.exists():
        raise FileExistsError("downstream output root already exists")
    root.mkdir(parents=True)

    os.environ[A_CHECKPOINT_ENV] = str(Path(a_checkpoint_path).resolve())
    os.environ[T_CHECKPOINT_ENV] = str(Path(t_checkpoint_path).resolve())
    _a_runtime.cache_clear()
    _t_runtime.cache_clear()

    baseline = POLICY_CATALOG.get(locked.DOWNSTREAM_OPPONENT_IDENTITY)
    if baseline is None or baseline.identity != locked.DOWNSTREAM_OPPONENT_IDENTITY:
        raise StageA0DownstreamError("locked downstream opponent is unavailable")

    a_identity = policy_identity(Arm.A, a_checkpoint.seed, a_checkpoint.identity)
    t_identity = policy_identity(Arm.T, t_checkpoint.seed, t_checkpoint.identity)
    a_plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=a_identity, factory=create_a_anchor_policy),
        baseline=baseline,
        seeds=locked.DOWNSTREAM_SEEDS,
        max_steps=locked.DOWNSTREAM_MAX_STEPS,
    )
    t_plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=t_identity, factory=create_t_anchor_policy),
        baseline=baseline,
        seeds=locked.DOWNSTREAM_SEEDS,
        max_steps=locked.DOWNSTREAM_MAX_STEPS,
    )

    a_result = _run(a_plan, workers, progress_callback=progress_callback)
    a_path = root / "arm-a.json"
    save_single_round_artifact(a_result, a_path)
    a_artifact = load_arm_artifact(a_path)
    _validate_arm_artifact(a_artifact, candidate_identity=a_identity)

    t_result = _run(t_plan, workers, progress_callback=progress_callback)
    t_path = root / "arm-t.json"
    save_single_round_artifact(t_result, t_path)
    t_artifact = load_arm_artifact(t_path)
    _validate_arm_artifact(t_artifact, candidate_identity=t_identity)

    a_blocks = focal_seed_block_means(a_artifact.game_results)
    t_blocks = focal_seed_block_means(t_artifact.game_results)
    # The neutral primitive names its first arm "candidate"; #262 defines
    # D_h = score_T,h - score_A,h, so T is passed first.
    deltas = paired_deltas_from_block_means(t_blocks, a_blocks)
    if tuple(item.seed for item in deltas) != locked.DOWNSTREAM_SEEDS:
        raise StageA0DownstreamError("paired downstream seeds drifted")
    summary = summarize_paired_deltas(deltas)
    if summary.block_count != len(locked.DOWNSTREAM_SEEDS):
        raise StageA0DownstreamError("paired downstream block count drifted")

    if summary.interval_lower > 0:
        classification = DOWNSTREAM_POSITIVE
    elif summary.interval_upper < 0:
        classification = DOWNSTREAM_NEGATIVE
    else:
        classification = DOWNSTREAM_INCONCLUSIVE

    document: dict[str, object] = {
        "lock_b_identity": EXPECTED_LOCK_B_IDENTITY,
        "gate_identity": gate["gate_identity"],
        "status": locked.DOWNSTREAM_STATUS,
        "execution": "RUN",
        "anchor_training_seed": locked.INTERACTIVE_ANCHOR_SEED,
        "a_checkpoint_identity": a_checkpoint.identity,
        "t_checkpoint_identity": t_checkpoint.identity,
        "opponent_identity": locked.DOWNSTREAM_OPPONENT_IDENTITY,
        "opponent_population": locked.DOWNSTREAM_OPPONENT_POPULATION,
        "ordered_seeds": list(locked.DOWNSTREAM_SEEDS),
        "rotation_count": locked.DOWNSTREAM_ROTATIONS,
        "games_per_arm": locked.DOWNSTREAM_GAMES_PER_ARM,
        "total_games": locked.DOWNSTREAM_TOTAL_GAMES,
        "arm_artifacts": {
            "A": {
                "sha256": artifact_file_digest(a_path),
                "candidate_identity": a_identity,
                "mean_focal_score": a_artifact.summary.candidate_metrics.mean_candidate_score,
                "diagnostics": arm_diagnostics(a_artifact),
            },
            "T": {
                "sha256": artifact_file_digest(t_path),
                "candidate_identity": t_identity,
                "mean_focal_score": t_artifact.summary.candidate_metrics.mean_candidate_score,
                "diagnostics": arm_diagnostics(t_artifact),
            },
        },
        "paired": {
            **summary.to_document(),
            "classification": classification,
            "seed_delta_identity": hashlib.sha256(
                canonical_json_text(
                    [item.to_document() for item in deltas]
                ).encode("utf-8")
            ).hexdigest(),
        },
        "protected_test_evaluated": False,
    }
    document["downstream_identity"] = hashlib.sha256(
        canonical_json_text(document).encode("utf-8")
    ).hexdigest()
    write_new_artifact_file(
        root / "downstream-result.json", canonical_json_text(document)
    )
    return document


def load_downstream(path) -> dict[str, object]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise StageA0DownstreamError("downstream result cannot be read") from error
    if canonical_json_text(document) != text:
        raise StageA0DownstreamError("downstream result is not canonical JSON")
    identity = document.get("downstream_identity")
    logical = {
        name: value
        for name, value in document.items()
        if name != "downstream_identity"
    }
    if identity != hashlib.sha256(
        canonical_json_text(logical).encode("utf-8")
    ).hexdigest():
        raise StageA0DownstreamError("downstream result identity mismatch")
    if document.get("protected_test_evaluated") is not False:
        raise StageA0DownstreamError("downstream result claims protected TEST exposure")
    return document


__all__ = [
    "create_a_anchor_policy",
    "create_t_anchor_policy",
    "evaluate_downstream",
    "load_downstream",
]
