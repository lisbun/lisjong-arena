"""実RiichiEnvを起動しない``run_comparison_parallel``のunit test。

``lisjong_arena.comparison._run_single_game``を差し替える既存serial testと
同じ考え方で、``lisjong_arena.comparison.run_game_jobs``を差し替えて
canonical result order、fail-closed、max_workers / factory serializability
のfail closedをRiichiEnvなしに高速に固定する。実際のprocess poolによる
orchestrationは``tests.test_parallel_execution``、実RiichiEnvでの
serial/parallel一致は``tests.test_comparison_parallel_integration``が担当する。
"""

import unittest
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple

from lisjong_arena._parallel_execution import (
    GameJobOutcome,
    PolicyFactoryNotSerializableError,
)
from lisjong_arena.comparison import (
    ROTATION_COUNT,
    ComparisonExecutionError,
    aggregate_policy_metrics,
    run_comparison,
    run_comparison_parallel,
)
from lisjong_arena.model import ComparisonPlan, PolicySpec
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

_SCORE_BY_RANK = {1: 40_000, 2: 30_000, 3: 20_000, 4: 10_000}


def _top_level_factory_a() -> object:
    return object()


def _top_level_factory_b() -> object:
    return object()


def _plan(seeds: tuple[int, ...] = (12345,), **overrides: object) -> ComparisonPlan:
    fields = {
        "policy_a": PolicySpec(identity="a", factory=_top_level_factory_a),
        "policy_b": PolicySpec(identity="b", factory=_top_level_factory_b),
        "seeds": seeds,
    }
    fields.update(overrides)
    return ComparisonPlan(**fields)


def _fake_outcomes(
    plan: ComparisonPlan,
    ranks_by_seed_rotation: dict[tuple[int, int], tuple[int, int, int, int]]
    | None = None,
    failing_key: tuple[int, int] | None = None,
    shuffle: bool = False,
) -> dict[tuple[int, int], GameJobOutcome]:
    keys = [
        (seed, rotation) for seed in plan.seeds for rotation in range(ROTATION_COUNT)
    ]
    if shuffle:
        keys = list(reversed(keys))

    outcomes: dict[tuple[int, int], GameJobOutcome] = {}
    for seed, rotation in keys:
        if failing_key == (seed, rotation):
            outcomes[(seed, rotation)] = GameJobOutcome(
                seed=seed, rotation=rotation, result=None, error_text="boom"
            )
            continue
        ranks = (
            (1, 2, 3, 4)
            if ranks_by_seed_rotation is None
            else ranks_by_seed_rotation[(seed, rotation)]
        )
        outcomes[(seed, rotation)] = GameJobOutcome(
            seed=seed,
            rotation=rotation,
            result=LocalGameResult(
                seed=seed,
                game_mode=plan.game_mode,
                scores=tuple(_SCORE_BY_RANK[rank] for rank in ranks),
                ranks=ranks,
                steps=1,
                decisions=4,
                seat_round_stats=neutral_seat_round_stats_tuple(
                    tuple(_SCORE_BY_RANK[rank] for rank in ranks)
                ),
            ),
            error_text=None,
        )
    return outcomes


class CanonicalOrderTest(unittest.TestCase):
    def test_result_order_does_not_depend_on_outcome_dict_iteration_order(
        self,
    ) -> None:
        plan = _plan((30, 10))
        outcomes = _fake_outcomes(plan, shuffle=True)

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ):
            result = run_comparison_parallel(plan, max_workers=4)

        self.assertEqual(
            [
                (seat_result.seed, seat_result.rotation, int(seat_result.seat))
                for seat_result in result.seat_results
            ],
            [
                (seed, rotation, seat)
                for seed in plan.seeds
                for rotation in range(ROTATION_COUNT)
                for seat in range(4)
            ],
        )

    def test_result_matches_serial_run_comparison_for_the_same_outcomes(self) -> None:
        plan = _plan((11, 22))
        outcomes = _fake_outcomes(plan)

        # serial code calls _run_single_game once per rotation in order;
        # reuse the same canonical outcomes so serial and parallel agree.
        calls_per_seed: dict[int, int] = dict.fromkeys(plan.seeds, 0)

        def _serial_single_game(policies, *, seed, game_mode, max_steps):
            rotation = calls_per_seed[seed]
            calls_per_seed[seed] += 1
            return outcomes[(seed, rotation)].result

        with mock.patch(
            "lisjong_arena.comparison._run_single_game", _serial_single_game
        ):
            serial_result = run_comparison(plan)

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ):
            parallel_result = run_comparison_parallel(plan, max_workers=2)

        self.assertEqual(serial_result.seat_results, parallel_result.seat_results)
        self.assertEqual(serial_result.metrics_a, parallel_result.metrics_a)
        self.assertEqual(serial_result.metrics_b, parallel_result.metrics_b)


