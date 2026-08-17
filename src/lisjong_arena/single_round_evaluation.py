"""fixed seedとABBB seat rotationによるcandidate single-round評価の実行。

既存``lisjong_arena.comparison``のAABB comparisonとは意味が異なるprotocolで
あるため、独立したPlan / Result契約とexecution経路として持つ。candidate 1体
とbaseline 1体を固定seedごとに``[A, B, B, B]`` -> ``[B, A, B, B]`` ->
``[B, B, A, B]`` -> ``[B, B, B, A]``へrotationし、各gameを既存の
``lisjong.local_game_runner.LocalGameRunner``で``game_mode="4p-red-single"``
として実行する。

``4p-red-single``はこのprotocol自身のinvariantであり、``ComparisonPlan``の
genericな``game_mode``のようにcallerが指定できるoptionではない。実行経路は

    lisjong-arena -> lisjong -> RiichiEnv

であり、このmoduleは``riichienv``をimportしない。
"""

from collections.abc import Mapping

from lisjong.local_game_runner import LocalGameResult, LocalGameRunner
from lisjong.policy_contract import Policy, Seat

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
    PolicySpec,
    SingleRoundCandidateMetrics,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)

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
    )


def aggregate_candidate_metrics(
    identity: str,
    game_results: tuple[SingleRoundGameResult, ...],
) -> SingleRoundCandidateMetrics:
    """raw game resultからcandidateのmetricsを集計する。

    ``mean_candidate_score``は全gameのcandidate final scoreの平均、
    ``seat_mean_scores``はcandidateがそのseatを担当した時のfinal score平均
    である。
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
    )


def run_single_round_evaluation(
    plan: SingleRoundEvaluationPlan,
) -> SingleRoundEvaluationResult:
    """``SingleRoundEvaluationPlan``に従ってcandidate single-round評価を実行する。

    実行順序は``seed入力順 -> rotation 0..3``で決定的である。seed数をNとする
    と、total gamesは4N、candidateは各seatをちょうどN回担当する。

    途中で1 gameでも失敗した場合は``SingleRoundEvaluationError``を送出し、
    成功したgameだけのpartialな結果を返さない。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")

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
    "SingleRoundEvaluationError",
    "aggregate_candidate_metrics",
    "run_single_round_evaluation",
]
