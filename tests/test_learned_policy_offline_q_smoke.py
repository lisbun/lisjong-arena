"""Serving smoke aggregation tests (Issue #140).

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、`summarize_smoke()`
のaggregation / fail-closed boundaryだけをsynthetic measurementで検証する。
"""

import unittest

from _learned_policy_offline_q_fixtures import make_result

from lisjong_arena.learned_policy_offline_q.smoke import (
    OfflineQSmokeError,
    SmokeGameMeasurement,
    summarize_smoke,
)


def _measurement(
    seed: int, *, activation: int, scaffold: int, support: int
) -> SmokeGameMeasurement:
    result = make_result(
        seed,
        (25000, 25000, 25000, 25000),
        steps=1,
        decisions=activation + scaffold + support,
    )
    return SmokeGameMeasurement(
        seed=seed,
        result=result,
        activation_count=activation,
        scaffold_fallback_count=scaffold,
        support_fallback_count=support,
        decision_count=activation + scaffold + support,
        wall_clock_seconds=1.0,
        cpu_seconds=1.0,
    )


class SmokeSummaryTest(unittest.TestCase):
    def test_summary_aggregates_rates_across_games(self):
        measurements = (
            _measurement(277, activation=6, scaffold=3, support=1),
            _measurement(278, activation=4, scaffold=4, support=2),
        )
        summary = summarize_smoke("bc", measurements)
        self.assertEqual(summary.game_count, 2)
        self.assertEqual(summary.total_decisions, 20)
        self.assertEqual(summary.total_activations, 10)
        self.assertAlmostEqual(summary.activation_rate, 0.5)
        self.assertAlmostEqual(summary.scaffold_fallback_rate, 7 / 20)
        self.assertAlmostEqual(summary.support_fallback_rate, 3 / 20)

    def test_empty_measurements_fail_closed(self):
        with self.assertRaises(OfflineQSmokeError):
            summarize_smoke("bc", ())


if __name__ == "__main__":
    unittest.main()
