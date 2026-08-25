"""fixed seedとABBB seat rotationによるcandidate single-round評価の実行。

既存``lisjong_arena.comparison``のAABB comparisonとは意味が異なるprotocolで
あるため、独立したPlan / Result契約とexecution経路として持つ。candidate 1体
とbaseline 1体を固定seedごとに``[A, B, B, B]`` -> ``[B, A, B, B]`` ->
``[B, B, A, B]`` -> ``[B, B, B, A]``へrotationし、各gameをArena-localの
``lisjong_arena.riichienv.local_game_runner.LocalGameRunner``で
``game_mode="4p-red-single"``として実行する。serial実行は
``run_single_round_evaluation()``、``(seed, rotation)``単位のlocal process
並列実行は``run_single_round_evaluation_parallel()``が担い、どちらも同じ
result / aggregation契約を使う。

``4p-red-single``はこのprotocol自身のinvariantであり、``ComparisonPlan``の
genericな``game_mode``のようにcallerが指定できるoptionではない。実行経路は

    lisjong-arena evaluation -> lisjong-arena riichienv.LocalGameRunner
        -> RiichiEnv (+ Arena-local RiichiEnv Adapter + Arena-local GameTrace)

であり、このmoduleは``riichienv``をimportしない。
"""

from collections.abc import Callable, Mapping

from lisjong.policy_contract import Policy, Seat

from lisjong_arena._parallel_execution import (
    GameJob,
    PolicyFactoryNotSerializableError,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundCandidateMahjongMetrics,
    SingleRoundCandidateMetrics,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner

ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
"""1 seedあたりのcandidate seat rotation数。"""

GAME_MODE = SINGLE_ROUND_GAME_MODE
"""single-round評価protocolが所有する固定game mode。callerから変更できない。"""


class SingleRoundEvaluationError(Exception):
    """single-round評価中の1 gameが失敗した場合。

    candidate / baseline factory、Policy execution、``LocalGameRunner``、
    結果の不整合いずれの失敗でも、成功したgameだけを集めたpartialな
    ``SingleRoundEvaluationResult``は返さず評価全体を失敗させる。失敗した
    gameを特定できるよう``seed``と``rotation``を保持する。
    """

    __slots__ = ("rotation", "seed")

    def __init__(self, message: str, *, seed: int, rotation: int) -> None:
        super().__init__(f"seed={seed} rotation={rotation}: {message}")
        self.seed = seed
        self.rotation = rotation


def _seat_assignment(
    plan: SingleRoundEvaluationPlan,
    rotation: int,
) -> tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec]:
    """rotation indexに対応するSeat 0..3のPolicySpec割り当てを返す。

    candidateはrotation番目のseatを担当し、残り3 seatはbaselineが担当する。

        rotation 0: [A, B, B, B]
        rotation 1: [B, A, B, B]
        rotation 2: [B, B, A, B]
        rotation 3: [B, B, B, A]

    既存AABB comparisonの``A/Bを同数seatずつ担当させる``rotationとは異なり、
    candidateだけを4 seatへ公平にrotationする。
    """
    return tuple(
        plan.candidate if seat == rotation else plan.baseline for seat in range(4)
    )


def _run_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    max_steps: int,
) -> LocalGameResult:
    """1局を既存の``LocalGameRunner``で``4p-red-single``として実行する。

    Arenaにおける単一game実行境界はこの1関数だけであり、unit testはここを
    差し替えて実RiichiEnvを起動せずにrotation / raw result / metricsを検証する。
    """
    return LocalGameRunner(
        policies,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
    ).run()


def _create_policies(
    assignment: tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec],
    *,
    seed: int,
    rotation: int,
) -> dict[Seat, Policy]:
    """各seatごとにfactoryを呼び、新しいPolicy instanceを1つずつ生成する。

    baseline 3 seat間でもinstanceを共有せず、rotationやseedをまたいでも
    再利用しない。
    """
    policies = {}
    for seat in Seat:
        spec = assignment[seat]
        try:
            policies[seat] = spec.factory()
        except Exception as exc:
            raise SingleRoundEvaluationError(
                f"policy factory for identity {spec.identity!r} failed at {seat!r}",
                seed=seed,
                rotation=rotation,
            ) from exc
    return policies


