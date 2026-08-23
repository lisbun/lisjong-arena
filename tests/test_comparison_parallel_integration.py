"""実RiichiEnvと実spawn workerを使う``run_comparison_parallel``のintegration test。

module import -> factory resolution -> Policy instance生成 -> game実行までを
実際のspawn worker process内部で通し、parent-side serialization
preflight（``pickle.dumps()``成功）だけをspawn互換性の証明にしない。

``lisjong.policies.MinimalPolicy``はimport可能なtop-level Policy classであり、
1手も計算を伴わないため高速である。CI runtimeを抑えるためseed数は1に留め、
``run_comparison()`` / ``run_comparison_parallel(max_workers=2)`` /
``run_comparison_parallel(max_workers=4)``が同一raw result・同一metricsに
なることを実行のたびに確認する。
"""

import unittest

from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena.comparison import (
    ROTATION_COUNT,
    run_comparison,
    run_comparison_parallel,
)
from lisjong_arena.model import ComparisonPlan, PolicySpec

_SEED = 12345


class ComparisonParallelIntegrationTest(unittest.TestCase):
    def test_serial_and_parallel_workers_agree_with_real_riichienv(self) -> None:
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
            policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
            seeds=(_SEED,),
        )

        serial = run_comparison(plan)
        parallel_two = run_comparison_parallel(plan, max_workers=2)
        parallel_four = run_comparison_parallel(plan, max_workers=4)

        self.assertEqual(serial.seat_results, parallel_two.seat_results)
        self.assertEqual(serial.seat_results, parallel_four.seat_results)
        self.assertEqual(serial.metrics_a, parallel_two.metrics_a)
        self.assertEqual(serial.metrics_a, parallel_four.metrics_a)
        self.assertEqual(serial.metrics_b, parallel_two.metrics_b)
        self.assertEqual(serial.metrics_b, parallel_four.metrics_b)

        self.assertEqual(len(serial.seat_results), 4 * ROTATION_COUNT)
        self.assertEqual(
            [seat_result.rotation for seat_result in parallel_two.seat_results],
            [rotation for rotation in range(ROTATION_COUNT) for _ in range(4)],
        )

    def test_parallel_execution_uses_more_than_one_worker_process(self) -> None:
        """spawn workerが実際にmodule importからgame実行まで独立に完走することを、
        1 seedあたり複数jobを同時に投入できるmax_workers>1で確認する。
        """
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
            policy_b=PolicySpec(identity="minimal-2", factory=MinimalPolicy),
            seeds=(_SEED,),
        )

        result = run_comparison_parallel(plan, max_workers=4)

        self.assertEqual(len(result.seat_results), 4 * ROTATION_COUNT)
        self.assertEqual(result.metrics_a.game_count, ROTATION_COUNT)
        self.assertEqual(result.metrics_b.game_count, ROTATION_COUNT)


if __name__ == "__main__":
    unittest.main()
