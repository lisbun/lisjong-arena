"""Issue #256 purpose-specific offensive-efficiency diagnostic execution.

The current ``mechanism-riichi-defense`` baseline is replayed on the exact #252
parent population.  The focal Policy action always drives the game.  A thin
wrapper observes focal discard choices and calls the supported lisjong #172
analysis seam; Arena never recreates shanten / ukeire / FiniteHorizon / terminal
progression semantics.

Phase 1 calls the seam with ``include_terminal_progression=False`` for every
choice discard.  Phase 2 deterministically selects at most 64 all-zero decisions
from the Phase-1 rows, replays only the games containing those decisions, and
calls the same seam with ``include_terminal_progression=True`` at the selected
stable decision ordinals.
"""

from __future__ import annotations

import math
import traceback
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from statistics import median

from lisjong.policies.mechanism_riichi_defense_offensive_efficiency_diagnostic import (
    MechanismRiichiDefenseOffensiveEfficiencyAnalysis,
    OffensiveEfficiencyBranch,
    analyze_mechanism_riichi_defense_offensive_efficiency,
)
from lisjong.policy_contract import (
    DecisionContext,
    DiscardAction,
    InternalAction,
    Policy,
    Seat,
    execute_policy,
)
from lisjong.policy_contract.riichi import RiichiState

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
from lisjong_arena.progression_development.protocol import (
    COMPARATOR_IDENTITY,
    MAX_STEPS,
    PARENT_IDENTITY,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    ROTATION_COUNT,
    comparator_spec,
    parent_spec,
    require_phase_b_population,
)
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

PHASE2_SAMPLE_LIMIT = 64
METRICS = ("R1_SHANTEN", "R2_UKEIRE", "R3_SECOND_STEP", "R4_COMPLETION_MASS")
UNIVERSES = ("FULL_LEGAL", "BASELINE_ELIGIBLE")


class OffensiveEfficiencyDiagnosticError(ValueError):
    """Issue #256 diagnostic contract cannot be satisfied."""


class DecisionKind(Enum):
    NORMAL_TURN = "normal_turn"
    POST_CALL = "post_call"


class TurnBucket(Enum):
    EARLY = "early"
    MIDDLE = "middle"
    LATE = "late"


