"""実RiichiEnvを使うcomparison integration test。

ここではPolicyの強さを評価しない。Arenaが実際に

    lisjong-arena -> lisjong -> RiichiEnv

の経路で異なる2 Policyを比較でき、fixed seedとseat rotationのもとで同じ
``ComparisonPlan``がraw resultとmetricsを再現することだけを確認する。

seedは既存のlisjong integration testと揃えて``12345``を使う。1 seedあたり
4 rotations = 4半荘であり、それを再現性確認のため2回実行する。``UkeirePolicy``は
discard候補ごとに多数の向聴数計算を行い実行時間が大きいため、ここには含めない。
環境差でflakyになる厳密なwall-clock thresholdも設けない。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import (
    ComparisonPlan,
    PolicySpec,
    load_comparison_artifact,
    run_comparison,
    save_comparison_artifact,
)
from lisjong_arena.comparison import ROTATION_COUNT

_SEED = 12345


class ComparisonIntegrationTest(unittest.TestCase):
    def test_fixed_seed_comparison_of_two_policies_is_reproducible(self) -> None:
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
            policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
            seeds=(_SEED,),
        )

        first = run_comparison(plan)
        second = run_comparison(plan)

        self.assertEqual(first.seat_results, second.seat_results)
        self.assertEqual(first.metrics_a, second.metrics_a)
        self.assertEqual(first.metrics_b, second.metrics_b)

        self.assertEqual(len(first.seat_results), 4 * ROTATION_COUNT)
        self.assertEqual(
            [seat_result.rotation for seat_result in first.seat_results],
            [rotation for rotation in range(ROTATION_COUNT) for _ in range(4)],
        )
        self.assertTrue(
            all(seat_result.seed == _SEED for seat_result in first.seat_results)
        )
        self.assertTrue(
            all(
                seat_result.game_mode == "4p-red-half"
                for seat_result in first.seat_results
            )
        )

        for metrics in (first.metrics_a, first.metrics_b):
            with self.subTest(identity=metrics.policy_identity):
                self.assertEqual(metrics.game_count, ROTATION_COUNT)
                self.assertEqual(metrics.seat_result_count, 2 * ROTATION_COUNT)
                self.assertEqual(
                    metrics.first_count
                    + metrics.second_count
                    + metrics.third_count
                    + metrics.fourth_count,
                    metrics.seat_result_count,
                )

        self.assertEqual(first.metrics_a.policy_identity, "minimal")
        self.assertEqual(first.metrics_b.policy_identity, "shanten")
        self.assertEqual(
            first.metrics_a.average_rank + first.metrics_b.average_rank,
            5.0,
        )
        self.assertEqual(
            first.metrics_a.average_score + first.metrics_b.average_score,
            50_000.0,
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            save_comparison_artifact(first, path)
            artifact = load_comparison_artifact(path)

        self.assertEqual(artifact.seat_results, first.seat_results)
        self.assertEqual(artifact.metrics_a, first.metrics_a)
        self.assertEqual(artifact.metrics_b, first.metrics_b)
        self.assertEqual(artifact.provenance.execution_environment, "riichienv")
        self.assertEqual(
            artifact.provenance.lisjong_revision,
            "dfaf494ac819da01eef4681ff9041a057fa313bc",
        )


if __name__ == "__main__":
    unittest.main()
