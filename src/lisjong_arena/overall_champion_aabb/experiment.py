"""Issue #250 Overall formal event orchestration.

実行substrateは既存のgeneric AABB comparisonをそのままreuseする。

```text
ComparisonPlan
    -> run_comparison() / run_comparison_parallel()
    -> ComparisonResult
    -> save_comparison_artifact()   (bundleのcomparison.json)
```

新しいhalf-game runner、AABB runner、generic evaluation frameworkは作らない。
このmoduleが足すのはOverall固有の薄いcompositionだけである。

participant identityはauto-discoverしない。execution APIはcallerから
``PolicySpec``を受け取り、実行前にlock participant bindingとのexact一致を
fail closedで確認する。generic Champion registryは導入しない。

このmoduleを実行するとreal 400-hanchan evaluationが走る。CIとtestは
``execute``境界を差し替えて実RiichiEnvを起動しない。
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

from .lock import (
    REQUIRED_DESTINATIONS,
    OverallChampionLockError,
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
    OverallChampionProtocolError,
    ParticipantBinding,
    factory_binding_of,
    require_overall_population,
)
from .result import (
    OverallChampionResultError,
    build_overall_result,
    load_bundle_comparison,
    save_overall_result,
    verify_overall_bundle,
)


@dataclass(frozen=True, slots=True)
class OverallEvaluationOutcome:
    """1回のformal Overall eventの成果物。"""

    worker_count: int
    comparison_artifact_path: Path
    overall_result_path: Path
    overall_result: dict[str, object]

    @property
    def classification(self) -> dict[str, object]:
        classification = self.overall_result["classification"]
        assert isinstance(classification, dict)
        return classification


def require_spec_matches_binding(
    spec: object,
    binding: ParticipantBinding,
    *,
    role: str,
) -> PolicySpec:
    """callerが渡した``PolicySpec``がlock participant bindingと一致することを要求する。

    identityだけでなくfactoryのexact ``module:qualname``も照合し、同じ
    identityを名乗る別実装でformal runを開始できないようにする。
    """
    if not isinstance(spec, PolicySpec):
        raise OverallChampionLockError(f"{role} spec must be a PolicySpec")
    if not isinstance(binding, ParticipantBinding):
        raise OverallChampionLockError(f"{role} binding must be a ParticipantBinding")
    if spec.identity != binding.policy_identity:
        raise OverallChampionLockError(
            f"{role} policy identity differs from the pre-execution lock"
        )
    try:
        actual = factory_binding_of(spec.factory)
    except OverallChampionProtocolError as exc:
        raise OverallChampionLockError(str(exc)) from exc
    if actual != binding.factory_binding:
        raise OverallChampionLockError(
            f"{role} policy factory binding differs from the pre-execution lock"
        )
    return spec


def build_comparison_plan(
    *,
    heuristic_spec: PolicySpec,
    learning_spec: PolicySpec,
    seeds: object,
) -> ComparisonPlan:
    """Overall v1条件を固定したgeneric ``ComparisonPlan``を作る。

    ``game_mode``と``max_steps``はprotocol invariantであり、callerから
    変更できない。
    """
    ordered = require_overall_population(seeds)
    return ComparisonPlan(
        policy_a=heuristic_spec,
        policy_b=learning_spec,
        seeds=ordered,
        game_mode=GAME_MODE,
        max_steps=MAX_STEPS,
    )


def default_execute(plan: ComparisonPlan, *, max_workers: int) -> ComparisonResult:
    """既存generic comparisonのserial / parallel pathへ委譲する。"""
    if max_workers == 1:
        return run_comparison(plan)
    return run_comparison_parallel(plan, max_workers=max_workers)


def run_overall_evaluation(
    *,
    lock_path: str | Path,
    heuristic_spec: PolicySpec,
    learning_spec: PolicySpec,
    execute: Callable[..., ComparisonResult] = default_execute,
) -> OverallEvaluationOutcome:
    """lockされたone-shot Overall formal eventを実行し、strict evidenceを残す。"""
    lock = load_lock_document(lock_path)
    live = require_live_execution_target(lock)
    heuristic_binding, learning_binding = locked_participants(lock)
    require_spec_matches_binding(heuristic_spec, heuristic_binding, role="heuristic")
    require_spec_matches_binding(learning_spec, learning_binding, role="learning")
    seeds = locked_seeds(lock)
    worker_count = locked_max_workers(lock)
    destinations = locked_destinations(lock)
    try:
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise OverallChampionLockError(str(exc)) from exc

    plan = build_comparison_plan(
        heuristic_spec=heuristic_spec,
        learning_spec=learning_spec,
        seeds=seeds,
    )
    result = execute(plan, max_workers=worker_count)
    if not isinstance(result, ComparisonResult):
        raise OverallChampionResultError("execution must return a ComparisonResult")
    if len(result.seat_results) != SEAT_RESULT_COUNT:
        raise OverallChampionResultError(
            f"a formal Overall event must produce exactly {HANCHAN_COUNT} hanchan "
            f"({SEAT_RESULT_COUNT} seat-results) but produced "
            f"{len(result.seat_results)} seat-results"
        )

    comparison_path = destinations["comparison_artifact"]
    save_comparison_artifact(result, comparison_path)
    comparison = load_bundle_comparison(comparison_path)
    require_comparison_provenance(lock, comparison.provenance)
    if comparison.provenance.lisjong_revision != live.lisjong_revision:
        raise OverallChampionResultError(
            "comparison artifact lisjong revision differs from the live "
            "execution target"
        )

    document = build_overall_result(
        lock_document=lock,
        comparison_artifact=comparison,
        comparison_artifact_path=comparison_path,
    )
    result_path = destinations["overall_result"]
    save_overall_result(document, result_path)
    verified = verify_overall_bundle(
        lock_path=lock_path,
        comparison_path=comparison_path,
        result_path=result_path,
    )
    return OverallEvaluationOutcome(
        worker_count=worker_count,
        comparison_artifact_path=comparison_path,
        overall_result_path=result_path,
        overall_result=verified,
    )


__all__ = [
    "OverallEvaluationOutcome",
    "build_comparison_plan",
    "default_execute",
    "require_spec_matches_binding",
    "run_overall_evaluation",
]