class MaxWorkersTest(unittest.TestCase):
    def test_max_workers_is_forwarded_to_run_game_jobs(self) -> None:
        plan = _plan((1,))
        outcomes = _fake_outcomes(plan)

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ) as fake:
            run_comparison_parallel(plan, max_workers=7)

        self.assertEqual(fake.call_args.kwargs["max_workers"], 7)

    def test_invalid_max_workers_is_rejected_before_any_job_is_run(self) -> None:
        plan = _plan((1,))

        with mock.patch("lisjong_arena.comparison.run_game_jobs") as fake:
            with self.assertRaises(ValueError):
                run_comparison_parallel(plan, max_workers=0)

        fake.assert_not_called()


class FailClosedTest(unittest.TestCase):
    def test_rejects_a_non_plan_argument(self) -> None:
        with self.assertRaises(TypeError):
            run_comparison_parallel(object(), max_workers=2)

    def test_one_game_failure_does_not_return_a_partial_result(self) -> None:
        plan = _plan((11, 22))
        outcomes = _fake_outcomes(plan, failing_key=(22, 1))

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ):
            with self.assertRaises(ComparisonExecutionError) as raised:
                run_comparison_parallel(plan, max_workers=2)

        self.assertEqual(raised.exception.seed, 22)
        self.assertEqual(raised.exception.rotation, 1)
        self.assertIn("seed=22", str(raised.exception))
        self.assertIn("rotation=1", str(raised.exception))
        self.assertIn("boom", str(raised.exception.__cause__))

    def test_failure_is_reported_in_canonical_order_regardless_of_which_job_failed_first(
        self,
    ) -> None:
        plan = _plan((11, 22))
        outcomes = _fake_outcomes(plan, failing_key=(11, 3))
        outcomes[(22, 0)] = GameJobOutcome(
            seed=22, rotation=0, result=None, error_text="also boom"
        )

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ):
            with self.assertRaises(ComparisonExecutionError) as raised:
                run_comparison_parallel(plan, max_workers=2)

        # canonical order visits seed=11 (all rotations) before seed=22, so
        # the seed=11 rotation=3 failure must be reported, not seed=22.
        self.assertEqual(raised.exception.seed, 11)
        self.assertEqual(raised.exception.rotation, 3)

    def test_policy_a_lambda_factory_fails_closed_before_any_job_is_run(self) -> None:
        plan = _plan((1,), policy_a=PolicySpec(identity="a", factory=lambda: object()))

        with mock.patch("lisjong_arena.comparison.run_game_jobs") as fake:
            with self.assertRaises(PolicyFactoryNotSerializableError) as raised:
                run_comparison_parallel(plan, max_workers=2)

        self.assertEqual(raised.exception.identity, "a")
        fake.assert_not_called()

    def test_serial_run_comparison_still_accepts_a_lambda_factory(self) -> None:
        plan = _plan((1,), policy_b=PolicySpec(identity="b", factory=lambda: object()))

        def _fake_single_game(policies, *, seed, game_mode, max_steps):
            return LocalGameResult(
                seed=seed,
                game_mode=game_mode,
                scores=(40_000, 30_000, 20_000, 10_000),
                ranks=(1, 2, 3, 4),
                steps=1,
                decisions=4,
                seat_round_stats=neutral_seat_round_stats_tuple(
                    (40_000, 30_000, 20_000, 10_000)
                ),
            )

        with mock.patch("lisjong_arena.comparison._run_single_game", _fake_single_game):
            result = run_comparison(plan)

        self.assertEqual(len(result.seat_results), 16)


class ExistingAggregationReuseTest(unittest.TestCase):
    def test_parallel_metrics_use_the_shared_aggregation_function(self) -> None:
        plan = _plan((1, 2, 3))
        outcomes = _fake_outcomes(plan)

        with mock.patch(
            "lisjong_arena.comparison.run_game_jobs", return_value=outcomes
        ):
            result = run_comparison_parallel(plan, max_workers=3)

        self.assertEqual(
            result.metrics_a, aggregate_policy_metrics("a", result.seat_results)
        )
        self.assertEqual(
            result.metrics_b, aggregate_policy_metrics("b", result.seat_results)
        )


if __name__ == "__main__":
    unittest.main()
