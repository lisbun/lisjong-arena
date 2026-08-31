"""ABBB strength artifact testが共有するfixture。

実RiichiEnvを起動せず、既存``SingleRoundEvaluationResult``契約を満たす
raw game resultsだけを組み立てる。provenanceはinstall metadataへ依存させず
固定値をstubする。
"""

from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.model import (
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    SingleRoundStrengthSummary,
    aggregate_candidate_metrics,
    summarize_single_round_strength,
)

ARENA_REVISION = "2672cc24b90712e98d863727e3bb55785035c35b"
LISJONG_REVISION = "b11841e287e8f11d55fe0fdaa5127ad16e00aa01"
LISJONG_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
CANDIDATE = "yakuhai-call"
BASELINE = "combined"


def game_scores(seed: int, rotation: int) -> tuple[int, int, int, int]:
    """seed / rotationごとに異なる、合計100,000の決定的なfinal scores。"""
    candidate_score = 30_000 + 1_000 * (seed % 5) + 100 * rotation
    others = (100_000 - candidate_score) // 3
    remainder = (100_000 - candidate_score) - 2 * others
    scores = []
    baseline_scores = [others, others, remainder]
    for seat in range(4):
        if seat == rotation:
            scores.append(candidate_score)
        else:
            scores.append(baseline_scores.pop())
    return tuple(scores)


def game_results(seeds: tuple[int, ...]) -> tuple[SingleRoundGameResult, ...]:
    return tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=game_scores(seed, rotation),
            seat_round_stats=neutral_seat_round_stats_tuple(
                game_scores(seed, rotation)
            ),
        )
        for seed in seeds
        for rotation in range(ROTATION_COUNT)
    )


def evaluation_result(
    seeds: tuple[int, ...] = (20_200, 20_201),
    *,
    candidate: str = CANDIDATE,
    baseline: str = BASELINE,
    max_steps: int = 10_000,
) -> SingleRoundEvaluationResult:
    plan = SingleRoundEvaluationPlan(
        candidate=POLICY_CATALOG[candidate],
        baseline=POLICY_CATALOG[baseline],
        seeds=seeds,
        max_steps=max_steps,
    )
    results = game_results(seeds)
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(candidate, results),
    )


def canonical_summary(
    results: tuple[SingleRoundGameResult, ...],
    *,
    candidate: str = CANDIDATE,
) -> SingleRoundStrengthSummary:
    """raw game resultsだけから再集計したcanonical strength summary。"""
    return summarize_single_round_strength(
        aggregate_candidate_metrics(candidate, results), results
    )


def provenance(
    *,
    lisjong_arena_revision: str = ARENA_REVISION,
    lisjong_version: str = "0.1.0",
    lisjong_revision: str = LISJONG_REVISION,
) -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=lisjong_arena_revision,
        lisjong_version=lisjong_version,
        lisjong_revision=lisjong_revision,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=LISJONG_ENGINE_REVISION,
        riichienv_version="0.4.8",
        python_version="3.14.0",
    )


def save(
    result: SingleRoundEvaluationResult,
    path: Path,
    *,
    execution_provenance: SingleRoundExecutionProvenance | None = None,
) -> None:
    """install metadataへ依存せず、固定provenanceでartifactを保存する。"""
    with mock.patch(
        "lisjong_arena.single_round_artifact._collect_execution_provenance",
        return_value=provenance()
        if execution_provenance is None
        else execution_provenance,
    ):
        save_single_round_artifact(result, path)