@dataclass(frozen=True, slots=True, order=True)
class DecisionIdentity:
    seed: int
    rotation: int
    decision_ordinal: int

    def __post_init__(self) -> None:
        for name in ("seed", "rotation", "decision_ordinal"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if not 0 <= self.rotation < ROTATION_COUNT:
            raise ValueError("rotation must be in range(4)")
        if self.decision_ordinal < 0:
            raise ValueError("decision_ordinal must be non-negative")


@dataclass(frozen=True, slots=True)
class Phase1DecisionRecord:
    identity: DecisionIdentity
    candidate_seat: Seat
    decision_kind: DecisionKind
    open_hand: bool
    self_riichi: bool
    turn_bucket: TurnBucket
    branch: OffensiveEfficiencyBranch
    legal_discard_count: int
    baseline_eligible_discard_count: int
    selected_action_repr: str
    selected_post_discard_shanten: int
    full_shanten_regret: int
    eligible_shanten_regret: int
    full_ukeire_regret: int | None
    eligible_ukeire_regret: int | None
    full_second_step_regret: int | None
    eligible_second_step_regret: int | None
    full_completion_regret: int
    eligible_completion_regret: int
    full_completion_all_zero: bool
    eligible_completion_all_zero: bool

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DecisionIdentity):
            raise TypeError("identity must be a DecisionIdentity")
        if not isinstance(self.candidate_seat, Seat):
            raise TypeError("candidate_seat must be a Seat")
        if self.candidate_seat != Seat(self.identity.rotation):
            raise ValueError("candidate_seat must match rotation")
        if not isinstance(self.decision_kind, DecisionKind):
            raise TypeError("decision_kind must be a DecisionKind")
        if not isinstance(self.turn_bucket, TurnBucket):
            raise TypeError("turn_bucket must be a TurnBucket")
        if not isinstance(self.branch, OffensiveEfficiencyBranch):
            raise TypeError("branch must be an OffensiveEfficiencyBranch")
        for name in ("open_hand", "self_riichi"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        for name in (
            "legal_discard_count",
            "baseline_eligible_discard_count",
            "selected_post_discard_shanten",
            "full_shanten_regret",
            "eligible_shanten_regret",
            "full_completion_regret",
            "eligible_completion_regret",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in (
            "full_ukeire_regret",
            "eligible_ukeire_regret",
            "full_second_step_regret",
            "eligible_second_step_regret",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be None or a non-negative int")
        if self.legal_discard_count < 2:
            raise ValueError("Phase1DecisionRecord requires a choice discard")
        if not 1 <= self.baseline_eligible_discard_count <= self.legal_discard_count:
            raise ValueError("baseline eligible count must be within legal discard count")
        if type(self.selected_action_repr) is not str or not self.selected_action_repr:
            raise ValueError("selected_action_repr must be a non-empty str")
        for name in ("full_completion_all_zero", "eligible_completion_all_zero"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class Phase1GameDiagnostics:
    seed: int
    rotation: int
    candidate_seat: Seat
    focal_decision_count: int
    discard_decision_count: int
    choice_discard_decision_count: int
    forced_discard_decision_count: int
    records: tuple[Phase1DecisionRecord, ...]

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
        records = tuple(self.records)
        if any(not isinstance(item, Phase1DecisionRecord) for item in records):
            raise TypeError("records must contain Phase1DecisionRecord values")
        if self.choice_discard_decision_count != len(records):
            raise ValueError("choice discard count must equal record count")
        if self.discard_decision_count != (
            self.choice_discard_decision_count + self.forced_discard_decision_count
        ):
            raise ValueError("choice and forced discards must partition discard decisions")
        if self.discard_decision_count > self.focal_decision_count:
            raise ValueError("discard decisions cannot exceed focal decisions")
        object.__setattr__(self, "records", records)


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    applicable_count: int
    nonzero_count: int
    nonzero_incidence: float
    mean: float | None
    median: float | None
    p90: int | None
    maximum: int | None


@dataclass(frozen=True, slots=True)
class MetricSummary:
    metric: str
    universe: str
    distribution: DistributionSummary


@dataclass(frozen=True, slots=True)
class ClusterSummary:
    metric: str
    universe: str
    dimension: str
    value: str
    population_count: int
    population_share: float
    distribution: DistributionSummary


@dataclass(frozen=True, slots=True)
class Phase1Aggregate:
    game_count: int
    discard_decision_count: int
    choice_discard_decision_count: int
    forced_discard_decision_count: int
    normal_turn_choice_count: int
    post_call_choice_count: int
    branch_counts: tuple[tuple[str, int], ...]
    metric_summaries: tuple[MetricSummary, ...]


@dataclass(frozen=True, slots=True)
class Phase1EvaluationResult:
    evaluation_result: SingleRoundEvaluationResult
    game_diagnostics: tuple[Phase1GameDiagnostics, ...]
    records: tuple[Phase1DecisionRecord, ...]
    aggregate: Phase1Aggregate
    clusters: tuple[ClusterSummary, ...]

    def __post_init__(self) -> None:
        games = tuple(self.game_diagnostics)
        records = tuple(self.records)
        if len(games) != len(self.evaluation_result.game_results):
            raise ValueError("game diagnostics must align with evaluation game results")
        flattened = tuple(record for game in games for record in game.records)
        if records != flattened:
            raise ValueError("records must be canonical flattened game records")
        if self.aggregate != aggregate_phase1(games):
            raise ValueError("aggregate does not match canonical Phase 1 aggregation")
        if self.clusters != build_cluster_summaries(records):
            raise ValueError("clusters do not match canonical Phase 1 records")
        object.__setattr__(self, "game_diagnostics", games)
        object.__setattr__(self, "records", records)


@dataclass(frozen=True, slots=True)
class Phase2Sample:
    identity: DecisionIdentity
    shanten_bucket: str
    open_hand: bool


@dataclass(frozen=True, slots=True)
class TerminalUniverseResult:
    selected_terminal_shanten_mass: int
    best_terminal_shanten_mass: int
    regret_mass: int
    sequence_denominator: int
    best_tie_count: int
    selected_is_best: bool
    best_action_reprs: tuple[str, ...]

    @property
    def expected_regret(self) -> float:
        return self.regret_mass / self.sequence_denominator


@dataclass(frozen=True, slots=True)
class Phase2DecisionRecord:
    sample: Phase2Sample
    full_legal: TerminalUniverseResult | None
    baseline_eligible: TerminalUniverseResult | None


@dataclass(frozen=True, slots=True)
class Phase2UniverseAggregate:
    universe: str
    applicable_count: int
    selected_best_count: int
    nonzero_count: int
    nonzero_incidence: float
    mean_expected_regret: float | None
    median_expected_regret: float | None
    p90_expected_regret: float | None
    max_expected_regret: float | None


@dataclass(frozen=True, slots=True)
class Phase2Aggregate:
    sample_count: int
    universe_summaries: tuple[Phase2UniverseAggregate, ...]


class _Phase1Recorder:
    __slots__ = (
        "seed",
        "rotation",
        "candidate_seat",
        "focal_decision_count",
        "discard_decision_count",
        "choice_discard_decision_count",
        "forced_discard_decision_count",
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
        self.records: list[Phase1DecisionRecord] = []

    def record(self, decision: DecisionContext, action: InternalAction) -> None:
        ordinal = self.focal_decision_count
        self.focal_decision_count += 1
        if not isinstance(action, DiscardAction):
            return
        discard_actions = tuple(
            item for item in decision.legal_actions if isinstance(item, DiscardAction)
        )
        if action not in discard_actions:
            raise OffensiveEfficiencyDiagnosticError(
                "selected discard is not present in legal discard actions"
            )
        self.discard_decision_count += 1
        if len(discard_actions) == 1:
            self.forced_discard_decision_count += 1
            return
        self.choice_discard_decision_count += 1
        analysis = analyze_mechanism_riichi_defense_offensive_efficiency(
            decision.input,
            discard_actions,
            include_terminal_progression=False,
        )
        if analysis.baseline_selected_action != action:
            raise OffensiveEfficiencyDiagnosticError(
                "#172 baseline_selected_action differs from production action"
            )
        self.records.append(
            _phase1_record_from_analysis(
                identity=DecisionIdentity(self.seed, self.rotation, ordinal),
                candidate_seat=self.candidate_seat,
                decision=decision,
                action=action,
                analysis=analysis,
            )
        )

    def snapshot(self) -> Phase1GameDiagnostics:
        return Phase1GameDiagnostics(
            seed=self.seed,
            rotation=self.rotation,
            candidate_seat=self.candidate_seat,
            focal_decision_count=self.focal_decision_count,
            discard_decision_count=self.discard_decision_count,
            choice_discard_decision_count=self.choice_discard_decision_count,
            forced_discard_decision_count=self.forced_discard_decision_count,
            records=tuple(self.records),
        )


class _ObservedBaselinePolicy:
    __slots__ = ("_policy", "_recorder")

    def __init__(self, policy: Policy, recorder: _Phase1Recorder) -> None:
        self._policy = policy
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        action = execute_policy(self._policy, decision)
        self._recorder.record(decision, action)
        return action


def _turn_bucket(prior_self_discards: int) -> TurnBucket:
    if prior_self_discards <= 5:
        return TurnBucket.EARLY
    if prior_self_discards <= 11:
        return TurnBucket.MIDDLE
    return TurnBucket.LATE


def _decision_attributes(
    decision: DecisionContext,
) -> tuple[DecisionKind, bool, bool, TurnBucket]:
    player = decision.input.players[decision.input.self_seat]
    kind = (
        DecisionKind.POST_CALL
        if decision.input.own_hand.drawn_tile is None
        else DecisionKind.NORMAL_TURN
    )
    open_hand = any(meld.from_seat is not None for meld in player.melds)
    self_riichi = player.riichi is not RiichiState.NONE
    return kind, open_hand, self_riichi, _turn_bucket(len(player.discards))


def _phase1_record_from_analysis(
    *,
    identity: DecisionIdentity,
    candidate_seat: Seat,
    decision: DecisionContext,
    action: DiscardAction,
    analysis: MechanismRiichiDefenseOffensiveEfficiencyAnalysis,
) -> Phase1DecisionRecord:
    kind, open_hand, self_riichi, turn_bucket = _decision_attributes(decision)
    try:
        selected_candidate = next(
            candidate
            for candidate in analysis.candidate_evaluations
            if candidate.action == action
        )
    except StopIteration as exc:
        raise OffensiveEfficiencyDiagnosticError(
            "#172 analysis does not contain the selected discard candidate"
        ) from exc
    full = analysis.full_legal_summary
    eligible = analysis.baseline_eligible_summary
    return Phase1DecisionRecord(
        identity=identity,
        candidate_seat=candidate_seat,
        decision_kind=kind,
        open_hand=open_hand,
        self_riichi=self_riichi,
        turn_bucket=turn_bucket,
        branch=analysis.branch,
        legal_discard_count=len(analysis.legal_discard_actions),
        baseline_eligible_discard_count=len(analysis.baseline_eligible_actions),
        selected_action_repr=repr(action),
        selected_post_discard_shanten=selected_candidate.post_discard_shanten,
        full_shanten_regret=full.shanten_regret,
        eligible_shanten_regret=eligible.shanten_regret,
        full_ukeire_regret=full.ukeire_regret,
        eligible_ukeire_regret=eligible.ukeire_regret,
        full_second_step_regret=full.second_step_regret,
        eligible_second_step_regret=eligible.second_step_regret,
        full_completion_regret=full.completion_regret,
        eligible_completion_regret=eligible.completion_regret,
        full_completion_all_zero=full.completion_all_zero,
        eligible_completion_all_zero=eligible.completion_all_zero,
    )


def _distribution(values: Sequence[int | None]) -> DistributionSummary:
    applicable = sorted(value for value in values if value is not None)
    count = len(applicable)
    nonzero = sum(value != 0 for value in applicable)
    if not applicable:
        return DistributionSummary(0, 0, 0.0, None, None, None, None)
    p90_index = max(0, math.ceil(0.9 * count) - 1)
    return DistributionSummary(
        applicable_count=count,
        nonzero_count=nonzero,
        nonzero_incidence=nonzero / count,
        mean=sum(applicable) / count,
        median=float(median(applicable)),
        p90=applicable[p90_index],
        maximum=applicable[-1],
    )


def metric_value(
    record: Phase1DecisionRecord, metric: str, universe: str
) -> int | None:
    fields = {
        ("R1_SHANTEN", "FULL_LEGAL"): "full_shanten_regret",
        ("R1_SHANTEN", "BASELINE_ELIGIBLE"): "eligible_shanten_regret",
        ("R2_UKEIRE", "FULL_LEGAL"): "full_ukeire_regret",
        ("R2_UKEIRE", "BASELINE_ELIGIBLE"): "eligible_ukeire_regret",
        ("R3_SECOND_STEP", "FULL_LEGAL"): "full_second_step_regret",
        ("R3_SECOND_STEP", "BASELINE_ELIGIBLE"): "eligible_second_step_regret",
        ("R4_COMPLETION_MASS", "FULL_LEGAL"): "full_completion_regret",
        ("R4_COMPLETION_MASS", "BASELINE_ELIGIBLE"): "eligible_completion_regret",
    }
    try:
        return getattr(record, fields[(metric, universe)])
    except KeyError as exc:
        raise ValueError(f"unsupported metric/universe: {metric!r}/{universe!r}") from exc


def aggregate_phase1(games: tuple[Phase1GameDiagnostics, ...]) -> Phase1Aggregate:
    records = tuple(record for game in games for record in game.records)
    branch_counts = tuple(
        sorted(
            (
                (branch.name, sum(record.branch is branch for record in records))
                for branch in OffensiveEfficiencyBranch
            ),
            key=lambda item: item[0],
        )
    )
    summaries = tuple(
        MetricSummary(
            metric=metric,
            universe=universe,
            distribution=_distribution(
                tuple(metric_value(record, metric, universe) for record in records)
            ),
        )
        for metric in METRICS
        for universe in UNIVERSES
    )
    return Phase1Aggregate(
        game_count=len(games),
        discard_decision_count=sum(game.discard_decision_count for game in games),
        choice_discard_decision_count=len(records),
        forced_discard_decision_count=sum(
            game.forced_discard_decision_count for game in games
        ),
        normal_turn_choice_count=sum(
            record.decision_kind is DecisionKind.NORMAL_TURN for record in records
        ),
        post_call_choice_count=sum(
            record.decision_kind is DecisionKind.POST_CALL for record in records
        ),
        branch_counts=branch_counts,
        metric_summaries=summaries,
    )


def _shanten_bucket(value: int) -> str:
    if value <= 2:
        return str(value)
    return "3+"


def _cluster_dimensions(record: Phase1DecisionRecord) -> tuple[tuple[str, str], ...]:
    return (
        ("branch", record.branch.name),
        ("selected_shanten", _shanten_bucket(record.selected_post_discard_shanten)),
        ("open_closed", "open" if record.open_hand else "closed"),
        ("turn_bucket", record.turn_bucket.value),
        ("decision_kind", record.decision_kind.value),
    )


def build_cluster_summaries(
    records: tuple[Phase1DecisionRecord, ...],
) -> tuple[ClusterSummary, ...]:
    if not records:
        return ()
    grouped: dict[tuple[str, str], list[Phase1DecisionRecord]] = defaultdict(list)
    for record in records:
        for dimension in _cluster_dimensions(record):
            grouped[dimension].append(record)
    result: list[ClusterSummary] = []
    for (dimension, value), items in sorted(grouped.items()):
        for metric in METRICS:
            for universe in UNIVERSES:
                result.append(
                    ClusterSummary(
                        metric=metric,
                        universe=universe,
                        dimension=dimension,
                        value=value,
                        population_count=len(items),
                        population_share=len(items) / len(records),
                        distribution=_distribution(
                            tuple(
                                metric_value(item, metric, universe) for item in items
                            )
                        ),
                    )
                )
    return tuple(result)


def build_phase1_plan() -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=parent_spec(),
        baseline=comparator_spec(),
        seeds=require_phase_b_population(PHASE_B_SEEDS),
        max_steps=MAX_STEPS,
    )


def _validate_plan(plan: SingleRoundEvaluationPlan) -> None:
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")
    if plan.candidate.identity != PARENT_IDENTITY:
        raise OffensiveEfficiencyDiagnosticError(
            f"candidate must be {PARENT_IDENTITY!r}"
        )
    if plan.baseline.identity != COMPARATOR_IDENTITY:
        raise OffensiveEfficiencyDiagnosticError(
            f"baseline must be {COMPARATOR_IDENTITY!r}"
        )
    if plan.seeds != PHASE_B_SEEDS:
        raise OffensiveEfficiencyDiagnosticError("plan must use exact #252 Phase B seeds")
    if plan.max_steps != MAX_STEPS:
        raise OffensiveEfficiencyDiagnosticError("plan max_steps differs from #252")


def _run_phase1_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
    max_steps: int,
) -> tuple[LocalGameResult, Phase1GameDiagnostics]:
    recorder = _Phase1Recorder(
        seed=seed, rotation=rotation, candidate_seat=candidate_seat
    )
    wrapped = dict(policies)
    wrapped[candidate_seat] = _ObservedBaselinePolicy(
        policies[candidate_seat], recorder
    )
    result = LocalGameRunner(
        wrapped, seed=seed, game_mode=GAME_MODE, max_steps=max_steps
    ).run()
    return result, recorder.snapshot()


@dataclass(frozen=True, slots=True)
class _Phase1GameJob(GameJob):
    candidate_seat: Seat


@dataclass(frozen=True, slots=True)
class _Phase1GameJobOutcome(GameJobOutcome):
    diagnostic: Phase1GameDiagnostics | None


def _run_phase1_game_job(job: _Phase1GameJob) -> _Phase1GameJobOutcome:
    try:
        policies = _create_policies(job.assignment, seed=job.seed, rotation=job.rotation)
        result, diagnostic = _run_phase1_single_game(
            policies,
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=job.candidate_seat,
            max_steps=job.max_steps,
        )
    except Exception:
        return _Phase1GameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=(
                "offensive-efficiency Phase 1 game failed:\n"
                f"{traceback.format_exc()}"
            ),
            diagnostic=None,
        )
    return _Phase1GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        diagnostic=diagnostic,
    )


def _build_phase1_result(
    plan: SingleRoundEvaluationPlan,
    game_results: list[SingleRoundGameResult],
    diagnostics: list[Phase1GameDiagnostics],
) -> Phase1EvaluationResult:
    expected = ROTATION_COUNT * len(plan.seeds)
    if len(game_results) != expected or len(diagnostics) != expected:
        raise OffensiveEfficiencyDiagnosticError(
            "Phase 1 did not produce the expected game count"
        )
    frozen_games = tuple(game_results)
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_games,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_games
        ),
    )
    frozen_diagnostics = tuple(diagnostics)
    records = tuple(record for game in frozen_diagnostics for record in game.records)
    return Phase1EvaluationResult(
        evaluation_result=evaluation,
        game_diagnostics=frozen_diagnostics,
        records=records,
        aggregate=aggregate_phase1(frozen_diagnostics),
        clusters=build_cluster_summaries(records),
    )


