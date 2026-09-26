"""Issue #389 — focal Policy 1 armの実行。

既存ABBB single-round pathをそのまま使う。seat assignment、Policy生成、
raw ``SingleRoundGameResult``構築、aggregationは``single_round_evaluation``の
既存helperを再利用し、並列実行は既存``run_game_jobs``へbenchmark専用の
top-level workerを渡すだけである（Issue #196以降の既存precedentと同じ形）。

benchmark workerが既存pathと異なるのは、``LocalGameRunner``の``trace_sink``へ
``OffenseFactsCollector``を接続する1点だけである。``LocalGameRunner``、
``RoundStatsCollector``、``SeatRoundStats``、single-round artifact v1は変更しない。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lisjong.policy_contract import Policy, Seat

from lisjong_arena._parallel_execution import (
    GameJob,
    GameJobOutcome,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    SingleRoundEvaluationError,
    _build_game_result,
    _create_policies,
    _seat_assignment,
    aggregate_candidate_metrics,
)

from .protocol import GAME_MODE, MAX_STEPS, OPPONENT_IDENTITY, opponent_spec
from .record import (
    KyokuOffenseFacts,
    KyokuOffenseRecord,
    OffenseFactsCollector,
    validate_record_against_game_result,
)


class PureOffenseExecutionError(RuntimeError):
    """benchmark workerが不完全または予期しないoutcomeを返した場合。"""


@dataclass(frozen=True, slots=True)
class BenchmarkArmResult:
    """1 focal Policy armの成功した実行結果。

    ``evaluation``は既存ABBB ``SingleRoundEvaluationResult``そのものであり、
    ``offense_records``は同じ順序（seed入力順 -> rotation 0..3）の
    benchmark-owned recordである。
    """

    evaluation: SingleRoundEvaluationResult
    offense_records: tuple[KyokuOffenseRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation, SingleRoundEvaluationResult):
            raise TypeError("evaluation must be a SingleRoundEvaluationResult")
        records = tuple(self.offense_records)
        if len(records) != len(self.evaluation.game_results):
            raise PureOffenseExecutionError(
                "offense records must cover every game result exactly once"
            )
        for record, game_result in zip(
            records, self.evaluation.game_results, strict=True
        ):
            validate_record_against_game_result(record, game_result)
        object.__setattr__(self, "offense_records", records)


@dataclass(frozen=True, slots=True)
class _BenchmarkGameJobOutcome(GameJobOutcome):
    facts: KyokuOffenseFacts | None


def _run_benchmark_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    max_steps: int,
) -> tuple[LocalGameResult, KyokuOffenseFacts]:
    """1 gameを既存``LocalGameRunner``で実行し、offense factも回収する。

    unit testはこの関数を差し替えて実RiichiEnvを起動せずに検証する。
    """
    collector = OffenseFactsCollector()
    result = LocalGameRunner(
        policies,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
        trace_sink=collector,
    ).run()
    return result, collector.facts()


def _run_benchmark_game_job(job: GameJob) -> _BenchmarkGameJobOutcome:
    """spawn worker内部でfresh Policyを生成して1 gameを実行する。"""
    try:
        policies = _create_policies(
            job.assignment, seed=job.seed, rotation=job.rotation
        )
        result, facts = _run_benchmark_game(
            policies, seed=job.seed, max_steps=job.max_steps
        )
    except Exception:
        return _BenchmarkGameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=f"benchmark game failed:\n{traceback.format_exc()}",
            facts=None,
        )
    return _BenchmarkGameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        facts=facts,
    )


def _record(
    result: LocalGameResult,
    facts: KyokuOffenseFacts,
    *,
    seed: int,
    rotation: int,
) -> tuple[SingleRoundGameResult, KyokuOffenseRecord]:
    game_result = _build_game_result(
        result, seed=seed, rotation=rotation, candidate_seat=Seat(rotation)
    )
    record = KyokuOffenseRecord(
        seed=seed, rotation=rotation, focal_seat=Seat(rotation), facts=facts
    )
    try:
        validate_record_against_game_result(record, game_result)
    except ValueError as exc:
        raise SingleRoundEvaluationError(
            f"offense record is inconsistent: {exc}", seed=seed, rotation=rotation
        ) from exc
    return game_result, record


def benchmark_plan(
    focal: PolicySpec, seeds: Sequence[int]
) -> SingleRoundEvaluationPlan:
    """focal vs passive tsumogiri x3の既存ABBB planを作る。"""
    if not isinstance(focal, PolicySpec):
        raise TypeError("focal must be a PolicySpec")
    if focal.identity == OPPONENT_IDENTITY:
        raise ValueError("the focal Policy must not be the passive opponent")
    return SingleRoundEvaluationPlan(
        candidate=focal,
        baseline=opponent_spec(),
        seeds=tuple(seeds),
        max_steps=MAX_STEPS,
    )


def run_benchmark_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> BenchmarkArmResult:
    """1 armを実行する。``max_workers == 1``はin-process serial実行。

    実行順序・raw result順序は``seed入力順 -> rotation 0..3``へcanonicalizeする。
    1 gameでも失敗した場合はpartial resultを返さず``SingleRoundEvaluationError``
    を送出する。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")
    if plan.baseline.identity != OPPONENT_IDENTITY:
        raise ValueError("benchmark plan must use the passive tsumogiri opponent")
    if plan.max_steps != MAX_STEPS:
        raise ValueError(f"benchmark plan must use max_steps={MAX_STEPS}")
    validate_max_workers(max_workers)

    total = ROTATION_COUNT * len(plan.seeds)
    game_results: list[SingleRoundGameResult] = []
    records: list[KyokuOffenseRecord] = []

    if max_workers == 1:
        for seed in plan.seeds:
            for rotation in range(ROTATION_COUNT):
                policies = _create_policies(
                    _seat_assignment(plan, rotation), seed=seed, rotation=rotation
                )
                try:
                    result, facts = _run_benchmark_game(
                        policies, seed=seed, max_steps=plan.max_steps
                    )
                except Exception as exc:
                    raise SingleRoundEvaluationError(
                        "single game execution failed", seed=seed, rotation=rotation
                    ) from exc
                game_result, record = _record(
                    result, facts, seed=seed, rotation=rotation
                )
                game_results.append(game_result)
                records.append(record)
                if progress_callback is not None:
                    progress_callback(len(game_results), total)
    else:
        check_policy_spec_serializable(plan.candidate)
        check_policy_spec_serializable(plan.baseline)
        jobs = [
            GameJob(
                seed=seed,
                rotation=rotation,
                assignment=_seat_assignment(plan, rotation),
                game_mode=GAME_MODE,
                max_steps=plan.max_steps,
            )
            for seed in plan.seeds
            for rotation in range(ROTATION_COUNT)
        ]
        outcomes = run_game_jobs(
            jobs,
            max_workers=max_workers,
            game_runner=_run_benchmark_game_job,
            progress_callback=progress_callback,
        )
        for seed in plan.seeds:
            for rotation in range(ROTATION_COUNT):
                outcome = outcomes[(seed, rotation)]
                if outcome.error_text is not None:
                    raise SingleRoundEvaluationError(
                        "single game execution failed in a worker process",
                        seed=seed,
                        rotation=rotation,
                    ) from RuntimeError(outcome.error_text)
                if not isinstance(outcome, _BenchmarkGameJobOutcome):
                    raise PureOffenseExecutionError(
                        "worker returned an unexpected outcome type"
                    )
                if outcome.result is None or outcome.facts is None:
                    raise PureOffenseExecutionError(
                        "worker returned incomplete success"
                    )
                game_result, record = _record(
                    outcome.result, outcome.facts, seed=seed, rotation=rotation
                )
                game_results.append(game_result)
                records.append(record)

    frozen_results = tuple(game_results)
    if len(frozen_results) != total:
        raise PureOffenseExecutionError(
            f"expected {total} games but produced {len(frozen_results)}"
        )
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_results
        ),
    )
    return BenchmarkArmResult(evaluation=evaluation, offense_records=tuple(records))


__all__ = [
    "BenchmarkArmResult",
    "PureOffenseExecutionError",
    "benchmark_plan",
    "run_benchmark_arm",
]
