"""実RiichiEnvを起動しないcomparison unit test。

Arenaにおける単一game実行境界は``lisjong_arena.comparison._run_single_game``
だけなので、testはこの1点を差し替えてrotation、実行順序、Policy lifecycle、
raw result、metrics、fail closedを高速に固定する。testのためだけの汎用backend
abstractionはproduction側へ導入しない。
"""

import unittest
from collections.abc import Mapping
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Policy, Seat

from lisjong_arena.comparison import (
    ROTATION_COUNT,
    ComparisonExecutionError,
    _seat_assignment,
    run_comparison,
)
from lisjong_arena.model import ComparisonPlan, ComparisonResult, PolicySpec
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameResult,
    LocalGameRunnerError,
)

_SCORE_BY_RANK = {1: 40_000, 2: 30_000, 3: 20_000, 4: 10_000}
_EVEN_RANKS = (1, 2, 3, 4)


class _StubPolicy:
    """Policy契約に形だけ適合するstub。

    unit testでは1手も進行しないため``choose_action``は呼ばれない。呼ばれた
    場合はtestの前提が壊れているので失敗させる。
    """

    def __init__(self, identity: str) -> None:
        self.identity = identity

    def choose_action(self, decision: object) -> object:
        raise AssertionError("unit tests must not execute policies")


class _RecordingFactory:
    """生成したPolicy instanceを記録するfactory。"""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.instances: list[_StubPolicy] = []

    def __call__(self) -> _StubPolicy:
        instance = _StubPolicy(self.identity)
        self.instances.append(instance)
        return instance


class _GameCall:
    """``_run_single_game``の1回分の呼び出し記録。"""

    def __init__(
        self,
        policies: Mapping[Seat, Policy],
        seed: int,
        game_mode: str,
        max_steps: int,
    ) -> None:
        self.policies = dict(policies)
        self.seed = seed
        self.game_mode = game_mode
        self.max_steps = max_steps


class _FakeSingleGame:
    """``_run_single_game``差し替え用の決定的なfake。

    ``ranks_per_game``を与えると呼び出し順にその順位を返す。scoreは順位から
    一意に決まるので、raw resultとmetricsの期待値をtest側で計算できる。
    """

    def __init__(
        self,
        ranks_per_game: tuple[tuple[int, int, int, int], ...] | None = None,
        failure: Exception | None = None,
        fail_on_call: int | None = None,
    ) -> None:
        self.calls: list[_GameCall] = []
        self._ranks_per_game = ranks_per_game
        self._failure = failure
        self._fail_on_call = fail_on_call

    def __call__(
        self,
        policies: Mapping[Seat, Policy],
        *,
        seed: int,
        game_mode: str,
        max_steps: int,
    ) -> LocalGameResult:
        index = len(self.calls)
        self.calls.append(_GameCall(policies, seed, game_mode, max_steps))
        if self._failure is not None and index == self._fail_on_call:
            raise self._failure

        ranks = (
            _EVEN_RANKS if self._ranks_per_game is None else self._ranks_per_game[index]
        )
        scores = tuple(_SCORE_BY_RANK[rank] for rank in ranks)
        return LocalGameResult(
            seed=seed,
            game_mode=game_mode,
            scores=scores,
            ranks=ranks,
            steps=1,
            decisions=4,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )


def _plan(seeds: tuple[int, ...] = (12345,), **overrides: object) -> ComparisonPlan:
    fields = {
        "policy_a": PolicySpec(identity="a", factory=_RecordingFactory("a")),
        "policy_b": PolicySpec(identity="b", factory=_RecordingFactory("b")),
        "seeds": seeds,
    }
    fields.update(overrides)
    return ComparisonPlan(**fields)


def _run(plan: ComparisonPlan, fake: _FakeSingleGame) -> ComparisonResult:
    with mock.patch("lisjong_arena.comparison._run_single_game", fake):
        return run_comparison(plan)


