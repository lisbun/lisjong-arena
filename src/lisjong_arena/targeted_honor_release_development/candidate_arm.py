"""Issue #263 Phase-B H-arm execution with typed #174 trace diagnostics.

Unlike Phase A, H is authoritative here.  The wrapper calls H exactly once via
the normal traced execution boundary, records the existing immutable
TargetedHonorReleaseAnalysis when the decision is a choice discard, and returns
H's selected action unchanged to LocalGameRunner.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lisjong.policies.targeted_honor_release_terminal_progression import (
    TargetedHonorReleaseAnalysis,
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

from .diagnostic import (
    DecisionIdentity,
    DecisionRecord,
    DiagnosticAggregate,
    GameDiagnostics,
    TargetedHonorReleaseDiagnosticError,
    aggregate_diagnostics,
)
from .protocol import (
    CANDIDATE_IDENTITY,
    COMPARATOR_IDENTITY,
    ROTATION_COUNT,
)


@dataclass(frozen=True, slots=True)
class CandidateArmDiagnosticResult:
    evaluation_result: SingleRoundEvaluationResult
    game_diagnostics: tuple[GameDiagnostics, ...]
    records: tuple[DecisionRecord, ...]
    aggregate: DiagnosticAggregate

    def __post_init__(self) -> None:
        games = tuple(self.game_diagnostics)
        records = tuple(record for game in games for record in game.records)
        if len(games) != len(self.evaluation_result.game_results):
            raise ValueError("diagnostics must align with H-arm game results")
        if records != tuple(self.records):
            raise ValueError("records must be canonical flattened H-arm records")
        object.__setattr__(self, "game_diagnostics", games)
        object.__setattr__(self, "records", records)


class _DrivingRecorder:
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
        self.records: list[DecisionRecord] = []

    def decide(self, candidate: Policy, decision: DecisionContext) -> InternalAction:
        ordinal = self.focal_decision_count
        self.focal_decision_count += 1

        trace_recorder = DecisionTraceRecorder()
        started = time.perf_counter()
        candidate_action = execute_policy_with_trace(
            candidate, decision, trace_recorder
        )
        elapsed = float(time.perf_counter() - started)
        self.candidate_runtime_total_seconds += elapsed
        traces = trace_recorder.snapshot()
        if len(traces) != 1:
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm traced execution must emit exactly one decision trace"
            )
        trace = traces[0]
        if trace.selected_action != candidate_action:
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm trace selected action differs from executed action"
            )

        if not isinstance(candidate_action, DiscardAction):
            return candidate_action

        discard_actions = tuple(
            action
            for action in decision.legal_actions
            if isinstance(action, DiscardAction)
        )
        if candidate_action not in discard_actions:
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm selected discard is not a legal discard"
            )
        self.discard_decision_count += 1
        if len(discard_actions) == 1:
            self.forced_discard_decision_count += 1
            return candidate_action

        self.choice_discard_decision_count += 1
        analysis = trace.analysis
        if not isinstance(analysis, TargetedHonorReleaseAnalysis):
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm choice discard did not expose TargetedHonorReleaseAnalysis"
            )
        if analysis.selected_action != candidate_action:
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm analysis selected_action differs from executed action"
            )
        if analysis.parent_action not in discard_actions:
            raise TargetedHonorReleaseDiagnosticError(
                "H-arm analysis parent_action is not a legal discard"
            )
        masses = tuple(
            item.terminal_shanten_mass for item in analysis.progression_evaluations
        )
        self.records.append(
            DecisionRecord(
                identity=DecisionIdentity(self.seed, self.rotation, ordinal),
                candidate_seat=self.candidate_seat,
                parent_action_repr=repr(analysis.parent_action),
                candidate_action_repr=repr(candidate_action),
                action_changed=analysis.action_changed,
                activation_stage=analysis.activation_stage,
                branch=analysis.branch,
                closed_hand=analysis.closed_hand,
                parent_post_discard_shanten=analysis.parent_post_discard_shanten,
                parent_current_ukeire=analysis.parent_current_ukeire,
                parent_retained_real_value=analysis.parent_retained_real_value,
                eligible_candidate_count=analysis.eligible_candidate_count,
                target_candidate_count=analysis.target_candidate_count,
                honor_target_candidate_count=analysis.honor_target_candidate_count,
                hva_decisive_stage=analysis.hva_decisive_stage,
                r5_activated=bool(analysis.progression_evaluations),
                r5_best_count=analysis.r5_best_count,
                sequence_denominator=analysis.sequence_denominator,
                terminal_shanten_masses=masses,
                candidate_elapsed_seconds=elapsed,
            )
        )
        return candidate_action

    def snapshot(self, game_wall_clock_seconds: float) -> GameDiagnostics:
        return GameDiagnostics(
            seed=self.seed,
            rotation=self.rotation,
            candidate_seat=self.candidate_seat,
            focal_decision_count=self.focal_decision_count,
            discard_decision_count=self.discard_decision_count,
            choice_discard_decision_count=self.choice_discard_decision_count,
            forced_discard_decision_count=self.forced_discard_decision_count,
            game_wall_clock_seconds=float(game_wall_clock_seconds),
            candidate_runtime_total_seconds=float(
                self.candidate_runtime_total_seconds
            ),
            records=tuple(self.records),
        )


class _DrivingCandidatePolicy:
    __slots__ = ("_candidate", "_recorder")

    def __init__(self, candidate: Policy, recorder: _DrivingRecorder) -> None:
        self._candidate = candidate
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        return self._recorder.decide(self._candidate, decision)


def _validate_plan(plan: SingleRoundEvaluationPlan) -> None:
    if plan.candidate.identity != CANDIDATE_IDENTITY:
        raise TargetedHonorReleaseDiagnosticError(
            "H-arm plan must use the exact #174 candidate identity"
        )
    if plan.baseline.identity != COMPARATOR_IDENTITY:
        raise TargetedHonorReleaseDiagnosticError(
            "H-arm plan must use the exact passive comparator identity"
        )


def _run_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
    max_steps: int,
) -> tuple[LocalGameResult, GameDiagnostics]:
    recorder = _DrivingRecorder(
        seed=seed, rotation=rotation, candidate_seat=candidate_seat
    )
    wrapped = dict(policies)
    wrapped[candidate_seat] = _DrivingCandidatePolicy(
        policies[candidate_seat], recorder
    )
    started = time.perf_counter()
    result = LocalGameRunner(
        wrapped, seed=seed, game_mode=GAME_MODE, max_steps=max_steps
    ).run()
    elapsed = float(time.perf_counter() - started)
    return result, recorder.snapshot(elapsed)


@dataclass(frozen=True, slots=True)
class _CandidateJob(GameJob):
    candidate_seat: Seat


@dataclass(frozen=True, slots=True)
class _CandidateJobOutcome(GameJobOutcome):
    diagnostic: GameDiagnostics | None


def _run_game_job(job: _CandidateJob) -> _CandidateJobOutcome:
    try:
        policies = _create_policies(
            job.assignment, seed=job.seed, rotation=job.rotation
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
                "targeted honor-release H-arm game failed:\n"
                f"{traceback.format_exc()}"
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


def run_candidate_arm_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> CandidateArmDiagnosticResult:
    _validate_plan(plan)
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
    diagnostics: list[GameDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "targeted honor-release H arm failed in a worker",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _CandidateJobOutcome):
                raise TargetedHonorReleaseDiagnosticError(
                    "H-arm worker returned an unexpected outcome type"
                )
            if outcome.result is None or outcome.diagnostic is None:
                raise TargetedHonorReleaseDiagnosticError(
                    "H-arm worker returned incomplete success"
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
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            CANDIDATE_IDENTITY, frozen_results
        ),
    )
    frozen_games = tuple(diagnostics)
    records = tuple(record for game in frozen_games for record in game.records)
    aggregate = aggregate_diagnostics(
        frozen_games, replay_wall_clock_seconds=wall_clock
    )
    return CandidateArmDiagnosticResult(
        evaluation_result=evaluation,
        game_diagnostics=frozen_games,
        records=records,
        aggregate=aggregate,
    )


__all__ = [
    "CandidateArmDiagnosticResult",
    "run_candidate_arm_parallel",
]
