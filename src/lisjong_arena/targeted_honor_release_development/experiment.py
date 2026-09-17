"""Issue #263 post-merge Phase-A / Phase-B orchestration."""

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

from .artifact import build_artifact, load_artifact, save_artifact
from .diagnostic import PhaseADiagnosticResult, run_phase_a_parallel
from .lock import (
    load_lock_document,
    locked_max_workers,
    locked_phase_b_seeds,
    require_live_execution_target,
    require_locked_destination,
)
from .paired import (
    TargetedHonorReleasePairedError,
    build_classified_result,
    build_paired_result,
    save_classified_result,
    save_paired_result,
    verify_classified_result,
    verify_paired_result,
)
from .protocol import (
    MAX_STEPS,
    PHASE_B_GAMES_PER_ARM,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_exact_candidate_semantics,
    require_phase_b_population,
)


@dataclass(frozen=True, slots=True)
class PhaseAOutcome:
    result: PhaseADiagnosticResult
    artifact_path: Path
    artifact: dict[str, object]


@dataclass(frozen=True, slots=True)
class PhaseBOutcome:
    worker_count: int
    candidate_artifact_path: Path
    parent_artifact_path: Path
    paired_result_path: Path
    classified_result_path: Path
    paired_result: dict[str, object]
    classified_result: dict[str, object]


def _source_parent_path(lock_document: dict[str, object]) -> Path:
    source = lock_document["source_parent"]
    if not isinstance(source, dict):
        raise TargetedHonorReleasePairedError("lock source_parent is malformed")
    return Path(str(source["artifact_path"]))


def run_phase_a(
    *,
    lock_path: str | Path,
    diagnostic_artifact_path: str | Path,
) -> PhaseAOutcome:
    """Run exact #252 parent replay and persist the #174 diagnostic once."""
    require_exact_candidate_semantics()
    lock = load_lock_document(lock_path)
    live = require_live_execution_target(lock)
    destination = require_locked_destination(
        lock, "phase_a_diagnostic", diagnostic_artifact_path
    )
    require_new_artifact_destinations(
        {"phase_a_diagnostic": destination},
        required_names=("phase_a_diagnostic",),
    )
    parent_path = _source_parent_path(lock)
    worker_count = locked_max_workers(lock)
    result = run_phase_a_parallel(
        parent_artifact_path=str(parent_path),
        max_workers=worker_count,
    )
    document = build_artifact(
        phase_a=result,
        parent_artifact_path=parent_path,
        max_workers=worker_count,
        provenance=live,
    )
    save_artifact(document, destination)
    verified = load_artifact(destination, parent_artifact_path=parent_path)
    return PhaseAOutcome(result=result, artifact_path=destination, artifact=verified)


def build_arm_plan(
    *, candidate_arm: bool, seeds: tuple[int, ...]
) -> SingleRoundEvaluationPlan:
    locked = require_phase_b_population(seeds)
    return SingleRoundEvaluationPlan(
        candidate=candidate_spec() if candidate_arm else parent_spec(),
        baseline=comparator_spec(),
        seeds=locked,
        max_steps=MAX_STEPS,
    )


def default_execute(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
) -> SingleRoundEvaluationResult:
    if max_workers == 1:
        return run_single_round_evaluation(plan)
    return run_single_round_evaluation_parallel(plan, max_workers=max_workers)


def _run_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    worker_count: int,
    destination: Path,
    expected_provenance: SingleRoundExecutionProvenance,
    execute: Callable[..., SingleRoundEvaluationResult],
) -> SingleRoundStrengthArtifact:
    result = execute(plan, max_workers=worker_count)
    if len(result.game_results) != PHASE_B_GAMES_PER_ARM:
        raise TargetedHonorReleasePairedError(
            f"arm must produce exactly {PHASE_B_GAMES_PER_ARM} games"
        )
    save_single_round_artifact(result, destination)
    artifact = load_arm_artifact(destination)
    if artifact.provenance != expected_provenance:
        raise TargetedHonorReleasePairedError(
            "arm provenance differs from the locked live execution target"
        )
    return artifact


def _require_phase_a_gate(
    lock_document: dict[str, object],
    *,
    diagnostic_artifact_path: str | Path,
) -> dict[str, object]:
    path = require_locked_destination(
        lock_document, "phase_a_diagnostic", diagnostic_artifact_path
    )
    parent_path = _source_parent_path(lock_document)
    artifact = load_artifact(path, parent_artifact_path=parent_path)
    gate = artifact["gate"]
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        label = gate.get("label") if isinstance(gate, dict) else None
        raise TargetedHonorReleasePairedError(
            f"Phase B is not authorized by the Phase-A gate (label={label!r})"
        )
    return artifact


def run_phase_b(
    *,
    lock_path: str | Path,
    diagnostic_artifact_path: str | Path,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
    paired_result_path: str | Path,
    classified_result_path: str | Path,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
) -> PhaseBOutcome:
    """Run the prelocked fresh H/C arms only after a passing Phase-A gate."""
    require_exact_candidate_semantics()
    lock = load_lock_document(lock_path)
    live = require_live_execution_target(lock)
    _require_phase_a_gate(lock, diagnostic_artifact_path=diagnostic_artifact_path)
    seeds = locked_phase_b_seeds(lock)
    worker_count = locked_max_workers(lock)

    destinations = {
        "candidate_artifact": require_locked_destination(
            lock, "candidate_artifact", candidate_artifact_path
        ),
        "parent_artifact": require_locked_destination(
            lock, "parent_artifact", parent_artifact_path
        ),
        "paired_result": require_locked_destination(
            lock, "paired_result", paired_result_path
        ),
        "classified_result": require_locked_destination(
            lock, "classified_result", classified_result_path
        ),
    }
    require_new_artifact_destinations(
        destinations,
        required_names=(
            "candidate_artifact",
            "parent_artifact",
            "paired_result",
            "classified_result",
        ),
    )

    candidate = _run_arm(
        build_arm_plan(candidate_arm=True, seeds=seeds),
        worker_count=worker_count,
        destination=destinations["candidate_artifact"],
        expected_provenance=live,
        execute=execute,
    )
    parent = _run_arm(
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
        seeds=seeds,
        worker_count=worker_count,
    )
    save_paired_result(paired, destinations["paired_result"])
    verified_paired = verify_paired_result(
        destinations["paired_result"],
        candidate_artifact_path=destinations["candidate_artifact"],
        parent_artifact_path=destinations["parent_artifact"],
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
    return PhaseBOutcome(
        worker_count=worker_count,
        candidate_artifact_path=destinations["candidate_artifact"],
        parent_artifact_path=destinations["parent_artifact"],
        paired_result_path=destinations["paired_result"],
        classified_result_path=destinations["classified_result"],
        paired_result=verified_paired,
        classified_result=verified_classified,
    )


__all__ = [
    "PhaseAOutcome",
    "PhaseBOutcome",
    "build_arm_plan",
    "default_execute",
    "run_phase_a",
    "run_phase_b",
]
