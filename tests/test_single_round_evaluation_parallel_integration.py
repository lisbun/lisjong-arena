"""実RiichiEnvと実spawn workerを使う``run_single_round_evaluation_parallel``の
integration test。

``tests.test_comparison_parallel_integration``と同じ理由で、module import ->
factory resolution -> Policy instance生成 -> game実行までを実際のspawn
worker process内部で通す。CI runtimeを抑えるためseed数は1に留める。
"""

import unittest

from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)

_SEED = 12345


class SingleRoundEvaluationParallelIntegrationTest(unittest.TestCase):
    def test_serial_and_parallel_workers_agree_with_real_riichienv(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=PolicySpec(identity="shanten", factory=ShantenPolicy),
            baseline=PolicySpec(identity="minimal", factory=MinimalPolicy),
            seeds=(_SEED,),
        )

        serial = run_single_round_evaluation(plan)
        parallel_two = run_single_round_evaluation_parallel(plan, max_workers=2)
        parallel_four = run_single_round_evaluation_parallel(plan, max_workers=4)

        self.assertEqual(serial.game_results, parallel_two.game_results)
        self.assertEqual(serial.game_results, parallel_four.game_results)
        self.assertEqual(serial.candidate_metrics, parallel_two.candidate_metrics)
        self.assertEqual(serial.candidate_metrics, parallel_four.candidate_metrics)

        self.assertEqual(len(serial.game_results), ROTATION_COUNT)
        self.assertEqual(parallel_two.candidate_metrics.game_count, ROTATION_COUNT)


if __name__ == "__main__":
    unittest.main()