def run_phase1_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Phase1EvaluationResult:
    """Replay exact #252 parent population and collect cheap R1-R4 diagnostics."""
    _validate_plan(plan)
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)
    jobs = [
        _Phase1GameJob(
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
    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        game_runner=_run_phase1_game_job,
        progress_callback=progress_callback,
    )
    game_results: list[SingleRoundGameResult] = []
    diagnostics: list[Phase1GameDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "offensive-efficiency Phase 1 failed in a worker",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _Phase1GameJobOutcome):
                raise OffensiveEfficiencyDiagnosticError(
                    "Phase 1 worker returned an unexpected outcome type"
                )
            if outcome.result is None or outcome.diagnostic is None:
                raise OffensiveEfficiencyDiagnosticError(
                    "Phase 1 worker returned incomplete success"
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
    return _build_phase1_result(plan, game_results, diagnostics)


def require_trajectory_identity(
    phase1: Phase1EvaluationResult,
    historical_parent: SingleRoundStrengthArtifact,
) -> None:
    """Require exact replay raw result identity against #252 parent-C artifact."""
    plan = historical_parent.plan
    if plan.candidate_identity != PARENT_IDENTITY:
        raise OffensiveEfficiencyDiagnosticError(
            "historical artifact is not the #252 parent identity"
        )
    if plan.baseline_identity != COMPARATOR_IDENTITY:
        raise OffensiveEfficiencyDiagnosticError(
            "historical artifact does not use the #252 passive comparator"
        )
    if plan.seeds != PHASE_B_SEEDS or plan.max_steps != MAX_STEPS:
        raise OffensiveEfficiencyDiagnosticError(
            "historical artifact population differs from #252 Phase B parent"
        )
    if len(historical_parent.game_results) != PHASE_B_GAMES_PER_ARM:
        raise OffensiveEfficiencyDiagnosticError(
            "historical parent artifact must contain exactly 400 games"
        )
    replay = phase1.evaluation_result.game_results
    if len(replay) != PHASE_B_GAMES_PER_ARM:
        raise OffensiveEfficiencyDiagnosticError("replay must contain exactly 400 games")
    if replay != historical_parent.game_results:
        for current, historical in zip(
            replay, historical_parent.game_results, strict=True
        ):
            if current != historical:
                raise OffensiveEfficiencyDiagnosticError(
                    "TRAJECTORY IDENTITY FAILURE at "
                    f"seed={current.seed} rotation={current.rotation}"
                )
        raise OffensiveEfficiencyDiagnosticError("TRAJECTORY IDENTITY FAILURE")


def select_phase2_samples(
    records: tuple[Phase1DecisionRecord, ...],
    *,
    limit: int = PHASE2_SAMPLE_LIMIT,
) -> tuple[Phase2Sample, ...]:
    """Deterministic shanten-bucket x open/closed round-robin sample."""
    if type(limit) is not int or not 0 <= limit <= PHASE2_SAMPLE_LIMIT:
        raise ValueError(f"limit must be an int in [0, {PHASE2_SAMPLE_LIMIT}]")
    candidates = tuple(
        record
        for record in records
        if record.full_completion_all_zero or record.eligible_completion_all_zero
    )
    if limit == 0 or not candidates:
        return ()
    shanten_order = ("0", "1", "2", "3+")
    open_order = (False, True)
    queues: dict[tuple[str, bool], list[Phase1DecisionRecord]] = {}
    for shanten in shanten_order:
        for open_hand in open_order:
            items = sorted(
                (
                    record
                    for record in candidates
                    if _shanten_bucket(record.selected_post_discard_shanten) == shanten
                    and record.open_hand is open_hand
                ),
                key=lambda record: record.identity,
            )
            if items:
                queues[(shanten, open_hand)] = items
    selected: list[Phase2Sample] = []
    offsets = {key: 0 for key in queues}
    while len(selected) < min(limit, len(candidates)):
        made_progress = False
        for key in tuple(queues):
            offset = offsets[key]
            items = queues[key]
            if offset >= len(items):
                continue
            record = items[offset]
            offsets[key] = offset + 1
            selected.append(
                Phase2Sample(
                    identity=record.identity,
                    shanten_bucket=key[0],
                    open_hand=key[1],
                )
            )
            made_progress = True
            if len(selected) >= min(limit, len(candidates)):
                break
        if not made_progress:
            break
    return tuple(selected)


def _terminal_universe_result(
    analysis: MechanismRiichiDefenseOffensiveEfficiencyAnalysis,
    *,
    baseline_eligible: bool,
) -> TerminalUniverseResult | None:
    summary = (
        analysis.baseline_eligible_terminal_progression_summary
        if baseline_eligible
        else analysis.full_legal_terminal_progression_summary
    )
    if summary is None:
        return None
    candidates = tuple(
        candidate
        for candidate in analysis.candidate_evaluations
        if (not baseline_eligible or candidate.baseline_eligible)
        and candidate.terminal_shanten_mass is not None
    )
    if not candidates:
        raise OffensiveEfficiencyDiagnosticError(
            "R5 summary is present without evaluated candidates"
        )
    best_mass = min(candidate.terminal_shanten_mass for candidate in candidates)
    best = tuple(
        candidate for candidate in candidates if candidate.terminal_shanten_mass == best_mass
    )
    return TerminalUniverseResult(
        selected_terminal_shanten_mass=summary.selected_terminal_shanten_mass,
        best_terminal_shanten_mass=summary.best_terminal_shanten_mass,
        regret_mass=summary.terminal_shanten_regret_mass,
        sequence_denominator=analysis.sequence_denominator,
        best_tie_count=len(best),
        selected_is_best=summary.terminal_shanten_regret_mass == 0,
        best_action_reprs=tuple(repr(candidate.action) for candidate in best),
    )


class _Phase2Recorder:
    __slots__ = ("_targets", "_ordinal", "_records")

    def __init__(self, targets: tuple[Phase1DecisionRecord, ...]) -> None:
        self._targets = {target.identity.decision_ordinal: target for target in targets}
        self._ordinal = 0
        self._records: dict[int, Phase2DecisionRecord] = {}

    def record(self, decision: DecisionContext, action: InternalAction) -> None:
        ordinal = self._ordinal
        self._ordinal += 1
        target = self._targets.get(ordinal)
        if target is None:
            return
        if not isinstance(action, DiscardAction):
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 selected target is no longer a discard decision"
            )
        discard_actions = tuple(
            item for item in decision.legal_actions if isinstance(item, DiscardAction)
        )
        if len(discard_actions) < 2:
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 selected target is no longer a choice discard"
            )
        analysis = analyze_mechanism_riichi_defense_offensive_efficiency(
            decision.input,
            discard_actions,
            include_terminal_progression=True,
        )
        if analysis.baseline_selected_action != action:
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 #172 selection differs from production action"
            )
        current = _phase1_record_from_analysis(
            identity=target.identity,
            candidate_seat=target.candidate_seat,
            decision=decision,
            action=action,
            analysis=analysis,
        )
        comparable_fields = (
            "decision_kind",
            "open_hand",
            "self_riichi",
            "turn_bucket",
            "branch",
            "legal_discard_count",
            "baseline_eligible_discard_count",
            "selected_action_repr",
            "selected_post_discard_shanten",
            "full_shanten_regret",
            "eligible_shanten_regret",
            "full_ukeire_regret",
            "eligible_ukeire_regret",
            "full_second_step_regret",
            "eligible_second_step_regret",
            "full_completion_regret",
            "eligible_completion_regret",
            "full_completion_all_zero",
            "eligible_completion_all_zero",
        )
        if any(getattr(current, name) != getattr(target, name) for name in comparable_fields):
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 replay target differs from Phase 1 decision metadata"
            )
        self._records[ordinal] = Phase2DecisionRecord(
            sample=Phase2Sample(
                target.identity,
                _shanten_bucket(target.selected_post_discard_shanten),
                target.open_hand,
            ),
            full_legal=_terminal_universe_result(analysis, baseline_eligible=False),
            baseline_eligible=_terminal_universe_result(
                analysis, baseline_eligible=True
            ),
        )

    def snapshot(self) -> tuple[Phase2DecisionRecord, ...]:
        missing = set(self._targets) - set(self._records)
        if missing:
            raise OffensiveEfficiencyDiagnosticError(
                f"Phase 2 replay did not encounter target ordinals: {sorted(missing)}"
            )
        return tuple(self._records[index] for index in sorted(self._records))


