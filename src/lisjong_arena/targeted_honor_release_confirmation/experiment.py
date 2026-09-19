"""Issue #270 post-merge independent confirmation orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.model import SingleRoundEvaluationPlan, SingleRoundEvaluationResult
from lisjong_arena.progression_development.paired import load_arm_artifact
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)
from lisjong_arena.targeted_honor_release_development.candidate_arm import (
    CandidateArmDiagnosticResult,
    run_candidate_arm_parallel,
)

from .lock import (
    load_lock_document,
    locked_confirmation_seeds,
    locked_destinations,
    locked_max_workers,
    require_live_execution_target,
)
from .paired import (
    TargetedHonorReleaseConfirmationPairedError,
    build_classified_result,
    build_paired_result,
    save_classified_result,
    save_paired_result,
    verify_classified_result,
    verify_paired_result,
)
from .protocol import (
    GAMES_PER_ARM,
    MAX_STEPS,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_confirmation_population,
    require_exact_candidate_semantics,
)
from .trace import build_trace_artifact, save_trace_artifact, verify_trace_artifact


@dataclass(frozen=True, slots=True)
class ConfirmationOutcome:
    worker_count: int
    candidate_artifact_path: Path
    parent_artifact_path: Path
    candidate_trace_path: Path
    paired_result_path: Path
    classified_result_path: Path
    paired_result: dict[str, object]
    classified_result: dict[str, object]


def build_arm_plan(
    *, candidate_arm: bool, seeds: tuple[int, ...]
) -> SingleRoundEvaluationPlan:
    locked = require_confirmation_population(seeds)
    return SingleRoundEvaluationPlan(
        candidate=candidate_spec() if candidate_arm else parent_spec(),
        baseline=comparator_spec(),
        seeds=locked,
        max_steps=MAX_STEPS,
    )


def default_execute(
    plan: SingleRoundEvaluationPlan, *, max_workers: int
) -> SingleRoundEvaluationResult:
    if max_workers == 1:
        return run_single_round_evaluation(plan)
    return run_single_round_evaluation_parallel(plan, max_workers=max_workers)


def _run_parent_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    worker_count: int,
    destination: Path,
    expected_provenance: SingleRoundExecutionProvenance,
    execute: Callable[..., SingleRoundEvaluationResult],
) -> SingleRoundStrengthArtifact:
    result = execute(plan, max_workers=worker_count)
    if len(result.game_results) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"C arm must produce exactly {GAMES_PER_ARM} games"
        )
    save_single_round_artifact(result, destination)
    artifact = load_arm_artifact(destination)
    if artifact.provenance != expected_provenance:
        raise TargetedHonorReleaseConfirmationPairedError(
            "C arm provenance differs from locked live execution target"
        )
    return artifact


def _run_candidate_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    worker_count: int,
    destination: Path,
    expected_provenance: SingleRoundExecutionProvenance,
    execute: Callable[..., CandidateArmDiagnosticResult],
) -> tuple[SingleRoundStrengthArtifact, CandidateArmDiagnosticResult]:
    result = execute(plan, max_workers=worker_count)
    if len(result.evaluation_result.game_results) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"H arm must produce exactly {GAMES_PER_ARM} games"
        )
    if len(result.game_diagnostics) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationPairedError(
            f"H arm must produce exactly {GAMES_PER_ARM} trace records"
        )
    save_single_round_artifact(result.evaluation_result, destination)
    artifact = load_arm_artifact(destination)
    if artifact.provenance != expected_provenance:
        raise TargetedHonorReleaseConfirmationPairedError(
            "H arm provenance differs from locked live execution target"
        )
    return artifact, result


def run_confirmation(
    *,
    lock_path: str | Path,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
    candidate_execute: Callable[
        ..., CandidateArmDiagnosticResult
    ] = run_candidate_arm_parallel,
) -> ConfirmationOutcome:
    """Run the one-shot locked H/C confirmation and persist strict evidence."""
    require_exact_candidate_semantics()
    lock = load_lock_document(lock_path)
    live = require_live_execution_target(lock)
    seeds = locked_confirmation_seeds(lock)
    worker_count = locked_max_workers(lock)
    destinations = locked_destinations(lock)
    require_new_artifact_destinations(
        destinations,
        required_names=(
            "candidate_artifact",
            "parent_artifact",
            "candidate_trace",
            "paired_result",
            "classified_result",
        ),
    )

    candidate, candidate_diagnostics = _run_candidate_arm(
        build_arm_plan(candidate_arm=True, seeds=seeds),
        worker_count=worker_count,
        destination=destinations["candidate_artifact"],
        expected_provenance=live,
        execute=candidate_execute,
    )
    trace = build_trace_artifact(
        games=candidate_diagnostics.game_diagnostics,
        aggregate=candidate_diagnostics.aggregate,
        candidate_artifact=candidate,
        candidate_artifact_path=destinations["candidate_artifact"],
        seeds=seeds,
        worker_count=worker_count,
    )
    save_trace_artifact(trace, destinations["candidate_trace"])
    verified_trace = verify_trace_artifact(
        destinations["candidate_trace"],
        candidate_artifact_path=destinations["candidate_artifact"],
    )

    parent = _run_parent_arm(
        build_arm_plan(candidate_arm=False, seeds=seeds),
        worker_count=worker_count,
        destination=destinations["parent_artifact"],
        expected_provenance=live,
        execute=execute,
    )
    paired = build_paired_result(
        candidate_artifact=candidate,
        candidate_artifact_path=destinations["candidate_artifact"],
        parent_artifact=parent,
        parent_artifact_path=destinations["parent_artifact"],
        candidate_trace_artifact=verified_trace,
        candidate_trace_artifact_path=destinations["candidate_trace"],
        seeds=seeds,
        worker_count=worker_count,
    )
    save_paired_result(paired, destinations["paired_result"])
    verified_paired = verify_paired_result(
        destinations["paired_result"],
        candidate_artifact_path=destinations["candidate_artifact"],
        parent_artifact_path=destinations["parent_artifact"],
        candidate_trace_artifact_path=destinations["candidate_trace"],
    )
    classified = build_classified_result(
        paired_result=verified_paired,
        paired_result_path=destinations["paired_result"],
    )
    save_classified_result(classified, destinations["classified_result"])
    verified_classified = verify_classified_result(
        destinations["classified_result"],
        paired_result_path=destinations["paired_result"],
    )
    return ConfirmationOutcome(
        worker_count=worker_count,
        candidate_artifact_path=destinations["candidate_artifact"],
        parent_artifact_path=destinations["parent_artifact"],
        candidate_trace_path=destinations["candidate_trace"],
        paired_result_path=destinations["paired_result"],
        classified_result_path=destinations["classified_result"],
        paired_result=verified_paired,
        classified_result=verified_classified,
    )


__all__ = [
    "ConfirmationOutcome",
    "build_arm_plan",
    "default_execute",
    "run_confirmation",
]
