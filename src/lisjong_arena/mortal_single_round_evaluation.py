"""Mortal 1体 + TwoStep Policy 3体のfixed-seed single-round評価。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lisjong.policy_contract import Policy, Seat

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundCandidateMetrics,
    SingleRoundGameResult,
    _normalize_seeds,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.riichienv.mortal_mixed_game_runner import MortalMixedGameRunner
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics

MORTAL_IDENTITY = "mortal"


class MortalSingleRoundEvaluationError(Exception):
    """Mortal single-round評価の1 gameが失敗した場合。"""

    __slots__ = ("rotation", "seed")

    def __init__(self, message: str, *, seed: int, rotation: int) -> None:
        super().__init__(f"seed={seed} rotation={rotation}: {message}")
        self.seed = seed
        self.rotation = rotation


@dataclass(frozen=True, slots=True)
class MortalSingleRoundEvaluationPlan:
    """Mortal-vs-Three-TwoStepの4 seat rotation条件。"""

    baseline: PolicySpec
    seeds: tuple[int, ...]
    mortal_config: MortalDockerConfig
    max_steps: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.baseline, PolicySpec):
            raise TypeError("baseline must be a PolicySpec")
        if self.baseline.identity != "two-step":
            raise ValueError("Mortal baseline must be the two-step Policy")
        if not isinstance(self.mortal_config, MortalDockerConfig):
            raise TypeError("mortal_config must be a MortalDockerConfig")
        if type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class MortalSingleRoundEvaluationResult:
    """Mortal protocolの条件、canonical raw games、既存candidate metrics。"""

    plan: MortalSingleRoundEvaluationPlan
    game_results: tuple[SingleRoundGameResult, ...]
    candidate_metrics: SingleRoundCandidateMetrics

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MortalSingleRoundEvaluationPlan):
            raise TypeError("plan must be a MortalSingleRoundEvaluationPlan")
        if isinstance(self.game_results, (str, bytes, bytearray)):
            raise TypeError("game_results must be an ordered collection")
        try:
            game_results = tuple(self.game_results)
        except TypeError:
            raise TypeError("game_results must be an ordered collection") from None
        if any(not isinstance(item, SingleRoundGameResult) for item in game_results):
            raise TypeError("game_results must contain only SingleRoundGameResult")
        object.__setattr__(self, "game_results", game_results)

        expected_count = SINGLE_ROUND_ROTATION_COUNT * len(self.plan.seeds)
        if len(game_results) != expected_count:
            raise ValueError(
                f"game_results must contain exactly {expected_count} records"
            )
        expected_order = (
            (seed, rotation)
            for seed in self.plan.seeds
            for rotation in range(SINGLE_ROUND_ROTATION_COUNT)
        )
        for game_result, (seed, rotation) in zip(game_results, expected_order):
            if (game_result.seed, game_result.rotation) != (seed, rotation):
                raise ValueError(
                    "game_results must be ordered by plan.seeds then rotation 0..3"
                )
            if game_result.candidate_seat != Seat(rotation):
                raise ValueError(
                    "game_results candidate_seat must equal Seat(rotation)"
                )
            if game_result.game_mode != SINGLE_ROUND_GAME_MODE:
                raise ValueError(f"game_results must use {SINGLE_ROUND_GAME_MODE!r}")

        if not isinstance(self.candidate_metrics, SingleRoundCandidateMetrics):
            raise TypeError("candidate_metrics must be a SingleRoundCandidateMetrics")
        if self.candidate_metrics.candidate_identity != MORTAL_IDENTITY:
            raise ValueError("candidate_metrics.candidate_identity must be 'mortal'")
        if self.candidate_metrics.game_count != expected_count:
            raise ValueError(
                f"candidate_metrics.game_count must equal {expected_count}"
            )


def _create_baseline_policies(
    plan: MortalSingleRoundEvaluationPlan,
    *,
    mortal_seat: Seat,
    seed: int,
    rotation: int,
) -> dict[Seat, Policy]:
    policies: dict[Seat, Policy] = {}
    for seat in Seat:
        if seat == mortal_seat:
            continue
        try:
            policies[seat] = plan.baseline.factory()
        except Exception as exc:
            raise MortalSingleRoundEvaluationError(
                f"baseline policy factory failed at {seat!r}",
                seed=seed,
                rotation=rotation,
            ) from exc
    return policies


def _run_mortal_single_game(
    policies: dict[Seat, Policy],
    *,
    mortal_seat: Seat,
    mortal_config: MortalDockerConfig,
    seed: int,
    max_steps: int,
) -> LocalGameResult:
    return MortalMixedGameRunner(
        policies,
        mortal_seat=mortal_seat,
        mortal_config=mortal_config,
        seed=seed,
        game_mode=SINGLE_ROUND_GAME_MODE,
        max_steps=max_steps,
    ).run()


def _build_game_result(
    result: LocalGameResult,
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
) -> SingleRoundGameResult:
    if result.seed != seed or result.game_mode != SINGLE_ROUND_GAME_MODE:
        raise MortalSingleRoundEvaluationError(
            "mixed runner returned a result for different conditions "
            f"(seed={result.seed!r}, game_mode={result.game_mode!r})",
            seed=seed,
            rotation=rotation,
        )
    return SingleRoundGameResult(
        seed=seed,
        rotation=rotation,
        game_mode=result.game_mode,
        candidate_seat=candidate_seat,
        scores=result.scores,
        seat_round_stats=result.seat_round_stats,
    )


def run_mortal_single_round_evaluation(
    plan: MortalSingleRoundEvaluationPlan,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> MortalSingleRoundEvaluationResult:
    """seed入力順 -> Mortal seat 0..3順でserial実行し、1失敗で全体を失敗する。"""
    if not isinstance(plan, MortalSingleRoundEvaluationPlan):
        raise TypeError("plan must be a MortalSingleRoundEvaluationPlan")

    total = SINGLE_ROUND_ROTATION_COUNT * len(plan.seeds)
    completed = 0
    game_results: list[SingleRoundGameResult] = []
    for seed in plan.seeds:
        for rotation in range(SINGLE_ROUND_ROTATION_COUNT):
            mortal_seat = Seat(rotation)
            policies = _create_baseline_policies(
                plan,
                mortal_seat=mortal_seat,
                seed=seed,
                rotation=rotation,
            )
            try:
                result = _run_mortal_single_game(
                    policies,
                    mortal_seat=mortal_seat,
                    mortal_config=plan.mortal_config,
                    seed=seed,
                    max_steps=plan.max_steps,
                )
            except Exception as exc:
                raise MortalSingleRoundEvaluationError(
                    "single game execution failed",
                    seed=seed,
                    rotation=rotation,
                ) from exc
            game_results.append(
                _build_game_result(
                    result,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=mortal_seat,
                )
            )
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

    frozen_results = tuple(game_results)
    return MortalSingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(MORTAL_IDENTITY, frozen_results),
    )


__all__ = [
    "MORTAL_IDENTITY",
    "MortalSingleRoundEvaluationError",
    "MortalSingleRoundEvaluationPlan",
    "MortalSingleRoundEvaluationResult",
    "run_mortal_single_round_evaluation",
]
