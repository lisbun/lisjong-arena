"""Trace-preserving direct-ABBB execution for Issue #297."""

from __future__ import annotations

import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lisjong.policies.hand_value_tradeoff_targeted_honor_release import (
    HandValueTradeoffTargetedHonorReleaseAnalysis,
)
from lisjong.policy_contract import (
    DecisionContext,
    DecisionTraceRecorder,
    DiscardAction,
    InternalAction,
    Policy,
    Seat,
    execute_policy_with_trace,
)

from lisjong_arena._parallel_execution import (
    GameJob,
    GameJobOutcome,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.model import (
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    SingleRoundEvaluationError,
    _build_game_result,
    _create_policies,
    _seat_assignment,
    aggregate_candidate_metrics,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    ROTATION_COUNT,
    TOTAL_GAMES,
)


class ChampionHandValueV2TraceError(ValueError):
    """Candidate trace execution or aggregation is inconsistent."""


@dataclass(frozen=True, slots=True)
class CompositionDecisionRecord:
    seed: int
    rotation: int
    ordinal: int
    candidate_seat: Seat
    selection_source: str
    champion_activation_stage: str | None
    hand_value_v2_attempted: bool
    action_changed_vs_champion: bool
    action_changed_vs_former_parent: bool


@dataclass(frozen=True, slots=True)
class GameCompositionDiagnostics:
    seed: int
    rotation: int
    candidate_seat: Seat
    focal_decision_count: int
    discard_decision_count: int
    choice_discard_decision_count: int
    forced_discard_decision_count: int
    candidate_runtime_total_seconds: float
    game_wall_clock_seconds: float
    records: tuple[CompositionDecisionRecord, ...]


@dataclass(frozen=True, slots=True)
class CompositionAggregate:
    game_count: int
    focal_decision_count: int
    discard_decision_count: int
    choice_discard_decision_count: int
    forced_discard_decision_count: int
    selection_source_counts: dict[str, int]
    champion_activation_stage_counts: dict[str, int]
    champion_targeted_preserved_count: int
    hand_value_v2_attempted_count: int
    action_changed_vs_champion_count: int
    action_changed_vs_former_parent_count: int
    candidate_runtime_total_seconds: float
    replay_wall_clock_seconds: float


@dataclass(frozen=True, slots=True)
class CandidateScreenResult:
    evaluation_result: SingleRoundEvaluationResult
    game_diagnostics: tuple[GameCompositionDiagnostics, ...]
    aggregate: CompositionAggregate


class _Recorder:
    __slots__ = (
        "seed",
        "rotation",
        "candidate_seat",
        "focal_decision_count",
        "discard_decision_count",
        "choice_discard_decision_count",
        "forced_discard_decision_count",
        "candidate_runtime_total_seconds",
        "records",
    )

    def __init__(self, *, seed: int, rotation: int, candidate_seat: Seat) -> None:
        self.seed = seed
        self.rotation = rotation
        self.candidate_seat = candidate_seat
        self.focal_decision_count = 0
        self.discard_decision_count = 0
        self.choice_discard_decision_count = 0
        self.forced_discard_decision_count = 0
        self.candidate_runtime_total_seconds = 0.0
        self.records: list[CompositionDecisionRecord] = []

    def decide(self, candidate: Policy, decision: DecisionContext) -> InternalAction:
        ordinal = self.focal_decision_count
        self.focal_decision_count += 1

        recorder = DecisionTraceRecorder()
        started = time.perf_counter()
        selected = execute_policy_with_trace(candidate, decision, recorder)
        elapsed = float(time.perf_counter() - started)
        self.candidate_runtime_total_seconds += elapsed

        traces = recorder.snapshot()
        if len(traces) != 1:
            raise ChampionHandValueV2TraceError(
                "candidate traced execution must emit exactly one decision trace"
            )
        trace = traces[0]
        if trace.selected_action != selected:
            raise ChampionHandValueV2TraceError(
                "candidate trace selected action differs from executed action"
            )

        if not isinstance(selected, DiscardAction):
            return selected

        discard_actions = tuple(
            action
            for action in decision.legal_actions
            if isinstance(action, DiscardAction)
        )
        if selected not in discard_actions:
            raise ChampionHandValueV2TraceError(
                "candidate selected discard is not a legal discard"
            )
        self.discard_decision_count += 1
        if len(discard_actions) == 1:
            self.forced_discard_decision_count += 1
            return selected

        self.choice_discard_decision_count += 1
        analysis = trace.analysis
        if not isinstance(analysis, HandValueTradeoffTargetedHonorReleaseAnalysis):
            raise ChampionHandValueV2TraceError(
                "candidate choice discard did not expose composition analysis"
            )
        if analysis.selected_action != selected:
            raise ChampionHandValueV2TraceError(
                "composition analysis selected action differs from executed action"
            )

        champion_stage = (
            None
            if analysis.champion_analysis is None
            else analysis.champion_analysis.activation_stage.name
        )
        self.records.append(
            CompositionDecisionRecord(
                seed=self.seed,
                rotation=self.rotation,
                ordinal=ordinal,
                candidate_seat=self.candidate_seat,
                selection_source=analysis.selection_source.name,
                champion_activation_stage=champion_stage,
                hand_value_v2_attempted=analysis.hand_value_v2_action is not None,
                action_changed_vs_champion=analysis.action_changed_vs_champion,
                action_changed_vs_former_parent=(
                    analysis.action_changed_vs_former_parent
                ),
            )
        )
        return selected

    def snapshot(self, game_wall_clock_seconds: float) -> GameCompositionDiagnostics:
        return GameCompositionDiagnostics(
            seed=self.seed,
            rotation=self.rotation,
            candidate_seat=self.candidate_seat,
            focal_decision_count=self.focal_decision_count,
            discard_decision_count=self.discard_decision_count,
            choice_discard_decision_count=self.choice_discard_decision_count,
            forced_discard_decision_count=self.forced_discard_decision_count,
            candidate_runtime_total_seconds=float(
                self.candidate_runtime_total_seconds
            ),
            game_wall_clock_seconds=float(game_wall_clock_seconds),
            records=tuple(self.records),
        )


class _DrivingCandidatePolicy:
    __slots__ = ("_candidate", "_recorder")

    def __init__(self, candidate: Policy, recorder: _Recorder) -> None:
        self._candidate = candidate
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        return self._recorder.decide(self._candidate, decision)


@dataclass(frozen=True, slots=True)
class _CandidateJob(GameJob):
    candidate_seat: Seat


@dataclass(frozen=True, slots=True)
class _CandidateJobOutcome(GameJobOutcome):
    diagnostic: GameCompositionDiagnostics | None


def _run_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
    max_steps: int,
) -> tuple[LocalGameResult, GameCompositionDiagnostics]:
    recorder = _Recorder(
        seed=seed,
        rotation=rotation,
        candidate_seat=candidate_seat,
    )
    wrapped = dict(policies)
    wrapped[candidate_seat] = _DrivingCandidatePolicy(
        policies[candidate_seat], recorder
    )
    started = time.perf_counter()
    result = LocalGameRunner(
        wrapped,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
    ).run()
    return result, recorder.snapshot(float(time.perf_counter() - started))