class _ObservedPhase2Policy:
    __slots__ = ("_policy", "_recorder")

    def __init__(self, policy: Policy, recorder: _Phase2Recorder) -> None:
        self._policy = policy
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        action = execute_policy(self._policy, decision)
        self._recorder.record(decision, action)
        return action


@dataclass(frozen=True, slots=True)
class _Phase2GameJob(GameJob):
    candidate_seat: Seat
    targets: tuple[Phase1DecisionRecord, ...]


@dataclass(frozen=True, slots=True)
class _Phase2GameJobOutcome(GameJobOutcome):
    records: tuple[Phase2DecisionRecord, ...] | None


def _run_phase2_game_job(job: _Phase2GameJob) -> _Phase2GameJobOutcome:
    try:
        policies = _create_policies(job.assignment, seed=job.seed, rotation=job.rotation)
        recorder = _Phase2Recorder(job.targets)
        wrapped = dict(policies)
        wrapped[job.candidate_seat] = _ObservedPhase2Policy(
            policies[job.candidate_seat], recorder
        )
        result = LocalGameRunner(
            wrapped,
            seed=job.seed,
            game_mode=GAME_MODE,
            max_steps=job.max_steps,
        ).run()
        records = recorder.snapshot()
    except Exception:
        return _Phase2GameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=(
                "offensive-efficiency Phase 2 game failed:\n"
                f"{traceback.format_exc()}"
            ),
            records=None,
        )
    return _Phase2GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        records=records,
    )