class SeatRotationTest(unittest.TestCase):
    def test_rotations_match_the_comparison_protocol(self) -> None:
        plan = _plan()
        expected = (
            ("a", "a", "b", "b"),
            ("b", "a", "a", "b"),
            ("b", "b", "a", "a"),
            ("a", "b", "b", "a"),
        )

        for rotation, identities in enumerate(expected):
            with self.subTest(rotation=rotation):
                assignment = _seat_assignment(plan, rotation)
                self.assertEqual(
                    tuple(spec.identity for spec in assignment),
                    identities,
                )

    def test_rotation_count_is_four(self) -> None:
        self.assertEqual(ROTATION_COUNT, 4)

    def test_each_policy_takes_each_seat_twice_per_seed(self) -> None:
        seeds = (11, 22, 33)
        result = _run(_plan(seeds), _FakeSingleGame())

        for seed in seeds:
            for identity in ("a", "b"):
                for seat in Seat:
                    with self.subTest(seed=seed, identity=identity, seat=seat):
                        occurrences = [
                            seat_result
                            for seat_result in result.seat_results
                            if seat_result.seed == seed
                            and seat_result.policy_identity == identity
                            and seat_result.seat is seat
                        ]
                        self.assertEqual(len(occurrences), 2)

    def test_every_game_assigns_two_seats_to_each_policy(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11, 22)), fake)

        for index, call in enumerate(fake.calls):
            with self.subTest(call=index):
                identities = [policy.identity for policy in call.policies.values()]
                self.assertEqual(identities.count("a"), 2)
                self.assertEqual(identities.count("b"), 2)


class ExecutionOrderTest(unittest.TestCase):
    def test_games_run_in_seed_input_order_then_rotation_order(self) -> None:
        seeds = (30, 10, 20)
        fake = _FakeSingleGame()

        _run(_plan(seeds), fake)

        self.assertEqual(
            [call.seed for call in fake.calls],
            [seed for seed in seeds for _ in range(ROTATION_COUNT)],
        )

    def test_seed_order_is_part_of_the_protocol(self) -> None:
        forward = _run(_plan((10, 20)), _FakeSingleGame())
        reversed_order = _run(_plan((20, 10)), _FakeSingleGame())

        self.assertEqual(
            [seat_result.seed for seat_result in forward.seat_results][0],
            10,
        )
        self.assertEqual(
            [seat_result.seed for seat_result in reversed_order.seat_results][0],
            20,
        )

    def test_raw_results_are_ordered_by_seed_rotation_seat(self) -> None:
        seeds = (30, 10)
        result = _run(_plan(seeds), _FakeSingleGame())

        self.assertEqual(
            [
                (seat_result.seed, seat_result.rotation, int(seat_result.seat))
                for seat_result in result.seat_results
            ],
            [
                (seed, rotation, seat)
                for seed in seeds
                for rotation in range(ROTATION_COUNT)
                for seat in range(4)
            ],
        )

    def test_total_game_and_seat_result_counts_follow_the_seed_count(self) -> None:
        seeds = (1, 2, 3)
        fake = _FakeSingleGame()

        result = _run(_plan(seeds), fake)

        self.assertEqual(len(fake.calls), 4 * len(seeds))
        self.assertEqual(len(result.seat_results), 16 * len(seeds))