def _run_game_job(job: _CandidateJob) -> _CandidateJobOutcome:
    try:
        policies = _create_policies(
            job.assignment,
            seed=job.seed,
            rotation=job.rotation,
        )
        result, diagnostic = _run_single_game(
            policies,
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=job.candidate_seat,
            max_steps=job.max_steps,
        )
    except Exception:
        return _CandidateJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=(
                "Champion + HandValue v2 screen game failed:\n"
                + traceback.format_exc()
            ),
            diagnostic=None,
        )
    return _CandidateJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        diagnostic=diagnostic,
    )


def aggregate_composition_diagnostics(
    games: tuple[GameCompositionDiagnostics, ...],
    *,
    replay_wall_clock_seconds: float,
) -> CompositionAggregate:
    if not games:
        raise ChampionHandValueV2TraceError("diagnostic games must not be empty")
    records = tuple(record for game in games for record in game.records)
    selection_sources = Counter(record.selection_source for record in records)
    activation_stages = Counter(
        record.champion_activation_stage
        for record in records
        if record.champion_activation_stage is not None
    )
    return CompositionAggregate(
        game_count=len(games),
        focal_decision_count=sum(game.focal_decision_count for game in games),
        discard_decision_count=sum(game.discard_decision_count for game in games),
        choice_discard_decision_count=sum(
            game.choice_discard_decision_count for game in games
        ),
        forced_discard_decision_count=sum(
            game.forced_discard_decision_count for game in games
        ),
        selection_source_counts=dict(sorted(selection_sources.items())),
        champion_activation_stage_counts=dict(sorted(activation_stages.items())),
        champion_targeted_preserved_count=selection_sources.get(
            "CHAMPION_TARGETED_HONOR_RELEASE", 0
        ),
        hand_value_v2_attempted_count=sum(
            record.hand_value_v2_attempted for record in records
        ),
        action_changed_vs_champion_count=sum(
            record.action_changed_vs_champion for record in records
        ),
        action_changed_vs_former_parent_count=sum(
            record.action_changed_vs_former_parent for record in records
        ),
        candidate_runtime_total_seconds=sum(
            game.candidate_runtime_total_seconds for game in games
        ),
        replay_wall_clock_seconds=float(replay_wall_clock_seconds),
    )


