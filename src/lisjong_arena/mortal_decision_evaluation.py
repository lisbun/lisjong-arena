"""Mortal driverとsame-state lisjong shadow Policyのserial single-round診断。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lisjong.policy_contract import Policy, Seat

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundGameResult,
    _normalize_seeds,
)
from lisjong_arena.mortal_decision_comparison import (
    MortalDecisionComparisonRecord,
    MortalDecisionComparisonSummary,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.riichienv.mortal_mixed_game_runner import MortalMixedGameRunner


class MortalDecisionEvaluationError(Exception):
    """Mortal same-state diagnosticの1 gameが失敗した場合。"""

    __slots__ = ("rotation", "seed")

    def __init__(self, message: str, *, seed: int, rotation: int) -> None:
        super().__init__(f"seed={seed} rotation={rotation}: {message}")
        self.seed = seed
        self.rotation = rotation


@dataclass(frozen=True, slots=True)
class MortalDecisionEvaluationPlan:
    """selected Policyをactual 3席と独立shadow 1席へfresh生成する条件。"""

    policy: PolicySpec
    seeds: tuple[int, ...]
    mortal_config: MortalDockerConfig
    max_steps: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.policy, PolicySpec):
            raise TypeError("policy must be a PolicySpec")
        if not isinstance(self.mortal_config, MortalDockerConfig):
            raise TypeError("mortal_config must be a MortalDockerConfig")
        if type(self.max_steps) is not int:
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        object.__setattr__(self, "seeds", _normalize_seeds(self.seeds))


@dataclass(frozen=True, slots=True)
class MortalDecisionGameResult:
    """1 rotationのobjective resultとMortal-seat paired decisions。"""

    objective_result: SingleRoundGameResult
    comparisons: tuple[MortalDecisionComparisonRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.objective_result, SingleRoundGameResult):
            raise TypeError("objective_result must be a SingleRoundGameResult")
        try:
            comparisons = tuple(self.comparisons)
        except TypeError:
            raise TypeError("comparisons must be an iterable") from None
        if not comparisons:
            raise ValueError("comparisons must contain at least one paired decision")
        if any(
            not isinstance(item, MortalDecisionComparisonRecord) for item in comparisons
        ):
            raise TypeError(
                "comparisons must contain only MortalDecisionComparisonRecord"
            )
        result = self.objective_result
        for ordinal, comparison in enumerate(comparisons):
            if (
                comparison.seed,
                comparison.rotation,
                comparison.mortal_seat,
                comparison.decision_ordinal,
            ) != (result.seed, result.rotation, result.candidate_seat, ordinal):
                raise ValueError(
                    "comparison identity must match objective result and be contiguous"
                )
        object.__setattr__(self, "comparisons", comparisons)


@dataclass(frozen=True, slots=True)
class MortalDecisionEvaluationResult:
    """canonical seed/rotation orderのgamesと全paired decision summary。"""

    plan: MortalDecisionEvaluationPlan
    game_results: tuple[MortalDecisionGameResult, ...]
    summary: MortalDecisionComparisonSummary

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MortalDecisionEvaluationPlan):
            raise TypeError("plan must be a MortalDecisionEvaluationPlan")
        try:
            game_results = tuple(self.game_results)
        except TypeError:
            raise TypeError("game_results must be an iterable") from None
        if any(not isinstance(item, MortalDecisionGameResult) for item in game_results):
            raise TypeError("game_results must contain only MortalDecisionGameResult")
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
        for game, (seed, rotation) in zip(game_results, expected_order):
            result = game.objective_result
            if (result.seed, result.rotation) != (seed, rotation):
                raise ValueError(
                    "game_results must be ordered by plan.seeds then rotation 0..3"
                )
            if result.candidate_seat != Seat(rotation):
                raise ValueError("objective candidate_seat must equal Seat(rotation)")
            if result.game_mode != SINGLE_ROUND_GAME_MODE:
                raise ValueError(
                    f"objective results must use {SINGLE_ROUND_GAME_MODE!r}"
                )
            if any(
                comparison.shadow_policy_identity != self.plan.policy.identity
                for comparison in game.comparisons
            ):
                raise ValueError(
                    "comparison shadow identity must match plan.policy.identity"
                )
        if not isinstance(self.summary, MortalDecisionComparisonSummary):
            raise TypeError("summary must be a MortalDecisionComparisonSummary")
        expected_records = tuple(
            comparison for game in game_results for comparison in game.comparisons
        )
        if self.summary.records != expected_records:
            raise ValueError("summary records must equal canonical game comparisons")
        object.__setattr__(self, "game_results", game_results)


def _create_policy_runtimes(
    plan: MortalDecisionEvaluationPlan,
    *,
    mortal_seat: Seat,
    seed: int,
    rotation: int,
) -> tuple[dict[Seat, Policy], Policy]:
    policies: dict[Seat, Policy] = {}
    for seat in Seat:
        if seat == mortal_seat:
            continue
        try:
            policies[seat] = plan.policy.factory()
        except Exception as exc:
            raise MortalDecisionEvaluationError(
                f"actual baseline policy factory failed at {seat!r}",
                seed=seed,
                rotation=rotation,
            ) from exc
    try:
        shadow_policy = plan.policy.factory()
    except Exception as exc:
        raise MortalDecisionEvaluationError(
            "shadow policy factory failed", seed=seed, rotation=rotation
        ) from exc
    instances = (*policies.values(), shadow_policy)
    if len({id(instance) for instance in instances}) != len(instances):
        raise MortalDecisionEvaluationError(
            "policy factory must return independent actual-seat and shadow instances",
            seed=seed,
            rotation=rotation,
        )
    return policies, shadow_policy


def _run_mortal_decision_game(
    policies: dict[Seat, Policy],
    shadow_policy: Policy,
    *,
    shadow_policy_identity: str,
    mortal_seat: Seat,
    mortal_config: MortalDockerConfig,
    seed: int,
    max_steps: int,
) -> tuple[LocalGameResult, tuple[MortalDecisionComparisonRecord, ...]]:
    runner = MortalMixedGameRunner(
        policies,
        mortal_seat=mortal_seat,
        mortal_config=mortal_config,
        seed=seed,
        game_mode=SINGLE_ROUND_GAME_MODE,
        max_steps=max_steps,
        shadow_policy=shadow_policy,
        shadow_policy_identity=shadow_policy_identity,
    )
    result = runner.run()
    return result, runner.comparison_records()


def _build_objective_result(
    result: LocalGameResult,
    *,
    seed: int,
    rotation: int,
    mortal_seat: Seat,
) -> SingleRoundGameResult:
    if result.seed != seed or result.game_mode != SINGLE_ROUND_GAME_MODE:
        raise MortalDecisionEvaluationError(
            "mixed runner returned a result for different conditions",
            seed=seed,
            rotation=rotation,
        )
    return SingleRoundGameResult(
        seed=seed,
        rotation=rotation,
        game_mode=result.game_mode,
        candidate_seat=mortal_seat,
        scores=result.scores,
        seat_round_stats=result.seat_round_stats,
    )


def run_mortal_decision_evaluation(
    plan: MortalDecisionEvaluationPlan,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> MortalDecisionEvaluationResult:
    """Mortalをdriverとしてseed入力順 -> rotation順にfail closedで診断する。"""
    if not isinstance(plan, MortalDecisionEvaluationPlan):
        raise TypeError("plan must be a MortalDecisionEvaluationPlan")
    total = SINGLE_ROUND_ROTATION_COUNT * len(plan.seeds)
    completed = 0
    games: list[MortalDecisionGameResult] = []
    for seed in plan.seeds:
        for rotation in range(SINGLE_ROUND_ROTATION_COUNT):
            mortal_seat = Seat(rotation)
            policies, shadow_policy = _create_policy_runtimes(
                plan,
                mortal_seat=mortal_seat,
                seed=seed,
                rotation=rotation,
            )
            try:
                local_result, comparisons = _run_mortal_decision_game(
                    policies,
                    shadow_policy,
                    shadow_policy_identity=plan.policy.identity,
                    mortal_seat=mortal_seat,
                    mortal_config=plan.mortal_config,
                    seed=seed,
                    max_steps=plan.max_steps,
                )
                games.append(
                    MortalDecisionGameResult(
                        objective_result=_build_objective_result(
                            local_result,
                            seed=seed,
                            rotation=rotation,
                            mortal_seat=mortal_seat,
                        ),
                        comparisons=comparisons,
                    )
                )
            except MortalDecisionEvaluationError:
                raise
            except Exception as exc:
                raise MortalDecisionEvaluationError(
                    "single game diagnostic failed", seed=seed, rotation=rotation
                ) from exc
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)
    frozen_games = tuple(games)
    summary = MortalDecisionComparisonSummary.from_records(
        comparison for game in frozen_games for comparison in game.comparisons
    )
    return MortalDecisionEvaluationResult(
        plan=plan,
        game_results=frozen_games,
        summary=summary,
    )


__all__ = [
    "MortalDecisionEvaluationError",
    "MortalDecisionEvaluationPlan",
    "MortalDecisionEvaluationResult",
    "MortalDecisionGameResult",
    "run_mortal_decision_evaluation",
]