class PolicyLifecycleTest(unittest.TestCase):
    def test_every_seat_of_every_game_gets_a_fresh_policy_instance(self) -> None:
        factory_a = _RecordingFactory("a")
        factory_b = _RecordingFactory("b")
        plan = _plan(
            (11, 22),
            policy_a=PolicySpec(identity="a", factory=factory_a),
            policy_b=PolicySpec(identity="b", factory=factory_b),
        )
        fake = _FakeSingleGame()

        _run(plan, fake)

        created = factory_a.instances + factory_b.instances
        self.assertEqual(len(factory_a.instances), 16)
        self.assertEqual(len(factory_b.instances), 16)
        self.assertEqual(len({id(instance) for instance in created}), 32)

        used = [policy for call in fake.calls for policy in call.policies.values()]
        self.assertEqual(len(used), 32)
        self.assertEqual(len({id(policy) for policy in used}), 32)

    def test_policy_instances_are_not_shared_between_seats_of_one_game(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11,)), fake)

        for index, call in enumerate(fake.calls):
            with self.subTest(call=index):
                policies = list(call.policies.values())
                self.assertEqual(len({id(policy) for policy in policies}), 4)

    def test_policies_are_keyed_by_every_seat(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11,)), fake)

        for index, call in enumerate(fake.calls):
            with self.subTest(call=index):
                self.assertEqual(set(call.policies), set(Seat))


class RunnerArgumentsTest(unittest.TestCase):
    def test_plan_conditions_are_passed_to_the_single_game_boundary(self) -> None:
        plan = _plan((7,), game_mode="4p-red-east", max_steps=123)
        fake = _FakeSingleGame()

        _run(plan, fake)

        for index, call in enumerate(fake.calls):
            with self.subTest(call=index):
                self.assertEqual(call.seed, 7)
                self.assertEqual(call.game_mode, "4p-red-east")
                self.assertEqual(call.max_steps, 123)


class RawResultTest(unittest.TestCase):
    def test_seat_results_match_the_assignment_and_the_game_result(self) -> None:
        ranks_per_game = (
            (1, 2, 3, 4),
            (4, 3, 2, 1),
            (2, 1, 4, 3),
            (3, 4, 1, 2),
        )
        plan = _plan((99,), game_mode="4p-red-east")
        result = _run(plan, _FakeSingleGame(ranks_per_game))

        expected_identities = (
            ("a", "a", "b", "b"),
            ("b", "a", "a", "b"),
            ("b", "b", "a", "a"),
            ("a", "b", "b", "a"),
        )
        seat_results = iter(result.seat_results)
        for rotation, ranks in enumerate(ranks_per_game):
            for seat in Seat:
                with self.subTest(rotation=rotation, seat=seat):
                    seat_result = next(seat_results)
                    self.assertEqual(seat_result.seed, 99)
                    self.assertEqual(seat_result.rotation, rotation)
                    self.assertEqual(seat_result.game_mode, "4p-red-east")
                    self.assertIs(seat_result.seat, seat)
                    self.assertEqual(
                        seat_result.policy_identity,
                        expected_identities[rotation][seat],
                    )
                    self.assertEqual(seat_result.rank, ranks[seat])
                    self.assertEqual(seat_result.score, _SCORE_BY_RANK[ranks[seat]])
        self.assertEqual(list(seat_results), [])

    def test_result_keeps_the_plan_and_freezes_raw_results(self) -> None:
        plan = _plan((99,))
        result = _run(plan, _FakeSingleGame())

        self.assertIs(result.plan, plan)
        self.assertIsInstance(result.seat_results, tuple)