def _build_game_result(
    result: LocalGameResult,
    *,
    seed: int,
    rotation: int,
    candidate_seat: Seat,
) -> SingleRoundGameResult:
    """1局の結果をrotation単位のraw recordへ変換する。

    要求した条件（seed、``4p-red-single``）と異なる結果をfail closedする。
    ``scores``の型/件数不正は``LocalGameResult``自体がすでに検証している。
    """
    if result.seed != seed or result.game_mode != GAME_MODE:
        raise SingleRoundEvaluationError(
            "LocalGameRunner returned a result for different conditions "
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


def _aggregate_candidate_mahjong_metrics(
    game_results: tuple[SingleRoundGameResult, ...],
) -> SingleRoundCandidateMahjongMetrics:
    """raw candidate ``SeatRoundStats``の列からIssue #61の7 metricsを集計する。

    candidateのraw factだけを対象にし、baselineのstatsはここでは使わない。
    母数が0の指標は``0.0``ではなく``None``にする。
    """
    round_count = len(game_results)
    candidate_stats = [
        game_result.candidate_round_stats for game_result in game_results
    ]

    score_deltas = [stats.score_delta for stats in candidate_stats]
    mean_round_score_delta = sum(score_deltas) / round_count

    win_points = [stats.win_points for stats in candidate_stats if stats.won]
    win_count = len(win_points)
    mean_win_points = None if win_count == 0 else sum(win_points) / win_count

    deal_in_losses = [stats.deal_in_loss for stats in candidate_stats if stats.dealt_in]
    deal_in_count = len(deal_in_losses)
    mean_deal_in_loss = (
        None if deal_in_count == 0 else sum(deal_in_losses) / deal_in_count
    )

    exhaustive_draw_stats = [
        stats for stats in candidate_stats if stats.exhaustive_draw
    ]
    exhaustive_draw_count = len(exhaustive_draw_stats)
    exhaustive_draw_tenpai_count = sum(
        1 for stats in exhaustive_draw_stats if stats.tenpai_at_exhaustive_draw
    )
    exhaustive_draw_tenpai_rate = (
        None
        if exhaustive_draw_count == 0
        else exhaustive_draw_tenpai_count / exhaustive_draw_count
    )

    first_tenpai_turns = [
        stats.first_tenpai_turn
        for stats in candidate_stats
        if stats.first_tenpai_turn is not None
    ]
    tenpai_reached_count = len(first_tenpai_turns)
    mean_first_tenpai_turn = (
        None
        if tenpai_reached_count == 0
        else sum(first_tenpai_turns) / tenpai_reached_count
    )

    return SingleRoundCandidateMahjongMetrics(
        round_count=round_count,
        mean_round_score_delta=mean_round_score_delta,
        win_count=win_count,
        win_rate=win_count / round_count,
        mean_win_points=mean_win_points,
        deal_in_count=deal_in_count,
        deal_in_rate=deal_in_count / round_count,
        mean_deal_in_loss=mean_deal_in_loss,
        exhaustive_draw_count=exhaustive_draw_count,
        exhaustive_draw_tenpai_count=exhaustive_draw_tenpai_count,
        exhaustive_draw_tenpai_rate=exhaustive_draw_tenpai_rate,
        tenpai_reached_count=tenpai_reached_count,
        mean_first_tenpai_turn=mean_first_tenpai_turn,
    )


def aggregate_candidate_metrics(
    identity: str,
    game_results: tuple[SingleRoundGameResult, ...],
) -> SingleRoundCandidateMetrics:
    """raw game resultからcandidateのmetricsを集計する。

    ``mean_candidate_score``は全gameのcandidate final scoreの平均、
    ``seat_mean_scores``はcandidateがそのseatを担当した時のfinal score平均
    である。``mahjong_metrics``はIssue #61の局単位Mahjong metricsであり、
    candidateのraw ``SeatRoundStats``だけから集計する(baseline statsは
    ``game_results``に残るが、ここでは使わない)。
    """
    if not game_results:
        raise ValueError("game_results must not be empty")

    scores_by_seat: dict[Seat, list[int]] = {seat: [] for seat in Seat}
    for game_result in game_results:
        scores_by_seat[game_result.candidate_seat].append(game_result.candidate_score)

    for seat in Seat:
        if not scores_by_seat[seat]:
            raise ValueError(f"candidate has no game results for {seat!r}")

    all_scores = [game_result.candidate_score for game_result in game_results]
    seat_mean_scores = tuple(
        sum(scores_by_seat[seat]) / len(scores_by_seat[seat]) for seat in Seat
    )

    return SingleRoundCandidateMetrics(
        candidate_identity=identity,
        game_count=len(game_results),
        mean_candidate_score=sum(all_scores) / len(all_scores),
        seat_mean_scores=seat_mean_scores,
        mahjong_metrics=_aggregate_candidate_mahjong_metrics(game_results),
    )


def run_single_round_evaluation(
    plan: SingleRoundEvaluationPlan,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SingleRoundEvaluationResult:
    """``SingleRoundEvaluationPlan``に従ってcandidate single-round評価を実行する。

    実行順序は``seed入力順 -> rotation 0..3``で決定的である。seed数をNとする
    と、total gamesは4N、candidateは各seatをちょうどN回担当する。

    ``progress_callback``は成功した1 gameをraw resultへ追加するたびに
    ``(completed, total)``で呼ぶoptional notificationであり、resultやmetricsの
    semantic dataには含めない。未指定callerの挙動は変更しない。

    途中で1 gameでも失敗した場合は``SingleRoundEvaluationError``を送出し、
    成功したgameだけのpartialな結果を返さない。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")

    total = ROTATION_COUNT * len(plan.seeds)
    completed = 0
    game_results: list[SingleRoundGameResult] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _seat_assignment(plan, rotation)
            candidate_seat = Seat(rotation)
            policies = _create_policies(assignment, seed=seed, rotation=rotation)
            try:
                result = _run_single_game(
                    policies,
                    seed=seed,
                    max_steps=plan.max_steps,
                )
            except Exception as exc:
                raise SingleRoundEvaluationError(
                    "single game execution failed",
                    seed=seed,
                    rotation=rotation,
                ) from exc
            game_results.append(
                _build_game_result(
                    result,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=candidate_seat,
                )
            )
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

    frozen_results = tuple(game_results)
    expected_count = ROTATION_COUNT * len(plan.seeds)
    if len(frozen_results) != expected_count:
        raise SingleRoundEvaluationError(
            f"expected {expected_count} raw game results but got {len(frozen_results)}",
            seed=plan.seeds[-1],
            rotation=ROTATION_COUNT - 1,
        )

    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_results
        ),
    )


def run_single_round_evaluation_parallel(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SingleRoundEvaluationResult:
    """``run_single_round_evaluation()``と同一semanticsで、local process poolを
    使って並列実行する。

    parallelization unitは``(seed, rotation)``の1 gameであり、1 seedの4
    rotationを1 workerへまとめて渡すことはしない。workerの完了順序に関わらず、
    最終raw resultは``run_single_round_evaluation()``と同じ``seed入力順 ->
    rotation 0..3``へcanonicalizeする。Policy instanceは各job・各seatについて
    worker process内部でfactoryからfresh生成し、parent processでは生成しない。

    ``progress_callback``はparent processが成功outcomeを回収するたびに
    ``(completed, total)``で呼ぶoptional notificationである。completion順序は
    result canonicalizationへ使わず、worker processからcallbackを呼ばない。

    ``plan.candidate`` / ``plan.baseline``の``factory``は、spawn worker
    processからimport可能なtop-level callableでなければならない。lambdaや
    local closureのような、process間serialize不能なfactoryはsilentにserial
    実行へfallbackせず``PolicyFactoryNotSerializableError``でfail closedする
    （``run_single_round_evaluation()``自体のfactory contractはこの制約で
    狭めない）。

    途中で1 gameでも失敗した場合は``run_single_round_evaluation()``と同様に
    ``SingleRoundEvaluationError``を送出し、成功したgameだけのpartialな結果は
    返さない。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")
    validate_max_workers(max_workers)
    check_policy_spec_serializable(plan.candidate)
    check_policy_spec_serializable(plan.baseline)

    jobs: list[GameJob] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _seat_assignment(plan, rotation)
            jobs.append(
                GameJob(
                    seed=seed,
                    rotation=rotation,
                    assignment=assignment,
                    game_mode=GAME_MODE,
                    max_steps=plan.max_steps,
                )
            )

    outcomes = run_game_jobs(
        jobs,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )

    game_results: list[SingleRoundGameResult] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            outcome = outcomes[(seed, rotation)]
            if outcome.error_text is not None:
                raise SingleRoundEvaluationError(
                    "single game execution failed in a worker process",
                    seed=seed,
                    rotation=rotation,
                ) from RuntimeError(outcome.error_text)
            candidate_seat = Seat(rotation)
            game_results.append(
                _build_game_result(
                    outcome.result,
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=candidate_seat,
                )
            )

    frozen_results = tuple(game_results)
    expected_count = ROTATION_COUNT * len(plan.seeds)
    if len(frozen_results) != expected_count:
        raise SingleRoundEvaluationError(
            f"expected {expected_count} raw game results but got {len(frozen_results)}",
            seed=plan.seeds[-1],
            rotation=ROTATION_COUNT - 1,
        )

    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_results
        ),
    )


__all__ = [
    "GAME_MODE",
    "ROTATION_COUNT",
    "PolicyFactoryNotSerializableError",
    "SingleRoundEvaluationError",
    "aggregate_candidate_metrics",
    "run_single_round_evaluation",
    "run_single_round_evaluation_parallel",
]
