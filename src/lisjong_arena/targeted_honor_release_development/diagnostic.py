"""Issue #263 Phase-A parent-trajectory diagnostic.

The exact #252 parent Policy drives every game.  On focal choice-discard
positions only, the exact #174 candidate is executed once through lisjong's
normal traced execution boundary.  The candidate proposal never feeds back into
the replayed trajectory.
"""

from __future__ import annotations

import math
import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lisjong.policies.targeted_honor_release_terminal_progression import (
    HandValueDecisiveStage,
    TargetedHonorReleaseActivationStage,
    TargetedHonorReleaseAnalysis,
    TargetedHonorReleaseBranch,
)
from lisjong.policy_contract import (
    DecisionContext,
    DecisionTraceRecorder,
    DiscardAction,
    InternalAction,
    Policy,
    Seat,
    execute_policy,
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
from lisjong_arena.progression_development.paired import load_arm_artifact
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner
from lisjong_arena.single_round_artifact import SingleRoundStrengthArtifact
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    SingleRoundEvaluationError,
    _build_game_result,
    _create_policies,
    _seat_assignment,
    aggregate_candidate_metrics,
)

from .protocol import (
    DIAGNOSTIC_COMPLETE_LABEL,
    FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    INFEASIBLE_LABEL,
    MAX_STEPS,
    OPPORTUNITY_NOT_OBSERVED_LABEL,
    COMPARATOR_IDENTITY,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    ROTATION_COUNT,
    TRAJECTORY_FAILURE_LABEL,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_phase_a_population,
)


class TargetedHonorReleaseDiagnosticError(ValueError):
    """Issue #263 Phase-A diagnostic cannot be completed safely."""