class MetricsTest(unittest.TestCase):
    """metricsの母数を固定する。

    ``game_count``はそのPolicyが参加したgame数、それ以外はseat resultを母数と
    する。1 gameで2 seatを担当してもgame_countは1しか増えない。
    """

    def test_game_count_and_seat_result_count_are_not_confused(self) -> None:
        seeds = (1, 2, 3)
        result = _run(_plan(seeds), _FakeSingleGame())

        for metrics in (result.metrics_a, result.metrics_b):
            with self.subTest(identity=metrics.policy_identity):
                self.assertEqual(metrics.game_count, 4 * len(seeds))
                self.assertEqual(metrics.seat_result_count, 8 * len(seeds))

    def test_averages_and_rank_counts_use_seat_results(self) -> None:
        ranks_per_game = (
            (1, 2, 3, 4),
            (1, 2, 3, 4),
            (1, 3, 2, 4),
            (2, 3, 4, 1),
        )
        result = _run(_plan((99,)), _FakeSingleGame(ranks_per_game))

        metrics_a = result.metrics_a
        self.assertEqual(metrics_a.policy_identity, "a")
        self.assertEqual(metrics_a.game_count, 4)
        self.assertEqual(metrics_a.seat_result_count, 8)
        self.assertEqual(metrics_a.average_rank, 17 / 8)
        self.assertEqual(metrics_a.average_score, 230_000 / 8)
        self.assertEqual(metrics_a.first_count, 2)
        self.assertEqual(metrics_a.second_count, 4)
        self.assertEqual(metrics_a.third_count, 1)
        self.assertEqual(metrics_a.fourth_count, 1)

        metrics_b = result.metrics_b
        self.assertEqual(metrics_b.policy_identity, "b")
        self.assertEqual(metrics_b.game_count, 4)
        self.assertEqual(metrics_b.seat_result_count, 8)
        self.assertEqual(metrics_b.average_rank, 23 / 8)
        self.assertEqual(metrics_b.average_score, 170_000 / 8)
        self.assertEqual(metrics_b.first_count, 2)
        self.assertEqual(metrics_b.second_count, 0)
        self.assertEqual(metrics_b.third_count, 3)
        self.assertEqual(metrics_b.fourth_count, 3)

    def test_rank_counts_cover_every_seat_result(self) -> None:
        result = _run(_plan((1, 2)), _FakeSingleGame())

        for metrics in (result.metrics_a, result.metrics_b):
            with self.subTest(identity=metrics.policy_identity):
                self.assertEqual(
                    metrics.first_count
                    + metrics.second_count
                    + metrics.third_count
                    + metrics.fourth_count,
                    metrics.seat_result_count,
                )


class FailClosedTest(unittest.TestCase):
    def test_rejects_a_non_plan_argument(self) -> None:
        with self.assertRaises(TypeError):
            run_comparison(object())

    def test_policy_factory_failure_fails_the_whole_comparison(self) -> None:
        cause = RuntimeError("factory exploded")

        def _failing_factory() -> _StubPolicy:
            raise cause

        plan = _plan(
            (11, 22),
            policy_b=PolicySpec(identity="b", factory=_failing_factory),
        )
        fake = _FakeSingleGame()

        with self.assertRaises(ComparisonExecutionError) as raised:
            _run(plan, fake)

        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 0)
        self.assertEqual(fake.calls, [])

    def test_single_game_failure_does_not_return_partial_results(self) -> None:
        cause = LocalGameRunnerError("runner exploded")
        fake = _FakeSingleGame(failure=cause, fail_on_call=5)

        with self.assertRaises(ComparisonExecutionError) as raised:
            _run(_plan((11, 22)), fake)

        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(raised.exception.seed, 22)
        self.assertEqual(raised.exception.rotation, 1)
        self.assertIn("seed=22", str(raised.exception))
        self.assertIn("rotation=1", str(raised.exception))
        self.assertEqual(len(fake.calls), 6)

    def test_result_for_other_conditions_is_rejected(self) -> None:
        class _WrongSeedGame(_FakeSingleGame):
            def __call__(self, policies, *, seed, game_mode, max_steps):
                return super().__call__(
                    policies,
                    seed=seed + 1,
                    game_mode=game_mode,
                    max_steps=max_steps,
                )

        with self.assertRaises(ComparisonExecutionError) as raised:
            _run(_plan((11,)), _WrongSeedGame())

        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 0)

    def test_inconsistent_ranks_are_rejected(self) -> None:
        fake = _FakeSingleGame(((1, 1, 3, 4), (1, 2, 3, 4), (1, 2, 3, 4), (1, 2, 3, 4)))

        with self.assertRaises(ComparisonExecutionError) as raised:
            _run(_plan((11,)), fake)

        self.assertEqual(raised.exception.rotation, 0)


if __name__ == "__main__":
    unittest.main()
