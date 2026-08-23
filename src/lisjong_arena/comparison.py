"""fixed seedとdeterministic seat rotationによるPolicy comparisonの実行。

Arenaの責務はmatchup / seeds / seat rotation / raw result / metricsだけであり、
単一gameの進行はArena-localの``lisjong_arena.riichienv.local_game_runner.
LocalGameRunner``へ委譲する。Policy判断、Policy contract、RiichiEnv
Observation / Action変換、legal action validation、麻雀ルール、
game state transitionはArenaへ再実装しない。実行経路は

    lisjong-arena evaluation -> lisjong-arena riichienv.LocalGameRunner
        -> RiichiEnv (+ TEMPORARY lisjong RiichiEnv Adapter / GameTrace)

であり、このmoduleは``riichienv``をimportしない。

RiichiEnvと将来の``lisjong-engine``という2つの実経路が揃う前に差異を推測しない
ため、``GameBackend`` / ``EvaluationBackend`` 等の汎用backend abstractionも
導入しない。``LocalGameRunner``は``_run_single_game()``から直接呼び出す。
"""

from collections.abc import Mapping

from lisjong.policy_contract import Policy, Seat

from lisjong_arena.model import (
    ComparisonPlan,
    ComparisonResult,
    PolicyMetrics,
    PolicySpec,
    SeatResult,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner

ROTATION_COUNT = 4
"""1 seedあたりのcyclic seat rotation数。"""

_EXPECTED_RANKS = (1, 2, 3, 4)


class ComparisonExecutionError(Exception):
    """comparison中の1 gameが失敗した場合。

    Policy factory、Policy execution、lisjong Adapter、``LocalGameRunner``、
    結果の不整合いずれの失敗でも、成功したgameだけを集めたpartialな
    ``ComparisonResult``は返さずcomparison全体を失敗させる。失敗したgameを
    特定できるよう``seed``と``rotation``を保持し、原因を隠さないよう元例外を
    ``raise ... from exc``で連結する。
    """

    __slots__ = ("rotation", "seed")

    def __init__(self, message: str, *, seed: int, rotation: int) -> None:
        super().__init__(f"seed={seed} rotation={rotation}: {message}")
        self.seed = seed
        self.rotation = rotation


def _seat_assignment(
    plan: ComparisonPlan,
    rotation: int,
) -> tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec]:
    """rotation indexに対応するSeat 0..3のPolicySpec割り当てを返す。

    base assignment``[A, A, B, B]``をrotation回だけ巡回させる。

        rotation 0: [A, A, B, B]
        rotation 1: [B, A, A, B]
        rotation 2: [B, B, A, A]
        rotation 3: [A, B, B, A]

    4 rotationすべてを実行すると、A/Bとも各seatをちょうど2回ずつ担当する。
    """
    base = (plan.policy_a, plan.policy_a, plan.policy_b, plan.policy_b)
    return tuple(base[(index - rotation) % ROTATION_COUNT] for index in range(4))


def _run_single_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    game_mode: str,
    max_steps: int,
) -> LocalGameResult:
    """1 gameを既存の``LocalGameRunner``で実行する。

    Arenaにおける単一game実行境界はこの1関数だけであり、unit testはここを
    差し替えて実RiichiEnvを起動せずにrotation / raw result / metricsを検証する。
    そのためだけのbackend abstractionはproduction側へ導入しない。
    """
    return LocalGameRunner(
        policies,
        seed=seed,
        game_mode=game_mode,
        max_steps=max_steps,
    ).run()


def _create_policies(
    assignment: tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec],
    *,
    seed: int,
    rotation: int,
) -> dict[Seat, Policy]:
    """各seatごとにfactoryを呼び、新しいPolicy instanceを1つずつ生成する。

    同じPolicySpecが同じgame内の複数seatへ割り当てられていてもinstanceを共有
    しない。Policy contractは意思決定へ影響するhidden mutable stateを禁止する
    一方、cacheやmetricsのような状態保持は許容するため、seat間・game間の
    lifecycleをArena側で明示的に分離する。
    """
    policies = {}
    for seat in Seat:
        spec = assignment[seat]
        try:
            policies[seat] = spec.factory()
        except Exception as exc:
            raise ComparisonExecutionError(
                f"policy factory for identity {spec.identity!r} failed at {seat!r}",
                seed=seed,
                rotation=rotation,
            ) from exc
    return policies


