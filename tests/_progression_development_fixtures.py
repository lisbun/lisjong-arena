"""Issue #252 testが共有するsynthetic fixture。

実RiichiEnvを起動せず、locked seedsのreal evaluationも実行しない。raw game
resultは``SingleRoundGameResult``契約を満たす合成値であり、focal seat scoreを
seedごとに指定できるため、paired ``D_s``をtest側でexactに予測できる。
provenanceはinstall metadata / Git HEADへ依存させず固定値をstubする。
"""

from collections.abc import Callable
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.progression_development.protocol import (
    ROTATION_COUNT,
    comparator_spec,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics

ARENA_REVISION = "1" * 40
LISJONG_REVISION = "2" * 40
LISJONG_ENGINE_REVISION = "3" * 40

FocalScore = Callable[[int, int], int]
"""``(seed, rotation) -> focal seat final score``。"""


def provenance(
    *, lisjong_revision: str = LISJONG_REVISION
) -> SingleRoundExecutionProvenance:
    """install metadataへ依存しない固定execution provenance。"""
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=ARENA_REVISION,
        lisjong_version="0.1.0",
        lisjong_revision=lisjong_revision,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=LISJONG_ENGINE_REVISION,
        riichienv_version="0.4.10",
        python_version="3.14.6",
    )


def constant_focal_score(value: int) -> FocalScore:
    """全gameで同じfocal seat scoreを返す。"""

    def focal(seed: int, rotation: int) -> int:
        del seed, rotation
        return value

    return focal


def seed_offset_focal_score(base: int, offset: Callable[[int], int]) -> FocalScore:
    """seedごとに``base + offset(seed)``のfocal scoreを返す(rotation非依存)。"""

    def focal(seed: int, rotation: int) -> int:
        del rotation
        return base + offset(seed)

    return focal


def game_scores(
    seed: int, rotation: int, focal: FocalScore
) -> tuple[int, int, int, int]:
    """focal seatへ指定scoreを置き、残り3 seatで100,000へそろえる。"""
    focal_score = focal(seed, rotation)
    others = (100_000 - focal_score) // 3
    remainder = (100_000 - focal_score) - 2 * others
    baseline_scores = [others, others, remainder]
    scores = []
    for seat in range(4):
        if seat == rotation:
            scores.append(focal_score)
        else:
            scores.append(baseline_scores.pop())
    return tuple(scores)


def game_results(
    seeds: tuple[int, ...], focal: FocalScore
) -> tuple[SingleRoundGameResult, ...]:
    """``seed入力順 -> rotation 0..3``のcanonical raw game results。"""
    return tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=game_scores(seed, rotation, focal),
            seat_round_stats=neutral_seat_round_stats_tuple(
                game_scores(seed, rotation, focal)
            ),
        )
        for seed in seeds
        for rotation in range(ROTATION_COUNT)
    )


def evaluation_result(
    plan: SingleRoundEvaluationPlan, focal: FocalScore
) -> SingleRoundEvaluationResult:
    """planに対応する合成``SingleRoundEvaluationResult``。"""
    results = game_results(plan.seeds, focal)
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(plan.candidate.identity, results),
    )


def arm_plan(
    candidate: PolicySpec, seeds: tuple[int, ...], *, max_steps: int = 10_000
) -> SingleRoundEvaluationPlan:
    """``candidate vs passive comparator x3``のplan。"""
    return SingleRoundEvaluationPlan(
        candidate=candidate,
        baseline=comparator_spec(),
        seeds=seeds,
        max_steps=max_steps,
    )


def save_arm_artifact(
    result: SingleRoundEvaluationResult,
    path: Path,
    *,
    execution_provenance: SingleRoundExecutionProvenance | None = None,
) -> None:
    """Git HEADへ依存せず固定provenanceでarm artifactを保存する。"""
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=(
            provenance() if execution_provenance is None else execution_provenance
        ),
    ):
        save_single_round_artifact(result, path)


def recording_execute(
    focal: FocalScore,
    *,
    failing_workers: frozenset[int] = frozenset(),
    divergent_workers: frozenset[int] = frozenset(),
    seconds_by_worker: dict[int, float] | None = None,
) -> Callable[..., SingleRoundEvaluationResult]:
    """``execute``差し替え用の決定的なfake。

    ``failing_workers``へ含めたworker数では例外を送出し、
    ``divergent_workers``ではserialと異なるraw resultを返す。実ゲームは
    一切実行しない。
    """
    calls: list[int] = []

    def execute(plan, *, max_workers, progress_callback=None):
        calls.append(max_workers)
        if max_workers in failing_workers:
            raise RuntimeError(f"worker pool with {max_workers} workers failed")
        if progress_callback is not None:
            total = ROTATION_COUNT * len(plan.seeds)
            for index in range(total):
                progress_callback(index + 1, total)
        if max_workers in divergent_workers:
            shifted = seed_offset_focal_score(0, lambda seed: focal(seed, 0) + 1)
            return evaluation_result(plan, shifted)
        return evaluation_result(plan, focal)

    execute.calls = calls  # type: ignore[attr-defined]
    execute.seconds_by_worker = seconds_by_worker or {}  # type: ignore[attr-defined]
    return execute


def scripted_clock(seconds_by_call: list[float]) -> Callable[[], float]:
    """``perf_counter``差し替え用の、呼び出し順に値を返す決定的なclock。"""
    values = iter(seconds_by_call)

    def clock() -> float:
        return next(values)

    return clock