@dataclass(frozen=True, slots=True, order=True)
class DecisionIdentity:
    seed: int
    rotation: int
    decision_ordinal: int

    def __post_init__(self) -> None:
        for name in ("seed", "rotation", "decision_ordinal"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        if self.seed < 0 or self.decision_ordinal < 0:
            raise ValueError("seed and decision_ordinal must be non-negative")
        if not 0 <= self.rotation < ROTATION_COUNT:
            raise ValueError("rotation must be in range(4)")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    identity: DecisionIdentity
    candidate_seat: Seat
    parent_action_repr: str
    candidate_action_repr: str
    action_changed: bool
    activation_stage: TargetedHonorReleaseActivationStage
    branch: TargetedHonorReleaseBranch
    closed_hand: bool
    parent_post_discard_shanten: int | None
    parent_current_ukeire: int | None
    parent_retained_real_value: int | None
    eligible_candidate_count: int
    target_candidate_count: int
    honor_target_candidate_count: int
    hva_decisive_stage: HandValueDecisiveStage
    r5_activated: bool
    r5_best_count: int
    sequence_denominator: int | None
    terminal_shanten_masses: tuple[int, ...]
    candidate_elapsed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DecisionIdentity):
            raise TypeError("identity must be a DecisionIdentity")
        if not isinstance(self.candidate_seat, Seat):
            raise TypeError("candidate_seat must be a Seat")
        if self.candidate_seat != Seat(self.identity.rotation):
            raise ValueError("candidate_seat must match rotation")
        for name in ("parent_action_repr", "candidate_action_repr"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty str")
        for name in ("action_changed", "closed_hand", "r5_activated"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if not isinstance(
            self.activation_stage, TargetedHonorReleaseActivationStage
        ):
            raise TypeError("activation_stage has the wrong enum type")
        if not isinstance(self.branch, TargetedHonorReleaseBranch):
            raise TypeError("branch has the wrong enum type")
        if not isinstance(self.hva_decisive_stage, HandValueDecisiveStage):
            raise TypeError("hva_decisive_stage has the wrong enum type")
        for name in (
            "parent_post_discard_shanten",
            "parent_current_ukeire",
            "parent_retained_real_value",
            "sequence_denominator",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise TypeError(f"{name} must be an int or None")
        for name in (
            "eligible_candidate_count",
            "target_candidate_count",
            "honor_target_candidate_count",
            "r5_best_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        masses = tuple(self.terminal_shanten_masses)
        if any(type(value) is not int or value < 0 for value in masses):
            raise ValueError("terminal_shanten_masses must contain non-negative ints")
        if self.r5_activated != bool(masses):
            raise ValueError("r5_activated must equal terminal mass availability")
        if self.r5_activated:
            if self.sequence_denominator is None or self.sequence_denominator <= 0:
                raise ValueError("R5 activation requires a positive denominator")
            if len(masses) != self.target_candidate_count:
                raise ValueError("R5 masses must cover every target candidate")
        elif self.r5_best_count != 0 or self.sequence_denominator is not None:
            raise ValueError("inactive R5 must not expose R5-only values")
        if type(self.candidate_elapsed_seconds) is not float:
            raise TypeError("candidate_elapsed_seconds must be a float")
        if self.candidate_elapsed_seconds < 0.0 or not math.isfinite(
            self.candidate_elapsed_seconds
        ):
            raise ValueError("candidate_elapsed_seconds must be finite and non-negative")
        object.__setattr__(self, "terminal_shanten_masses", masses)


@dataclass(frozen=True, slots=True)
class GameDiagnostics:
    seed: int
    rotation: int
    candidate_seat: Seat
    focal_decision_count: int
    discard_decision_count: int
    choice_discard_decision_count: int
    forced_discard_decision_count: int
    game_wall_clock_seconds: float
    candidate_runtime_total_seconds: float
    records: tuple[DecisionRecord, ...]

    def __post_init__(self) -> None:
        if type(self.seed) is not int or type(self.rotation) is not int:
            raise TypeError("seed and rotation must be ints")
        if not isinstance(self.candidate_seat, Seat):
            raise TypeError("candidate_seat must be a Seat")
        if self.candidate_seat != Seat(self.rotation):
            raise ValueError("candidate_seat must match rotation")
        for name in (
            "focal_decision_count",
            "discard_decision_count",
            "choice_discard_decision_count",
            "forced_discard_decision_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.choice_discard_decision_count != len(self.records):
            raise ValueError("choice discard count must equal record count")
        if self.discard_decision_count != (
            self.choice_discard_decision_count + self.forced_discard_decision_count
        ):
            raise ValueError("choice and forced discards must partition discards")
        if self.discard_decision_count > self.focal_decision_count:
            raise ValueError("discard decisions cannot exceed focal decisions")
        for name in ("game_wall_clock_seconds", "candidate_runtime_total_seconds"):
            value = getattr(self, name)
            if type(value) is not float or value < 0.0 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and non-negative")
        object.__setattr__(self, "records", tuple(self.records))


@dataclass(frozen=True, slots=True)
class NumericSummary:
    count: int
    mean: float | None
    p50: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class DiagnosticAggregate:
    game_count: int
    choice_discard_decision_count: int
    activation_stage_counts: tuple[tuple[str, int], ...]
    branch_counts: tuple[tuple[str, int], ...]
    closed_count: int
    open_count: int
    parent_shanten_counts: tuple[tuple[str, int], ...]
    parent_ukeire_counts: tuple[tuple[str, int], ...]
    parent_retained_value_counts: tuple[tuple[str, int], ...]
    target_candidate_counts: tuple[tuple[str, int], ...]
    honor_target_candidate_counts: tuple[tuple[str, int], ...]
    hva_decisive_stage_counts: tuple[tuple[str, int], ...]
    r5_activation_count: int
    r5_best_count_distribution: tuple[tuple[str, int], ...]
    action_change_count: int
    analyzed_decision_runtime: NumericSummary
    r5_activated_runtime: NumericSummary
    candidate_runtime_total_seconds: float
    candidate_runtime_per_game_seconds: float
    replay_game_runtime: NumericSummary
    replay_wall_clock_seconds: float
    projected_h_arm_wall_clock_hours: float


@dataclass(frozen=True, slots=True)
class DiagnosticGate:
    passed: bool
    label: str
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PhaseADiagnosticResult:
    evaluation_result: SingleRoundEvaluationResult
    game_diagnostics: tuple[GameDiagnostics, ...]
    records: tuple[DecisionRecord, ...]
    aggregate: DiagnosticAggregate
    gate: DiagnosticGate

    def __post_init__(self) -> None:
        games = tuple(self.game_diagnostics)
        records = tuple(record for game in games for record in game.records)
        if len(games) != len(self.evaluation_result.game_results):
            raise ValueError("diagnostics must align with evaluation game results")
        if records != tuple(self.records):
            raise ValueError("records must be canonical flattened game records")
        object.__setattr__(self, "game_diagnostics", games)
        object.__setattr__(self, "records", records)


class _Recorder:
    __slots__ = (
        "seed",
        "rotation",
        "candidate_seat",
        "candidate",
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
        self.candidate = candidate_spec().factory()
        self.focal_decision_count = 0
        self.discard_decision_count = 0
        self.choice_discard_decision_count = 0
        self.forced_discard_decision_count = 0
        self.candidate_runtime_total_seconds = 0.0
        self.records: list[DecisionRecord] = []

    def observe(
        self, decision: DecisionContext, parent_action: InternalAction
    ) -> None:
        ordinal = self.focal_decision_count
        self.focal_decision_count += 1

        trace_recorder = DecisionTraceRecorder()
        started = time.perf_counter()
        candidate_action = execute_policy_with_trace(
            self.candidate, decision, trace_recorder
        )
        elapsed = float(time.perf_counter() - started)
        self.candidate_runtime_total_seconds += elapsed
        traces = trace_recorder.snapshot()
        if len(traces) != 1:
            raise TargetedHonorReleaseDiagnosticError(
                "candidate traced execution must emit exactly one decision trace"
            )
        trace = traces[0]

        if not isinstance(parent_action, DiscardAction):
            if candidate_action != parent_action:
                raise TargetedHonorReleaseDiagnosticError(
                    "candidate changed a non-discard parent decision"
                )
            return

        discard_actions = tuple(
            action
            for action in decision.legal_actions
            if isinstance(action, DiscardAction)
        )
        if parent_action not in discard_actions:
            raise TargetedHonorReleaseDiagnosticError(
                "parent selected discard is not a legal discard"
            )
        self.discard_decision_count += 1
        if len(discard_actions) == 1:
            self.forced_discard_decision_count += 1
            if candidate_action != parent_action:
                raise TargetedHonorReleaseDiagnosticError(
                    "candidate changed a forced parent discard"
                )
            return

        self.choice_discard_decision_count += 1
        analysis = trace.analysis
        if not isinstance(analysis, TargetedHonorReleaseAnalysis):
            raise TargetedHonorReleaseDiagnosticError(
                "candidate choice discard did not expose TargetedHonorReleaseAnalysis"
            )
        if analysis.parent_action != parent_action:
            raise TargetedHonorReleaseDiagnosticError(
                "candidate analysis parent_action differs from production parent action"
            )
        if analysis.selected_action != candidate_action:
            raise TargetedHonorReleaseDiagnosticError(
                "candidate analysis selected_action differs from traced action"
            )
        masses = tuple(
            item.terminal_shanten_mass for item in analysis.progression_evaluations
        )
        self.records.append(
            DecisionRecord(
                identity=DecisionIdentity(self.seed, self.rotation, ordinal),
                candidate_seat=self.candidate_seat,
                parent_action_repr=repr(parent_action),
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


class _ObservedParentPolicy:
    __slots__ = ("_parent", "_recorder")

    def __init__(self, parent: Policy, recorder: _Recorder) -> None:
        self._parent = parent
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        parent_action = execute_policy(self._parent, decision)
        self._recorder.observe(decision, parent_action)
        return parent_action


def build_phase_a_plan() -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=parent_spec(),
        baseline=comparator_spec(),
        seeds=require_phase_a_population(PHASE_A_SEEDS),
        max_steps=MAX_STEPS,
    )


def _run_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
    max_steps: int,
) -> tuple[LocalGameResult, GameDiagnostics]:
    recorder = _Recorder(
        seed=seed, rotation=rotation, candidate_seat=candidate_seat
    )
    wrapped = dict(policies)
    wrapped[candidate_seat] = _ObservedParentPolicy(
        policies[candidate_seat], recorder
    )
    started = time.perf_counter()
    result = LocalGameRunner(
        wrapped, seed=seed, game_mode=GAME_MODE, max_steps=max_steps
    ).run()
    elapsed = float(time.perf_counter() - started)
    return result, recorder.snapshot(elapsed)


@dataclass(frozen=True, slots=True)
class _DiagnosticJob(GameJob):
    candidate_seat: Seat


@dataclass(frozen=True, slots=True)
class _DiagnosticJobOutcome(GameJobOutcome):
    diagnostic: GameDiagnostics | None


def _run_game_job(job: _DiagnosticJob) -> _DiagnosticJobOutcome:
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
        return _DiagnosticJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=(
                "targeted honor-release diagnostic game failed:\n"
                f"{traceback.format_exc()}"
            ),
            diagnostic=None,
        )
    return _DiagnosticJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        diagnostic=diagnostic,
    )


def _numeric_summary(values: Sequence[float]) -> NumericSummary:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return NumericSummary(0, None, None, None, None, None)
    if any(value < 0.0 or not math.isfinite(value) for value in ordered):
        raise TargetedHonorReleaseDiagnosticError(
            "runtime values must be finite and non-negative"
        )
    count = len(ordered)
    p50_index = max(0, math.ceil(0.50 * count) - 1)
    p95_index = max(0, math.ceil(0.95 * count) - 1)
    return NumericSummary(
        count=count,
        mean=sum(ordered) / count,
        p50=ordered[p50_index],
        p95=ordered[p95_index],
        minimum=ordered[0],
        maximum=ordered[-1],
    )


def _count_optional_int(
    records: Sequence[DecisionRecord], field: str
) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    for record in records:
        value = getattr(record, field)
        counts["UNAVAILABLE" if value is None else str(value)] += 1
    return tuple(sorted(counts.items()))


def _count_int(
    records: Sequence[DecisionRecord], field: str
) -> tuple[tuple[str, int], ...]:
    counts = Counter(str(getattr(record, field)) for record in records)
    return tuple(sorted(counts.items(), key=lambda item: int(item[0])))


def aggregate_diagnostics(
    games: tuple[GameDiagnostics, ...],
    *,
    replay_wall_clock_seconds: float,
) -> DiagnosticAggregate:
    records = tuple(record for game in games for record in game.records)
    analyzed_runtime = tuple(record.candidate_elapsed_seconds for record in records)
    r5_runtime = tuple(
        record.candidate_elapsed_seconds for record in records if record.r5_activated
    )
    candidate_total = sum(
        game.candidate_runtime_total_seconds for game in games
    )
    return DiagnosticAggregate(
        game_count=len(games),
        choice_discard_decision_count=len(records),
        activation_stage_counts=tuple(
            sorted(Counter(record.activation_stage.name for record in records).items())
        ),
        branch_counts=tuple(
            sorted(Counter(record.branch.name for record in records).items())
        ),
        closed_count=sum(record.closed_hand for record in records),
        open_count=sum(not record.closed_hand for record in records),
        parent_shanten_counts=_count_optional_int(
            records, "parent_post_discard_shanten"
        ),
        parent_ukeire_counts=_count_optional_int(records, "parent_current_ukeire"),
        parent_retained_value_counts=_count_optional_int(
            records, "parent_retained_real_value"
        ),
        target_candidate_counts=_count_int(records, "target_candidate_count"),
        honor_target_candidate_counts=_count_int(
            records, "honor_target_candidate_count"
        ),
        hva_decisive_stage_counts=tuple(
            sorted(Counter(record.hva_decisive_stage.name for record in records).items())
        ),
        r5_activation_count=sum(record.r5_activated for record in records),
        r5_best_count_distribution=_count_int(records, "r5_best_count"),
        action_change_count=sum(record.action_changed for record in records),
        analyzed_decision_runtime=_numeric_summary(analyzed_runtime),
        r5_activated_runtime=_numeric_summary(r5_runtime),
        candidate_runtime_total_seconds=candidate_total,
        candidate_runtime_per_game_seconds=(
            candidate_total / len(games) if games else 0.0
        ),
        replay_game_runtime=_numeric_summary(
            tuple(game.game_wall_clock_seconds for game in games)
        ),
        replay_wall_clock_seconds=float(replay_wall_clock_seconds),
        projected_h_arm_wall_clock_hours=float(replay_wall_clock_seconds) / 3600.0,
    )


def classify_gate(
    *,
    trajectory_identity_passed: bool,
    game_count: int,
    execution_failure_count: int,
    aggregate: DiagnosticAggregate,
) -> DiagnosticGate:
    reasons: list[str] = []
    if not trajectory_identity_passed:
        return DiagnosticGate(False, TRAJECTORY_FAILURE_LABEL, ("trajectory mismatch",))
    if game_count != PHASE_A_GAME_COUNT:
        reasons.append(
            f"expected {PHASE_A_GAME_COUNT} source games, got {game_count}"
        )
    if execution_failure_count != 0:
        reasons.append(f"execution failures={execution_failure_count}")
    if aggregate.r5_activation_count == 0 or aggregate.action_change_count == 0:
        return DiagnosticGate(
            False,
            OPPORTUNITY_NOT_OBSERVED_LABEL,
            tuple(reasons)
            + (
                f"r5_activation_count={aggregate.r5_activation_count}",
                f"action_change_count={aggregate.action_change_count}",
            ),
        )
    if (
        aggregate.projected_h_arm_wall_clock_hours
        > FEASIBILITY_WALL_CLOCK_LIMIT_HOURS
    ):
        return DiagnosticGate(
            False,
            INFEASIBLE_LABEL,
            tuple(reasons)
            + (
                "projected H-arm wall clock exceeds "
                f"{FEASIBILITY_WALL_CLOCK_LIMIT_HOURS} hours",
            ),
        )
    if reasons:
        return DiagnosticGate(False, INFEASIBLE_LABEL, tuple(reasons))
    return DiagnosticGate(True, DIAGNOSTIC_COMPLETE_LABEL, ())


def require_trajectory_identity(
    replay: SingleRoundEvaluationResult,
    historical_parent: SingleRoundStrengthArtifact,
) -> None:
    plan = historical_parent.plan
    if plan.candidate_identity != PARENT_IDENTITY:
        raise TargetedHonorReleaseDiagnosticError(
            "historical source artifact is not the #252 parent arm"
        )
    if plan.baseline_identity != COMPARATOR_IDENTITY:
        raise TargetedHonorReleaseDiagnosticError(
            "historical source artifact does not use the #252 passive comparator"
        )
    if plan.seeds != PHASE_A_SEEDS or plan.max_steps != MAX_STEPS:
        raise TargetedHonorReleaseDiagnosticError(
            "historical source artifact differs from the #252 parent population"
        )
    if len(historical_parent.game_results) != PHASE_A_GAME_COUNT:
        raise TargetedHonorReleaseDiagnosticError(
            "historical source artifact must contain exactly 400 games"
        )
    if replay.game_results != historical_parent.game_results:
        for current, historical in zip(
            replay.game_results, historical_parent.game_results, strict=True
        ):
            if current != historical:
                raise TargetedHonorReleaseDiagnosticError(
                    f"{TRAJECTORY_FAILURE_LABEL} at "
                    f"seed={current.seed} rotation={current.rotation}"
                )
        raise TargetedHonorReleaseDiagnosticError(TRAJECTORY_FAILURE_LABEL)


def run_phase_a_parallel(
    *,
    parent_artifact_path: str,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PhaseADiagnosticResult:
    plan = build_phase_a_plan()
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)
    check_policy_spec_serializable(candidate_spec())

    jobs = [
        _DiagnosticJob(
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
    replay_wall_clock = float(time.perf_counter() - started)

    game_results: list[SingleRoundGameResult] = []
    diagnostics: list[GameDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "targeted honor-release Phase A failed in a worker",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _DiagnosticJobOutcome):
                raise TargetedHonorReleaseDiagnosticError(
                    "Phase A worker returned an unexpected outcome type"
                )
            if outcome.result is None or outcome.diagnostic is None:
                raise TargetedHonorReleaseDiagnosticError(
                    "Phase A worker returned incomplete success"
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
        candidate_metrics=aggregate_candidate_metrics(PARENT_IDENTITY, frozen_results),
    )
    historical = load_arm_artifact(parent_artifact_path)
    require_trajectory_identity(evaluation, historical)
    frozen_games = tuple(diagnostics)
    records = tuple(record for game in frozen_games for record in game.records)
    aggregate = aggregate_diagnostics(
        frozen_games, replay_wall_clock_seconds=replay_wall_clock
    )
    gate = classify_gate(
        trajectory_identity_passed=True,
        game_count=len(frozen_results),
        execution_failure_count=0,
        aggregate=aggregate,
    )
    return PhaseADiagnosticResult(
        evaluation_result=evaluation,
        game_diagnostics=frozen_games,
        records=records,
        aggregate=aggregate,
        gate=gate,
    )


__all__ = [
    "DecisionIdentity",
    "DecisionRecord",
    "DiagnosticAggregate",
    "DiagnosticGate",
    "GameDiagnostics",
    "NumericSummary",
    "PhaseADiagnosticResult",
    "TargetedHonorReleaseDiagnosticError",
    "aggregate_diagnostics",
    "build_phase_a_plan",
    "classify_gate",
    "require_trajectory_identity",
    "run_phase_a_parallel",
]