def _build_seat_results(
    result: LocalGameResult,
    assignment: tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec],
    *,
    seed: int,
    rotation: int,
    game_mode: str,
) -> tuple[SeatResult, ...]:
    """1 gameの結果をSeat 0..3順のflat recordへ展開する。

    ``LocalGameResult``自体は4 seat分のscores / ranksの形式をすでに検証して
    いる。ここではArenaが要求する追加の前提、すなわち結果が要求した条件の
    ものであることと、rankがseat resultの母数として使える1..4の順列である
    ことだけを確認し、崩れていれば集計へ進まない。
    """
    if result.seed != seed or result.game_mode != game_mode:
        raise ComparisonExecutionError(
            "LocalGameRunner returned a result for different conditions "
            f"(seed={result.seed!r}, game_mode={result.game_mode!r})",
            seed=seed,
            rotation=rotation,
        )
    if tuple(sorted(result.ranks)) != _EXPECTED_RANKS:
        raise ComparisonExecutionError(
            f"ranks must be a permutation of {_EXPECTED_RANKS} but were "
            f"{result.ranks!r}",
            seed=seed,
            rotation=rotation,
        )

    return tuple(
        SeatResult(
            seed=seed,
            rotation=rotation,
            game_mode=game_mode,
            seat=seat,
            policy_identity=assignment[seat].identity,
            score=result.scores[seat],
            rank=result.ranks[seat],
        )
        for seat in Seat
    )


def aggregate_policy_metrics(
    identity: str,
    seat_results: tuple[SeatResult, ...],
) -> PolicyMetrics:
    """1つのPolicy identityのseat resultから基本metricsを集計する。

    ``game_count``は``(seed, rotation)``の異なり数、それ以外はすべてseat result
    を母数とする。``ComparisonPlan``が重複seedを拒否しているため、
    ``(seed, rotation)``はcomparison内でgameを一意に識別する。
    """
    own = tuple(result for result in seat_results if result.policy_identity == identity)
    if not own:
        raise ValueError(f"no seat results for identity {identity!r}")

    ranks = [result.rank for result in own]
    return PolicyMetrics(
        policy_identity=identity,
        game_count=len({(result.seed, result.rotation) for result in own}),
        seat_result_count=len(own),
        average_rank=sum(ranks) / len(own),
        average_score=sum(result.score for result in own) / len(own),
        first_count=ranks.count(1),
        second_count=ranks.count(2),
        third_count=ranks.count(3),
        fourth_count=ranks.count(4),
    )


def run_comparison(plan: ComparisonPlan) -> ComparisonResult:
    """``ComparisonPlan``に従ってPolicy comparisonを実行する。

    実行順序は``seed入力順 -> rotation 0..3 -> Seat 0..3``で決定的であり、
    raw resultの順序もそのまま同じ契約になる。seed数をNとすると、total games
    は4N、各Policyの参加game数は4N、seat result数は8N、各Policyが各seatを
    担当する回数は2Nになる。

    途中で1 gameでも失敗した場合は``ComparisonExecutionError``を送出し、
    成功したgameだけのpartialな結果を返さない。失敗gameをskipするfallbackも
    行わない。
    """
    if not isinstance(plan, ComparisonPlan):
        raise TypeError("plan must be a ComparisonPlan")

    seat_results: list[SeatResult] = []
    for seed in plan.seeds:
        for rotation in range(ROTATION_COUNT):
            assignment = _seat_assignment(plan, rotation)
            policies = _create_policies(assignment, seed=seed, rotation=rotation)
            try:
                result = _run_single_game(
                    policies,
                    seed=seed,
                    game_mode=plan.game_mode,
                    max_steps=plan.max_steps,
                )
            except Exception as exc:
                raise ComparisonExecutionError(
                    "single game execution failed",
                    seed=seed,
                    rotation=rotation,
                ) from exc
            seat_results.extend(
                _build_seat_results(
                    result,
                    assignment,
                    seed=seed,
                    rotation=rotation,
                    game_mode=plan.game_mode,
                )
            )

    frozen_results = tuple(seat_results)
    return ComparisonResult(
        plan=plan,
        seat_results=frozen_results,
        metrics_a=aggregate_policy_metrics(plan.policy_a.identity, frozen_results),
        metrics_b=aggregate_policy_metrics(plan.policy_b.identity, frozen_results),
    )


__all__ = [
    "ROTATION_COUNT",
    "ComparisonExecutionError",
    "aggregate_policy_metrics",
    "run_comparison",
]
