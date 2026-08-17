"""実RiichiEnvを起動しないsingle-round evaluation unit test。

単一game実行境界は``lisjong_arena.single_round_evaluation._run_single_game``
だけなので、testはこの1点を差し替えてrotation、実行順序、Policy lifecycle、
raw result、metrics、fail closedを高速に固定する。
"""

import unittest
from collections.abc import Mapping
from unittest import mock

from lisjong.local_game_runner import LocalGameResult, LocalGameRunnerError
from lisjong.policy_contract import Policy, Seat

from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
)
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    ROTATION_COUNT,
    SingleRoundEvaluationError,
    run_single_round_evaluation,
)


class _StubPolicy:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    def choose_action(self, decision: object) -> object:
        raise AssertionError("unit tests must not execute policies")


class _RecordingFactory:
    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.instances: list[_StubPolicy] = []

    def __call__(self) -> _StubPolicy:
        instance = _StubPolicy(self.identity)
        self.instances.append(instance)
        return instance


class _GameCall:
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

    ``scores_per_game``を与えると呼び出し順にその4 seat分scoreを返す。
    """

    def __init__(
        self,
        scores_per_game: tuple[tuple[int, int, int, int], ...] | None = None,
        failure: Exception | None = None,
        fail_on_call: int | None = None,
        game_mode: str = GAME_MODE,
        seed_offset: int = 0,
    ) -> None:
        self.calls: list[_GameCall] = []
        self._scores_per_game = scores_per_game
        self._failure = failure
        self._fail_on_call = fail_on_call
        self._game_mode = game_mode
        self._seed_offset = seed_offset

    def __call__(
        self,
        policies: Mapping[Seat, Policy],
        *,
        seed: int,
        max_steps: int,
    ) -> LocalGameResult:
        index = len(self.calls)
        self.calls.append(_GameCall(policies, seed, self._game_mode, max_steps))
        if self._failure is not None and index == self._fail_on_call:
            raise self._failure

        scores = (
            (30_000, 25_000, 25_000, 20_000)
            if self._scores_per_game is None
            else self._scores_per_game[index]
        )
        return LocalGameResult(
            seed=seed + self._seed_offset,
            game_mode=self._game_mode,
            scores=scores,
            ranks=(1, 2, 2, 4)
            if scores == (30_000, 25_000, 25_000, 20_000)
            else (1, 2, 3, 4),
            steps=1,
            decisions=4,
        )


def _plan(
    seeds: tuple[int, ...] = (12345,), **overrides: object
) -> SingleRoundEvaluationPlan:
    fields = {
        "candidate": PolicySpec(identity="a", factory=_RecordingFactory("a")),
        "baseline": PolicySpec(identity="b", factory=_RecordingFactory("b")),
        "seeds": seeds,
    }
    fields.update(overrides)
    return SingleRoundEvaluationPlan(**fields)


def _run(
    plan: SingleRoundEvaluationPlan, fake: _FakeSingleGame
) -> SingleRoundEvaluationResult:
    with mock.patch("lisjong_arena.single_round_evaluation._run_single_game", fake):
        return run_single_round_evaluation(plan)


class ProtocolInvariantTest(unittest.TestCase):
    def test_plan_does_not_accept_a_game_mode_argument(self) -> None:
        with self.assertRaises(TypeError):
            SingleRoundEvaluationPlan(
                candidate=PolicySpec(identity="a", factory=_RecordingFactory("a")),
                baseline=PolicySpec(identity="b", factory=_RecordingFactory("b")),
                seeds=(1,),
                game_mode="4p-red-half",
            )

    def test_game_mode_is_fixed_to_4p_red_single(self) -> None:
        self.assertEqual(GAME_MODE, "4p-red-single")

    def test_every_call_receives_the_fixed_game_mode(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11, 22)), fake)

        for call in fake.calls:
            self.assertEqual(call.game_mode, GAME_MODE)

    def test_a_non_matching_returned_game_mode_is_rejected(self) -> None:
        fake = _FakeSingleGame(game_mode="4p-red-half")

        with self.assertRaises(SingleRoundEvaluationError):
            _run(_plan((11,)), fake)


class RotationTest(unittest.TestCase):
    def test_rotation_table_is_abbb(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11,)), fake)

        expected_candidate_seats = (
            Seat.SEAT_0,
            Seat.SEAT_1,
            Seat.SEAT_2,
            Seat.SEAT_3,
        )
        for call, expected_seat in zip(fake.calls, expected_candidate_seats):
            identities = {
                seat: policy.identity for seat, policy in call.policies.items()
            }
            for seat in Seat:
                expected_identity = "a" if seat == expected_seat else "b"
                self.assertEqual(identities[seat], expected_identity)

    def test_rotation_count_is_four(self) -> None:
        self.assertEqual(ROTATION_COUNT, 4)

    def test_total_games_is_four_times_seed_count(self) -> None:
        seeds = (1, 2, 3)
        fake = _FakeSingleGame()

        result = _run(_plan(seeds), fake)

        self.assertEqual(len(fake.calls), 4 * len(seeds))
        self.assertEqual(len(result.game_results), 4 * len(seeds))

    def test_candidate_takes_each_seat_exactly_n_times(self) -> None:
        seeds = (11, 22, 33)
        result = _run(_plan(seeds), _FakeSingleGame())

        for seat in Seat:
            occurrences = [
                game_result
                for game_result in result.game_results
                if game_result.candidate_seat is seat
            ]
            self.assertEqual(len(occurrences), len(seeds))

    def test_baseline_takes_the_remaining_three_seats_every_game(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11,)), fake)

        for call in fake.calls:
            identities = [policy.identity for policy in call.policies.values()]
            self.assertEqual(identities.count("a"), 1)
            self.assertEqual(identities.count("b"), 3)

    def test_execution_order_is_seed_then_rotation(self) -> None:
        seeds = (30, 10, 20)
        fake = _FakeSingleGame()

        _run(_plan(seeds), fake)

        self.assertEqual(
            [call.seed for call in fake.calls],
            [seed for seed in seeds for _ in range(ROTATION_COUNT)],
        )

    def test_raw_results_are_ordered_by_seed_then_rotation(self) -> None:
        seeds = (30, 10)
        result = _run(_plan(seeds), _FakeSingleGame())

        self.assertEqual(
            [(gr.seed, gr.rotation) for gr in result.game_results],
            [(seed, rotation) for seed in seeds for rotation in range(ROTATION_COUNT)],
        )


class PolicyLifecycleTest(unittest.TestCase):
    def test_every_seat_of_every_game_gets_a_fresh_policy_instance(self) -> None:
        factory_a = _RecordingFactory("a")
        factory_b = _RecordingFactory("b")
        plan = _plan(
            (11, 22),
            candidate=PolicySpec(identity="a", factory=factory_a),
            baseline=PolicySpec(identity="b", factory=factory_b),
        )
        fake = _FakeSingleGame()

        _run(plan, fake)

        self.assertEqual(len(factory_a.instances), 8)
        self.assertEqual(len(factory_b.instances), 24)
        created = factory_a.instances + factory_b.instances
        self.assertEqual(len({id(instance) for instance in created}), 32)

    def test_baseline_instances_are_not_shared_within_one_game(self) -> None:
        fake = _FakeSingleGame()
        _run(_plan((11,)), fake)

        for call in fake.calls:
            policies = list(call.policies.values())
            self.assertEqual(len({id(policy) for policy in policies}), 4)


class RawResultTest(unittest.TestCase):
    def test_game_results_keep_all_four_seat_scores(self) -> None:
        scores_per_game = (
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (25_000, 25_000, 25_000, 25_000),
            (40_000, 30_000, 20_000, 10_000),
        )
        result = _run(_plan((99,)), _FakeSingleGame(scores_per_game))

        for game_result, scores in zip(result.game_results, scores_per_game):
            self.assertEqual(game_result.scores, scores)
            self.assertEqual(game_result.game_mode, GAME_MODE)

    def test_candidate_score_is_read_from_the_candidate_seat(self) -> None:
        scores_per_game = (
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (25_000, 25_000, 25_000, 25_000),
            (40_000, 30_000, 20_000, 10_000),
        )
        result = _run(_plan((99,)), _FakeSingleGame(scores_per_game))

        expected_candidate_scores = (30_000, 20_000, 25_000, 10_000)
        for game_result, expected in zip(
            result.game_results, expected_candidate_scores
        ):
            self.assertEqual(game_result.candidate_score, expected)

    def test_result_keeps_the_plan_and_freezes_raw_results(self) -> None:
        plan = _plan((99,))
        result = _run(plan, _FakeSingleGame())

        self.assertIs(result.plan, plan)
        self.assertIsInstance(result.game_results, tuple)


class MetricsTest(unittest.TestCase):
    def test_mean_candidate_score_averages_over_all_games(self) -> None:
        scores_per_game = (
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (25_000, 25_000, 25_000, 25_000),
            (40_000, 30_000, 20_000, 10_000),
        )
        result = _run(_plan((99,)), _FakeSingleGame(scores_per_game))

        expected = (30_000 + 20_000 + 25_000 + 10_000) / 4
        self.assertEqual(result.candidate_metrics.mean_candidate_score, expected)
        self.assertEqual(result.candidate_metrics.game_count, 4)
        self.assertEqual(result.candidate_metrics.candidate_identity, "a")

    def test_seat_mean_scores_are_reported_per_seat(self) -> None:
        scores_per_game = (
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
            (30_000, 25_000, 25_000, 20_000),
            (10_000, 20_000, 30_000, 40_000),
        )
        result = _run(_plan((1, 2)), _FakeSingleGame(scores_per_game))

        seat_means = result.candidate_metrics.seat_mean_scores
        self.assertEqual(seat_means[0], 30_000.0)
        self.assertEqual(seat_means[1], 20_000.0)


class FailClosedTest(unittest.TestCase):
    def test_rejects_a_non_plan_argument(self) -> None:
        with self.assertRaises(TypeError):
            run_single_round_evaluation(object())

    def test_candidate_factory_failure_fails_the_whole_evaluation(self) -> None:
        cause = RuntimeError("factory exploded")

        def _failing_factory() -> _StubPolicy:
            raise cause

        plan = _plan(
            (11, 22), candidate=PolicySpec(identity="a", factory=_failing_factory)
        )
        fake = _FakeSingleGame()

        with self.assertRaises(SingleRoundEvaluationError) as raised:
            _run(plan, fake)

        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 0)
        self.assertEqual(fake.calls, [])

    def test_runner_failure_does_not_return_partial_results(self) -> None:
        cause = LocalGameRunnerError("runner exploded")
        fake = _FakeSingleGame(failure=cause, fail_on_call=5)

        with self.assertRaises(SingleRoundEvaluationError) as raised:
            _run(_plan((11, 22)), fake)

        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(raised.exception.seed, 22)
        self.assertEqual(raised.exception.rotation, 1)
        self.assertIn("seed=22", str(raised.exception))
        self.assertIn("rotation=1", str(raised.exception))
        self.assertEqual(len(fake.calls), 6)

    def test_malformed_scores_are_rejected(self) -> None:
        fake = _FakeSingleGame(((30_000, 25_000, 25_000),))

        with self.assertRaises(SingleRoundEvaluationError) as raised:
            _run(_plan((11,)), fake)

        self.assertIs(type(raised.exception.__cause__), ValueError)

    def test_seed_mismatch_is_rejected(self) -> None:
        fake = _FakeSingleGame(seed_offset=1)

        with self.assertRaises(SingleRoundEvaluationError) as raised:
            _run(_plan((11,)), fake)

        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 0)

    def test_game_mode_mismatch_is_rejected(self) -> None:
        fake = _FakeSingleGame(game_mode="4p-red-east")

        with self.assertRaises(SingleRoundEvaluationError) as raised:
            _run(_plan((11,)), fake)

        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 0)


if __name__ == "__main__":
    unittest.main()