def run_phase2_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    phase1_records: tuple[Phase1DecisionRecord, ...],
    samples: tuple[Phase2Sample, ...],
    historical_parent: SingleRoundStrengthArtifact,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Phase2DecisionRecord, ...]:
    """Replay only sampled games and execute exact R5 through the #172 seam."""
    _validate_plan(plan)
    validate_max_workers(max_workers)
    if len(samples) > PHASE2_SAMPLE_LIMIT:
        raise OffensiveEfficiencyDiagnosticError("Phase 2 sample exceeds 64 decisions")
    expected_samples = select_phase2_samples(phase1_records)
    if samples != expected_samples:
        raise OffensiveEfficiencyDiagnosticError(
            "Phase 2 samples differ from the deterministic selection rule"
        )
    by_identity = {record.identity: record for record in phase1_records}
    targets_by_game: dict[tuple[int, int], list[Phase1DecisionRecord]] = defaultdict(list)
    for sample in samples:
        try:
            target = by_identity[sample.identity]
        except KeyError as exc:
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 sample identity is absent from Phase 1 records"
            ) from exc
        targets_by_game[(sample.identity.seed, sample.identity.rotation)].append(target)
    historical_by_game = {
        (item.seed, item.rotation): item for item in historical_parent.game_results
    }
    jobs = [
        _Phase2GameJob(
            seed=seed,
            rotation=rotation,
            assignment=_seat_assignment(plan, rotation),
            game_mode=GAME_MODE,
            max_steps=plan.max_steps,
            candidate_seat=Seat(rotation),
            targets=tuple(sorted(targets, key=lambda item: item.identity)),
        )
        for (seed, rotation), targets in sorted(targets_by_game.items())
    ]
    if not jobs:
        return ()
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)
    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        game_runner=_run_phase2_game_job,
        progress_callback=progress_callback,
    )
    found: dict[DecisionIdentity, Phase2DecisionRecord] = {}
    for job in jobs:
        outcome = outcomes[(job.seed, job.rotation)]
        if outcome.error_text is not None:
            raise SingleRoundEvaluationError(
                "offensive-efficiency Phase 2 failed in a worker",
                seed=job.seed,
                rotation=job.rotation,
            ) from RuntimeError(outcome.error_text)
        if not isinstance(outcome, _Phase2GameJobOutcome):
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 worker returned an unexpected outcome type"
            )
        if outcome.result is None or outcome.records is None:
            raise OffensiveEfficiencyDiagnosticError(
                "Phase 2 worker returned incomplete success"
            )
        game_result = _build_game_result(
            outcome.result,
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=Seat(job.rotation),
        )
        if game_result != historical_by_game[(job.seed, job.rotation)]:
            raise OffensiveEfficiencyDiagnosticError(
                "TRAJECTORY IDENTITY FAILURE during Phase 2 replay at "
                f"seed={job.seed} rotation={job.rotation}"
            )
        for record in outcome.records:
            found[record.sample.identity] = record
    return tuple(found[sample.identity] for sample in samples)