def run_candidate_screen_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> CandidateScreenResult:
    if plan.candidate.identity != CANDIDATE_IDENTITY:
        raise ChampionHandValueV2TraceError(
            "screen plan must use exact candidate identity"
        )
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)

    jobs = [
        _CandidateJob(
            seed=seed,
            rotation=rotation,
            assignment=_seat_assignment(plan, rotation),
            game_mode=GAME_MODE,
            max_steps=plan.max_steps,
            candidate_seat=Seat(rotation),
        )
        for seed in plan.seeds
        for rotation in range(ROTATION_COUNT)
    ]
    started = time.perf_counter()
    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        game_runner=_run_game_job,
        progress_callback=progress_callback,
    )
    wall_clock = float(time.perf_counter() - started)

    game_results: list[SingleRoundGameResult] = []
    diagnostics: list[GameCompositionDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "Champion + HandValue v2 screen failed in a worker",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _CandidateJobOutcome):
                raise ChampionHandValueV2TraceError(
                    "worker returned an unexpected outcome type"
                )
            if outcome.result is None or outcome.diagnostic is None:
                raise ChampionHandValueV2TraceError(
                    "worker returned incomplete success"
                )
            game_results.append(
                _build_game_result(
                    outcome.result,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=Seat(rotation),
                )
            )
            diagnostics.append(outcome.diagnostic)

    frozen_results = tuple(game_results)
    if len(frozen_results) != TOTAL_GAMES:
        raise ChampionHandValueV2TraceError(
            f"screen must produce exactly {TOTAL_GAMES} games"
        )
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            CANDIDATE_IDENTITY,
            frozen_results,
        ),
    )
    frozen_diagnostics = tuple(diagnostics)
    aggregate = aggregate_composition_diagnostics(
        frozen_diagnostics,
        replay_wall_clock_seconds=wall_clock,
    )
    if aggregate.choice_discard_decision_count != sum(
        len(game.records) for game in frozen_diagnostics
    ):
        raise ChampionHandValueV2TraceError(
            "every candidate choice discard must have one composition record"
        )
    return CandidateScreenResult(
        evaluation_result=evaluation,
        game_diagnostics=frozen_diagnostics,
        aggregate=aggregate,
    )


__all__ = [
    "CandidateScreenResult",
    "ChampionHandValueV2TraceError",
    "CompositionAggregate",
    "CompositionDecisionRecord",
    "GameCompositionDiagnostics",
    "aggregate_composition_diagnostics",
    "run_candidate_screen_parallel",
]
