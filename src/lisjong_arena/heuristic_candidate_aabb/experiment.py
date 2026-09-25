"""Issue #375 formal event orchestration.

既存generic AABB comparison(``ComparisonPlan`` -> ``run_comparison(_parallel)``
-> ``save_comparison_artifact``)をそのままreuseし、protocol固有の薄い
compositionだけを足す。CIとtestは``execute``境界を差し替えて実RiichiEnvを
起動しない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.artifact import save_comparison_artifact
from lisjong_arena.comparison import run_comparison, run_comparison_parallel
from lisjong_arena.model import ComparisonPlan, ComparisonResult, PolicySpec
from lisjong_arena.overall_champion_aabb.protocol import (
    OverallChampionProtocolError,
    ParticipantBinding,
    factory_binding_of,
)

from .lock import (
    REQUIRED_DESTINATIONS,
    HeuristicCandidateLockError,
    load_lock_document,
    locked_destinations,
    locked_max_workers,
    locked_participants,
    locked_seeds,
    require_comparison_provenance,
    require_live_execution_target,
)
from .protocol import (
    GAME_MODE,
    HANCHAN_COUNT,
    MAX_STEPS,
    SEAT_RESULT_COUNT,
    require_population,
)
from .result import (
    HeuristicCandidateResultError,
    build_candidate_result,
    load_bundle_comparison,
    save_candidate_result,
    verify_candidate_bundle,
)


@dataclass(frozen=True, slots=True)
class CandidateEvaluationOutcome:
    worker_count: int
    comparison_artifact_path: Path
    candidate_result_path: Path
    candidate_result: dict[str, object]


def require_spec_matches_binding(
    spec: object, binding: ParticipantBinding, *, role: str
) -> PolicySpec:
    """callerの``PolicySpec``がlock bindingのidentity / exact factoryと一致すること。"""
    if not isinstance(spec, PolicySpec):
        raise HeuristicCandidateLockError(f"{role} spec must be a PolicySpec")
    if spec.identity != binding.policy_identity:
        raise HeuristicCandidateLockError(
            f"{role} policy identity differs from the pre-execution lock"
        )
    try:
        actual = factory_binding_of(spec.factory)
    except OverallChampionProtocolError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    if actual != binding.factory_binding:
        raise HeuristicCandidateLockError(
            f"{role} policy factory binding differs from the pre-execution lock"
        )
    return spec


def build_comparison_plan(
    *, candidate_spec: PolicySpec, incumbent_spec: PolicySpec, seeds: object
) -> ComparisonPlan:
    """v1条件(game mode / max_steps)を固定したgeneric ``ComparisonPlan``。"""
    return ComparisonPlan(
        policy_a=candidate_spec,
        policy_b=incumbent_spec,
        seeds=require_population(seeds),
        game_mode=GAME_MODE,
        max_steps=MAX_STEPS,
    )


def default_execute(
    plan: ComparisonPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ComparisonResult:
    if max_workers == 1:
        return run_comparison(plan, progress_callback=progress_callback)
    return run_comparison_parallel(
        plan, max_workers=max_workers, progress_callback=progress_callback
    )


def run_candidate_evaluation(
    *,
    lock_path: str | Path,
    candidate_spec: PolicySpec,
    incumbent_spec: PolicySpec,
    seed_ledger: object,
    execute: Callable[..., ComparisonResult] = default_execute,
    progress_callback: Callable[[int, int], None] | None = None,
) -> CandidateEvaluationOutcome:
    """lockされたone-shot formal eventを実行し、strict evidenceを残す。

    live execution targetは実行の前後両方で検証し、後者はcomparison artifactを
    書き出す前に行う。失敗した場合はraw resultを一切採用しない。
    """
    lock = load_lock_document(lock_path)
    live_before = require_live_execution_target(lock, seed_ledger=seed_ledger)
    candidate_binding, incumbent_binding = locked_participants(lock)
    require_spec_matches_binding(candidate_spec, candidate_binding, role="candidate")
    require_spec_matches_binding(incumbent_spec, incumbent_binding, role="incumbent")
    worker_count = locked_max_workers(lock)
    destinations = locked_destinations(lock)
    try:
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc

    plan = build_comparison_plan(
        candidate_spec=candidate_spec,
        incumbent_spec=incumbent_spec,
        seeds=locked_seeds(lock),
    )
    if progress_callback is None:
        result = execute(plan, max_workers=worker_count)
    else:
        result = execute(
            plan, max_workers=worker_count, progress_callback=progress_callback
        )
    if not isinstance(result, ComparisonResult):
        raise HeuristicCandidateResultError("execution must return a ComparisonResult")
    if len(result.seat_results) != SEAT_RESULT_COUNT:
        raise HeuristicCandidateResultError(
            f"a formal event must produce exactly {HANCHAN_COUNT} hanchan "
            f"({SEAT_RESULT_COUNT} seat-results) but produced "
            f"{len(result.seat_results)} seat-results"
        )

    live_after = require_live_execution_target(lock, seed_ledger=seed_ledger)
    if live_after != live_before:
        raise HeuristicCandidateLockError(
            "live execution target changed while the formal event was running"
        )

    comparison_path = destinations["comparison_artifact"]
    save_comparison_artifact(result, comparison_path)
    comparison = load_bundle_comparison(comparison_path)
    require_comparison_provenance(lock, comparison.provenance)
    document = build_candidate_result(
        lock_document=lock,
        comparison_artifact=comparison,
        comparison_artifact_path=comparison_path,
    )
    result_path = destinations["candidate_result"]
    save_candidate_result(document, result_path)
    verified = verify_candidate_bundle(
        lock_path=lock_path, comparison_path=comparison_path, result_path=result_path
    )
    return CandidateEvaluationOutcome(
        worker_count=worker_count,
        comparison_artifact_path=comparison_path,
        candidate_result_path=result_path,
        candidate_result=verified,
    )


__all__ = [
    "CandidateEvaluationOutcome",
    "build_comparison_plan",
    "default_execute",
    "require_spec_matches_binding",
    "run_candidate_evaluation",
]
