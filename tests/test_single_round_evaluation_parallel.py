"""実RiichiEnvを起動しない``run_single_round_evaluation_parallel``のunit test。

``tests.test_comparison_parallel``と同じ考え方で
``lisjong_arena.single_round_evaluation.run_game_jobs``を差し替え、canonical
result order、fail-closed、max_workers / factory serializabilityのfail
closedをRiichiEnvなしに高速に固定する。実際のprocess poolによる
orchestrationは``tests.test_parallel_execution``、実RiichiEnvでの
serial/parallel一致は``tests.test_single_round_evaluation_parallel_integration``
が担当する。
"""

import unittest
from unittest import mock

from lisjong.policy_contract import Seat

from lisjong_arena._parallel_execution import (
    GameJobOutcome,
    PolicyFactoryNotSerializableError,
)
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    ROTATION_COUNT,
    SingleRoundEvaluationError,
    aggregate_candidate_metrics,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)


def _top_level_candidate_factory() -> object:
    return object()


def _top_level_baseline_factory() -> object:
    return object()


def _plan(
    seeds: tuple[int, ...] = (12345,), **overrides: object
) -> SingleRoundEvaluationPlan:
    fields = {
        "candidate": PolicySpec(
            identity="candidate", factory=_top_level_candidate_factory
        ),
        "baseline": PolicySpec(
            identity="baseline", factory=_top_level_baseline_factory
        ),
        "seeds": seeds,
    }
    fields.update(overrides)
    return SingleRoundEvaluationPlan(**fields)


def _scores_for(candidate_seat: Seat) -> tuple[int, int, int, int]:
    scores = [20_000, 20_000, 20_000, 20_000]
    scores[candidate_seat] = 40_000
    return tuple(scores)


def _fake_outcomes(
    plan: SingleRoundEvaluationPlan,
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
        candidate_seat = Seat(rotation)
        outcomes[(seed, rotation)] = GameJobOutcome(
            seed=seed,
            rotation=rotation,
            result=LocalGameResult(
                seed=seed,
                game_mode=GAME_MODE,
                scores=_scores_for(candidate_seat),
                ranks=(1, 2, 3, 4),
                steps=1,
                decisions=4,
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
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            return_value=outcomes,
        ):
            result = run_single_round_evaluation_parallel(plan, max_workers=4)

        self.assertEqual(
            [
                (game_result.seed, game_result.rotation)
                for game_result in result.game_results
            ],
            [
                (seed, rotation)
                for seed in plan.seeds
                for rotation in range(ROTATION_COUNT)
            ],
        )

    def test_result_matches_serial_run_single_round_evaluation_for_the_same_outcomes(
        self,
    ) -> None:
        plan = _plan((11, 22))
        outcomes = _fake_outcomes(plan)
        calls_per_seed: dict[int, int] = dict.fromkeys(plan.seeds, 0)

        def _serial_single_game(policies, *, seed, max_steps):
            rotation = calls_per_seed[seed]
            calls_per_seed[seed] += 1
            return outcomes[(seed, rotation)].result

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game",
            _serial_single_game,
        ):
            serial_result = run_single_round_evaluation(plan)

        with mock.patch(
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            return_value=outcomes,
        ):
            parallel_result = run_single_round_evaluation_parallel(plan, max_workers=2)

        self.assertEqual(serial_result.game_results, parallel_result.game_results)
        self.assertEqual(
            serial_result.candidate_metrics, parallel_result.candidate_metrics
        )


class MaxWorkersTest(unittest.TestCase):
    def test_max_workers_is_forwarded_to_run_game_jobs(self) -> None:
        plan = _plan((1,))
        outcomes = _fake_outcomes(plan)

        with mock.patch(
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            return_value=outcomes,
        ) as fake:
            run_single_round_evaluation_parallel(plan, max_workers=5)

        self.assertEqual(fake.call_args.kwargs["max_workers"], 5)

    def test_invalid_max_workers_is_rejected_before_any_job_is_run(self) -> None:
        plan = _plan((1,))

        with mock.patch("lisjong_arena.single_round_evaluation.run_game_jobs") as fake:
            with self.assertRaises(ValueError):
                run_single_round_evaluation_parallel(plan, max_workers=-1)

        fake.assert_not_called()


class FailClosedTest(unittest.TestCase):
    def test_rejects_a_non_plan_argument(self) -> None:
        with self.assertRaises(TypeError):
            run_single_round_evaluation_parallel(object(), max_workers=2)

    def test_one_game_failure_does_not_return_a_partial_result(self) -> None:
        plan = _plan((11, 22))
        outcomes = _fake_outcomes(plan, failing_key=(22, 2))

        with mock.patch(
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            return_value=outcomes,
        ):
            with self.assertRaises(SingleRoundEvaluationError) as raised:
                run_single_round_evaluation_parallel(plan, max_workers=2)

        self.assertEqual(raised.exception.seed, 22)
        self.assertEqual(raised.exception.rotation, 2)
        self.assertIn("seed=22", str(raised.exception))
        self.assertIn("rotation=2", str(raised.exception))
        self.assertIn("boom", str(raised.exception.__cause__))

    def test_candidate_lambda_factory_fails_closed_before_any_job_is_run(self) -> None:
        plan = _plan(
            (1,), candidate=PolicySpec(identity="candidate", factory=lambda: object())
        )

        with mock.patch("lisjong_arena.single_round_evaluation.run_game_jobs") as fake:
            with self.assertRaises(PolicyFactoryNotSerializableError) as raised:
                run_single_round_evaluation_parallel(plan, max_workers=2)

        self.assertEqual(raised.exception.identity, "candidate")
        fake.assert_not_called()

    def test_serial_run_single_round_evaluation_still_accepts_a_lambda_factory(
        self,
    ) -> None:
        plan = _plan(
            (1,), baseline=PolicySpec(identity="baseline", factory=lambda: object())
        )

        def _fake_single_game(policies, *, seed, max_steps):
            return LocalGameResult(
                seed=seed,
                game_mode=GAME_MODE,
                scores=(40_000, 20_000, 20_000, 20_000),
                ranks=(1, 2, 3, 4),
                steps=1,
                decisions=4,
            )

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game",
            _fake_single_game,
        ):
            result = run_single_round_evaluation(plan)

        self.assertEqual(len(result.game_results), 4)


class ExistingAggregationReuseTest(unittest.TestCase):
    def test_parallel_metrics_use_the_shared_aggregation_function(self) -> None:
        plan = _plan((1, 2, 3))
        outcomes = _fake_outcomes(plan)

        with mock.patch(
            "lisjong_arena.single_round_evaluation.run_game_jobs",
            return_value=outcomes,
        ):
            result = run_single_round_evaluation_parallel(plan, max_workers=3)

        self.assertEqual(
            result.candidate_metrics,
            aggregate_candidate_metrics("candidate", result.game_results),
        )


if __name__ == "__main__":
    unittest.main()