def _phase2_universe_aggregate(
    records: tuple[Phase2DecisionRecord, ...], universe: str
) -> Phase2UniverseAggregate:
    values: list[TerminalUniverseResult] = []
    for record in records:
        result = record.full_legal if universe == "FULL_LEGAL" else record.baseline_eligible
        if result is not None:
            values.append(result)
    regrets = sorted(item.expected_regret for item in values)
    if regrets:
        p90 = regrets[max(0, math.ceil(0.9 * len(regrets)) - 1)]
        mean_value = sum(regrets) / len(regrets)
        median_value = float(median(regrets))
        max_value = regrets[-1]
    else:
        p90 = mean_value = median_value = max_value = None
    nonzero = sum(item.regret_mass != 0 for item in values)
    return Phase2UniverseAggregate(
        universe=universe,
        applicable_count=len(values),
        selected_best_count=sum(item.selected_is_best for item in values),
        nonzero_count=nonzero,
        nonzero_incidence=0.0 if not values else nonzero / len(values),
        mean_expected_regret=mean_value,
        median_expected_regret=median_value,
        p90_expected_regret=p90,
        max_expected_regret=max_value,
    )


def aggregate_phase2(
    records: tuple[Phase2DecisionRecord, ...],
) -> Phase2Aggregate:
    return Phase2Aggregate(
        sample_count=len(records),
        universe_summaries=tuple(
            _phase2_universe_aggregate(records, universe) for universe in UNIVERSES
        ),
    )


__all__ = [
    "ClusterSummary",
    "DecisionIdentity",
    "DecisionKind",
    "DistributionSummary",
    "METRICS",
    "MetricSummary",
    "OffensiveEfficiencyDiagnosticError",
    "PHASE2_SAMPLE_LIMIT",
    "Phase1Aggregate",
    "Phase1DecisionRecord",
    "Phase1EvaluationResult",
    "Phase1GameDiagnostics",
    "Phase2Aggregate",
    "Phase2DecisionRecord",
    "Phase2Sample",
    "Phase2UniverseAggregate",
    "TerminalUniverseResult",
    "TurnBucket",
    "UNIVERSES",
    "aggregate_phase1",
    "aggregate_phase2",
    "build_cluster_summaries",
    "build_phase1_plan",
    "metric_value",
    "require_trajectory_identity",
    "run_phase1_parallel",
    "run_phase2_parallel",
    "select_phase2_samples",
]
