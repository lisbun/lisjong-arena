"""Issue #196専用のopen-hand call decision divergence diagnostics。

既存ABBB strength evaluationのcandidate seatだけを、actual candidateとexact
baseline shadowを同じ``DecisionContext``で比較する薄いPolicy wrapperで観測する。
runnerへ返すのは常にactual candidate actionであり、shadow actionを外部Actionへ
mappingしたりgame progressionへ適用したりしない。

これは``OpenHandYakuAwareCallPolicy``対``yakuhai-call``専用のdiagnosticであり、
generic shadow-policy / observability frameworkではない。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lisjong.policy_contract import (
    ChiAction,
    DecisionContext,
    InternalAction,
    PassAction,
    Policy,
    PonAction,
    Seat,
    execute_policy,
)

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
    GAME_MODE,
    ROTATION_COUNT,
    SingleRoundEvaluationError,
    _build_game_result,
    _create_policies,
    _seat_assignment,
    aggregate_candidate_metrics,
    scaled_candidate_game_delta,
)

OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY = "open-hand-yaku-aware-call"
OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY = "yakuhai-call"
OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION = "fd9d87efd7c0563b990320f3cc12495aed418be4"


@dataclass(frozen=True, slots=True)
class OpenHandGameDiagnostics:
    """1 gameのcandidate-seat decision diagnosticsとoutcome sign source。"""

    seed: int
    rotation: int
    candidate_seat: Seat
    total_candidate_seat_decisions: int
    same_action_decisions: int
    divergent_action_decisions: int
    shared_prefix_initial_call_opportunities: int
    baseline_pass_to_candidate_chi: int
    baseline_pass_to_candidate_pon: int
    scaled_candidate_score_delta: int

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if type(self.rotation) is not int:
            raise TypeError("rotation must be an int")
        if not 0 <= self.rotation < ROTATION_COUNT:
            raise ValueError("rotation must be in range(4)")
        if not isinstance(self.candidate_seat, Seat):
            raise TypeError("candidate_seat must be a Seat")
        if self.candidate_seat != Seat(self.rotation):
            raise ValueError("candidate_seat must match rotation")
        count_names = (
            "total_candidate_seat_decisions",
            "same_action_decisions",
            "divergent_action_decisions",
            "shared_prefix_initial_call_opportunities",
            "baseline_pass_to_candidate_chi",
            "baseline_pass_to_candidate_pon",
        )
        for name in count_names:
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if type(self.scaled_candidate_score_delta) is not int:
            raise TypeError("scaled_candidate_score_delta must be an int")
        if (
            self.same_action_decisions + self.divergent_action_decisions
            != self.total_candidate_seat_decisions
        ):
            raise ValueError(
                "same and divergent decisions must partition total decisions"
            )
        if (
            self.shared_prefix_initial_call_opportunities
            > self.total_candidate_seat_decisions
        ):
            raise ValueError(
                "shared-prefix opportunities must not exceed total decisions"
            )
        accepted_calls = (
            self.baseline_pass_to_candidate_chi + self.baseline_pass_to_candidate_pon
        )
        if accepted_calls > self.divergent_action_decisions:
            raise ValueError("accepted calls must be divergent decisions")

    @property
    def candidate_only_chi_count(self) -> int:
        return self.baseline_pass_to_candidate_chi

    @property
    def candidate_only_pon_count(self) -> int:
        return self.baseline_pass_to_candidate_pon


@dataclass(frozen=True, slots=True)
class OpenHandDiagnosticSummary:
    """全gameをcanonical順で集計したpurpose-specific diagnostics。"""

    game_count: int
    seed_block_count: int
    total_candidate_seat_decisions: int
    same_action_decisions: int
    divergent_action_decisions: int
    action_divergence_rate: float
    shared_prefix_initial_call_opportunities: int
    baseline_pass_to_candidate_chi: int
    baseline_pass_to_candidate_pon: int
    candidate_only_chi_count: int
    candidate_only_pon_count: int
    divergent_game_count: int
    divergent_seed_block_count: int
    nonzero_score_delta_seed_block_count: int
    divergent_positive_score_delta_seed_blocks: int
    divergent_zero_score_delta_seed_blocks: int
    divergent_negative_score_delta_seed_blocks: int
    mean_divergences_per_divergent_game: float | None
    divergent_decisions_by_candidate_seat: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        int_fields = (
            "game_count",
            "seed_block_count",
            "total_candidate_seat_decisions",
            "same_action_decisions",
            "divergent_action_decisions",
            "shared_prefix_initial_call_opportunities",
            "baseline_pass_to_candidate_chi",
            "baseline_pass_to_candidate_pon",
            "candidate_only_chi_count",
            "candidate_only_pon_count",
            "divergent_game_count",
            "divergent_seed_block_count",
            "nonzero_score_delta_seed_block_count",
            "divergent_positive_score_delta_seed_blocks",
            "divergent_zero_score_delta_seed_blocks",
            "divergent_negative_score_delta_seed_blocks",
        )
        for name in int_fields:
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if type(self.action_divergence_rate) is not float:
            raise TypeError("action_divergence_rate must be a float")
        if not 0.0 <= self.action_divergence_rate <= 1.0:
            raise ValueError("action_divergence_rate must be in [0, 1]")
        if (
            self.mean_divergences_per_divergent_game is not None
            and type(self.mean_divergences_per_divergent_game) is not float
        ):
            raise TypeError(
                "mean_divergences_per_divergent_game must be a float or None"
            )
        seats = tuple(self.divergent_decisions_by_candidate_seat)
        if len(seats) != len(Seat) or any(type(value) is not int for value in seats):
            raise TypeError(
                "divergent_decisions_by_candidate_seat must contain four ints"
            )
        if any(value < 0 for value in seats):
            raise ValueError(
                "divergent_decisions_by_candidate_seat must be non-negative"
            )
        object.__setattr__(self, "divergent_decisions_by_candidate_seat", seats)
        if self.same_action_decisions + self.divergent_action_decisions != (
            self.total_candidate_seat_decisions
        ):
            raise ValueError(
                "same and divergent decisions must partition total decisions"
            )
        expected_rate = (
            0.0
            if self.total_candidate_seat_decisions == 0
            else self.divergent_action_decisions / self.total_candidate_seat_decisions
        )
        if self.action_divergence_rate != expected_rate:
            raise ValueError("action_divergence_rate is inconsistent with counts")
        if self.candidate_only_chi_count != self.baseline_pass_to_candidate_chi:
            raise ValueError("candidate-only Chi count must equal Pass -> Chi count")
        if self.candidate_only_pon_count != self.baseline_pass_to_candidate_pon:
            raise ValueError("candidate-only Pon count must equal Pass -> Pon count")
        divergent_block_outcomes = (
            self.divergent_positive_score_delta_seed_blocks
            + self.divergent_zero_score_delta_seed_blocks
            + self.divergent_negative_score_delta_seed_blocks
        )
        if divergent_block_outcomes != self.divergent_seed_block_count:
            raise ValueError(
                "divergent block outcomes must partition divergent seed blocks"
            )
        if sum(seats) != self.divergent_action_decisions:
            raise ValueError("candidate seat counts must sum to divergent decisions")
        expected_mean = (
            None
            if self.divergent_game_count == 0
            else self.divergent_action_decisions / self.divergent_game_count
        )
        if self.mean_divergences_per_divergent_game != expected_mean:
            raise ValueError("mean divergences per divergent game is inconsistent")


def aggregate_open_hand_diagnostics(
    games: tuple[OpenHandGameDiagnostics, ...],
) -> OpenHandDiagnosticSummary:
    """canonical game diagnosticsからdecision/game/seed coverageを集計する。"""
    if not games:
        raise ValueError("games must not be empty")
    seeds: list[int] = []
    for game in games:
        if game.seed not in seeds:
            seeds.append(game.seed)
    divergent_seeds = {
        game.seed for game in games if game.divergent_action_decisions > 0
    }
    scaled_delta_by_seed = {
        seed: sum(
            game.scaled_candidate_score_delta for game in games if game.seed == seed
        )
        for seed in seeds
    }
    divergent_decisions = sum(game.divergent_action_decisions for game in games)
    divergent_games = sum(game.divergent_action_decisions > 0 for game in games)
    by_seat = tuple(
        sum(
            game.divergent_action_decisions
            for game in games
            if game.candidate_seat == seat
        )
        for seat in Seat
    )
    total_decisions = sum(game.total_candidate_seat_decisions for game in games)
    return OpenHandDiagnosticSummary(
        game_count=len(games),
        seed_block_count=len(seeds),
        total_candidate_seat_decisions=total_decisions,
        same_action_decisions=sum(game.same_action_decisions for game in games),
        divergent_action_decisions=divergent_decisions,
        action_divergence_rate=(
            0.0 if total_decisions == 0 else divergent_decisions / total_decisions
        ),
        shared_prefix_initial_call_opportunities=sum(
            game.shared_prefix_initial_call_opportunities for game in games
        ),
        baseline_pass_to_candidate_chi=sum(
            game.baseline_pass_to_candidate_chi for game in games
        ),
        baseline_pass_to_candidate_pon=sum(
            game.baseline_pass_to_candidate_pon for game in games
        ),
        candidate_only_chi_count=sum(game.candidate_only_chi_count for game in games),
        candidate_only_pon_count=sum(game.candidate_only_pon_count for game in games),
        divergent_game_count=divergent_games,
        divergent_seed_block_count=len(divergent_seeds),
        nonzero_score_delta_seed_block_count=sum(
            value != 0 for value in scaled_delta_by_seed.values()
        ),
        divergent_positive_score_delta_seed_blocks=sum(
            scaled_delta_by_seed[seed] > 0 for seed in divergent_seeds
        ),
        divergent_zero_score_delta_seed_blocks=sum(
            scaled_delta_by_seed[seed] == 0 for seed in divergent_seeds
        ),
        divergent_negative_score_delta_seed_blocks=sum(
            scaled_delta_by_seed[seed] < 0 for seed in divergent_seeds
        ),
        mean_divergences_per_divergent_game=(
            None if divergent_games == 0 else divergent_decisions / divergent_games
        ),
        divergent_decisions_by_candidate_seat=by_seat,
    )


@dataclass(frozen=True, slots=True)
class OpenHandDiagnosticEvaluationResult:
    """既存strength resultと、それへ順序・outcomeをbindしたdiagnostics。"""

    evaluation_result: SingleRoundEvaluationResult
    game_diagnostics: tuple[OpenHandGameDiagnostics, ...]
    summary: OpenHandDiagnosticSummary

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_result, SingleRoundEvaluationResult):
            raise TypeError("evaluation_result must be a SingleRoundEvaluationResult")
        games = tuple(self.game_diagnostics)
        if any(not isinstance(game, OpenHandGameDiagnostics) for game in games):
            raise TypeError(
                "game_diagnostics must contain only OpenHandGameDiagnostics"
            )
        object.__setattr__(self, "game_diagnostics", games)
        raw_results = self.evaluation_result.game_results
        if len(games) != len(raw_results):
            raise ValueError("game diagnostics count must match game_results")
        for diagnostic, game_result in zip(games, raw_results, strict=True):
            if (
                diagnostic.seed,
                diagnostic.rotation,
                diagnostic.candidate_seat,
            ) != (game_result.seed, game_result.rotation, game_result.candidate_seat):
                raise ValueError("game diagnostics order must match game_results")
            if diagnostic.scaled_candidate_score_delta != (
                scaled_candidate_game_delta(game_result)
            ):
                raise ValueError("diagnostic score delta must match game_result")
        if self.summary != aggregate_open_hand_diagnostics(games):
            raise ValueError("summary must match canonical diagnostic aggregation")


class _DecisionRecorder:
    __slots__ = (
        "baseline_pass_to_candidate_chi",
        "baseline_pass_to_candidate_pon",
        "divergent_action_decisions",
        "same_action_decisions",
        "shared_prefix_initial_call_opportunities",
        "total_candidate_seat_decisions",
        "_shared_prefix",
    )

    def __init__(self) -> None:
        self.total_candidate_seat_decisions = 0
        self.same_action_decisions = 0
        self.divergent_action_decisions = 0
        self.shared_prefix_initial_call_opportunities = 0
        self.baseline_pass_to_candidate_chi = 0
        self.baseline_pass_to_candidate_pon = 0
        self._shared_prefix = True

    def record(
        self,
        decision: DecisionContext,
        candidate_action: InternalAction,
        baseline_action: InternalAction,
    ) -> None:
        self.total_candidate_seat_decisions += 1
        if candidate_action == baseline_action:
            self.same_action_decisions += 1
        else:
            self.divergent_action_decisions += 1
        baseline_pass = isinstance(baseline_action, PassAction)
        legal_call = any(
            isinstance(action, (ChiAction, PonAction))
            for action in decision.legal_actions
        )
        # このdecision自体は比較開始時点でshared prefix上にある。opportunityを
        # 数えてからprefixを閉じ、first divergenceだけはdenominatorへ含める。
        if self._shared_prefix and baseline_pass and legal_call:
            self.shared_prefix_initial_call_opportunities += 1
        if baseline_pass and isinstance(candidate_action, ChiAction):
            self.baseline_pass_to_candidate_chi += 1
        if baseline_pass and isinstance(candidate_action, PonAction):
            self.baseline_pass_to_candidate_pon += 1
        if candidate_action != baseline_action:
            self._shared_prefix = False

    def snapshot(
        self,
        *,
        seed: int,
        rotation: int,
        candidate_seat: Seat,
        score_delta: int,
    ) -> OpenHandGameDiagnostics:
        return OpenHandGameDiagnostics(
            seed=seed,
            rotation=rotation,
            candidate_seat=candidate_seat,
            total_candidate_seat_decisions=self.total_candidate_seat_decisions,
            same_action_decisions=self.same_action_decisions,
            divergent_action_decisions=self.divergent_action_decisions,
            shared_prefix_initial_call_opportunities=(
                self.shared_prefix_initial_call_opportunities
            ),
            baseline_pass_to_candidate_chi=self.baseline_pass_to_candidate_chi,
            baseline_pass_to_candidate_pon=self.baseline_pass_to_candidate_pon,
            scaled_candidate_score_delta=score_delta,
        )


class _ShadowComparedCandidatePolicy:
    """actual candidateを返し、exact baseline actionをread-only記録する。"""

    __slots__ = ("_baseline", "_candidate", "_recorder")

    def __init__(
        self,
        candidate: Policy,
        baseline: Policy,
        recorder: _DecisionRecorder,
    ) -> None:
        self._candidate = candidate
        self._baseline = baseline
        self._recorder = recorder

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        candidate_action = execute_policy(self._candidate, decision)
        baseline_action = execute_policy(self._baseline, decision)
        self._recorder.record(decision, candidate_action, baseline_action)
        return candidate_action


def _run_diagnostic_single_game(
    policies: Mapping[Seat, Policy],
    shadow_baseline: Policy,
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
    max_steps: int,
) -> tuple[LocalGameResult, _DecisionRecorder]:
    """candidate seatだけへpurpose-specific wrapperを適用して1 game実行する。"""
    recorder = _DecisionRecorder()
    wrapped = dict(policies)
    wrapped[candidate_seat] = _ShadowComparedCandidatePolicy(
        policies[candidate_seat], shadow_baseline, recorder
    )
    result = LocalGameRunner(
        wrapped,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
    ).run()
    return result, recorder


@dataclass(frozen=True, slots=True)
class _DiagnosticGameJob(GameJob):
    candidate_seat: Seat
    shadow_baseline: PolicySpec


@dataclass(frozen=True, slots=True)
class _DiagnosticGameJobOutcome(GameJobOutcome):
    diagnostic: OpenHandGameDiagnostics | None


def _run_diagnostic_game_job(job: _DiagnosticGameJob) -> _DiagnosticGameJobOutcome:
    """spawn worker内でfresh actual/shadow Policiesを生成して1 game実行する。"""
    try:
        policies = _create_policies(
            job.assignment, seed=job.seed, rotation=job.rotation
        )
        shadow_baseline = job.shadow_baseline.factory()
        result, recorder = _run_diagnostic_single_game(
            policies,
            shadow_baseline,
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=job.candidate_seat,
            max_steps=job.max_steps,
        )
        game_result = _build_game_result(
            result,
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=job.candidate_seat,
        )
        diagnostic = recorder.snapshot(
            seed=job.seed,
            rotation=job.rotation,
            candidate_seat=job.candidate_seat,
            score_delta=scaled_candidate_game_delta(game_result),
        )
    except Exception:
        return _DiagnosticGameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=(
                "open-hand diagnostic single game execution failed:\n"
                f"{traceback.format_exc()}"
            ),
            diagnostic=None,
        )
    return _DiagnosticGameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        diagnostic=diagnostic,
    )


def _validate_plan(plan: SingleRoundEvaluationPlan) -> None:
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")
    if plan.candidate.identity != OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY:
        raise ValueError(
            "open-hand diagnostics require candidate identity "
            f"{OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY!r}"
        )
    if plan.baseline.identity != OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY:
        raise ValueError(
            "open-hand diagnostics require baseline identity "
            f"{OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY!r}"
        )


def _build_evaluation_result(
    plan: SingleRoundEvaluationPlan,
    game_results: list[SingleRoundGameResult],
    game_diagnostics: list[OpenHandGameDiagnostics],
) -> OpenHandDiagnosticEvaluationResult:
    frozen_results = tuple(game_results)
    frozen_diagnostics = tuple(game_diagnostics)
    expected_count = ROTATION_COUNT * len(plan.seeds)
    if (
        len(frozen_results) != expected_count
        or len(frozen_diagnostics) != expected_count
    ):
        raise SingleRoundEvaluationError(
            "diagnostic evaluation did not produce the expected game count",
            seed=plan.seeds[-1],
            rotation=ROTATION_COUNT - 1,
        )
    evaluation_result = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_results
        ),
    )
    return OpenHandDiagnosticEvaluationResult(
        evaluation_result=evaluation_result,
        game_diagnostics=frozen_diagnostics,
        summary=aggregate_open_hand_diagnostics(frozen_diagnostics),
    )


def run_open_hand_diagnostic_evaluation(
    plan: SingleRoundEvaluationPlan,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> OpenHandDiagnosticEvaluationResult:
    """既存ABBB serial順序を保ってpurpose-specific diagnosticsを収集する。"""
    _validate_plan(plan)
    total = ROTATION_COUNT * len(plan.seeds)
    completed = 0
    game_results: list[SingleRoundGameResult] = []
    game_diagnostics: list[OpenHandGameDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _seat_assignment(plan, rotation)
            candidate_seat = Seat(rotation)
            policies = _create_policies(assignment, seed=seed, rotation=rotation)
            try:
                shadow_baseline = plan.baseline.factory()
                result, recorder = _run_diagnostic_single_game(
                    policies,
                    shadow_baseline,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=candidate_seat,
                    max_steps=plan.max_steps,
                )
                game_result = _build_game_result(
                    result,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=candidate_seat,
                )
            except Exception as exc:
                raise SingleRoundEvaluationError(
                    "diagnostic single game execution failed",
                    seed=seed,
                    rotation=rotation,
                ) from exc
            game_results.append(game_result)
            game_diagnostics.append(
                recorder.snapshot(
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=candidate_seat,
                    score_delta=scaled_candidate_game_delta(game_result),
                )
            )
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)
    return _build_evaluation_result(plan, game_results, game_diagnostics)


def run_open_hand_diagnostic_evaluation_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> OpenHandDiagnosticEvaluationResult:
    """既存process poolを使い、結果をseed入力順・rotation順へcanonicalizeする。"""
    _validate_plan(plan)
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)
    jobs = [
        _DiagnosticGameJob(
            seed=seed,
            rotation=rotation,
            assignment=_seat_assignment(plan, rotation),
            game_mode=GAME_MODE,
            max_steps=plan.max_steps,
            candidate_seat=Seat(rotation),
            shadow_baseline=plan.baseline,
        )
        for seed in plan.seeds
        for rotation in range(ROTATION_COUNT)
    ]
    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        game_runner=_run_diagnostic_game_job,
        progress_callback=progress_callback,
    )
    game_results: list[SingleRoundGameResult] = []
    game_diagnostics: list[OpenHandGameDiagnostics] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "diagnostic single game execution failed in a worker process",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            if not isinstance(outcome, _DiagnosticGameJobOutcome):
                raise SingleRoundEvaluationError(
                    "diagnostic worker returned an unexpected outcome",
                    seed=seed,
                    rotation=rotation,
                )
            if outcome.result is None or outcome.diagnostic is None:
                raise SingleRoundEvaluationError(
                    "diagnostic worker returned an incomplete success outcome",
                    seed=seed,
                    rotation=rotation,
                )
            game_result = _build_game_result(
                outcome.result,
                seed=seed,
                rotation=rotation,
                candidate_seat=Seat(rotation),
            )
            game_results.append(game_result)
            game_diagnostics.append(outcome.diagnostic)
    return _build_evaluation_result(plan, game_results, game_diagnostics)


__all__ = [
    "OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY",
    "OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY",
    "OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION",
    "OpenHandDiagnosticEvaluationResult",
    "OpenHandDiagnosticSummary",
    "OpenHandGameDiagnostics",
    "aggregate_open_hand_diagnostics",
    "run_open_hand_diagnostic_evaluation",
    "run_open_hand_diagnostic_evaluation_parallel",
]
