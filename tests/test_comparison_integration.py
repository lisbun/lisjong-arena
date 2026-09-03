"""実RiichiEnvを使うserial / spawned-parallel comparison integration test。

ここではPolicyの強さを評価しない。Arenaが実際に

    lisjong-arena -> lisjong -> RiichiEnv

の経路で異なる2 Policyを比較でき、fixed seedとseat rotationのもとでserialと
spawned parallel executionが同じraw result / metricsを再現することを確認する。

1 seedは4 rotations = 4半荘なので、real-boundary coverageはserial 1回と
parallel 1回へ集約する。same-seed execution substrate自体の再現性は
``test_lisjong_engine_hanchan_integration.DeterminismTest``、worker-count forwardingや
spawn semantics / canonical orderingはfocused lower-level testsが独立に固定する。
``UkeirePolicy``はdiscard候補ごとに多数の向聴数計算を行い実行時間が大きいため、
ここには含めない。環境差でflakyになる厳密なwall-clock thresholdも設けない。
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
    run_comparison_parallel,
    save_comparison_artifact,
)
from lisjong_arena.comparison import ROTATION_COUNT

_SEED = 12345


class ComparisonIntegrationTest(unittest.TestCase):
    def test_real_serial_and_spawned_parallel_comparison_agree(self) -> None:
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
            policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
            seeds=(_SEED,),
        )

        serial = run_comparison(plan)
        parallel = run_comparison_parallel(plan, max_workers=4)

        self.assertEqual(serial.seat_results, parallel.seat_results)
        self.assertEqual(serial.metrics_a, parallel.metrics_a)
        self.assertEqual(serial.metrics_b, parallel.metrics_b)

        for result in (serial, parallel):
            self.assertEqual(len(result.seat_results), 4 * ROTATION_COUNT)
            self.assertEqual(
                [seat_result.rotation for seat_result in result.seat_results],
                [rotation for rotation in range(ROTATION_COUNT) for _ in range(4)],
            )
            self.assertTrue(
                all(seat_result.seed == _SEED for seat_result in result.seat_results)
            )
            self.assertTrue(
                all(
                    seat_result.game_mode == "4p-red-half"
                    for seat_result in result.seat_results
                )
            )

        for metrics in (serial.metrics_a, serial.metrics_b):
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

        self.assertEqual(serial.metrics_a.policy_identity, "minimal")
        self.assertEqual(serial.metrics_b.policy_identity, "shanten")
        self.assertEqual(
            serial.metrics_a.average_rank + serial.metrics_b.average_rank,
            5.0,
        )
        self.assertEqual(
            serial.metrics_a.average_score + serial.metrics_b.average_score,
            50_000.0,
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            save_comparison_artifact(serial, path)
            artifact = load_comparison_artifact(path)

        self.assertEqual(artifact.seat_results, serial.seat_results)
        self.assertEqual(artifact.metrics_a, serial.metrics_a)
        self.assertEqual(artifact.metrics_b, serial.metrics_b)
        self.assertEqual(artifact.provenance.execution_environment, "riichienv")
        self.assertEqual(
            artifact.provenance.lisjong_revision,
            "a0666d24e66179a45fd6e231a3cbd489b492d162",
        )


if __name__ == "__main__":
    unittest.main()
