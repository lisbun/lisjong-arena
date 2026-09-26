"""実RiichiEnvを使うIssue #389 benchmark arm integration test。

強さは評価しない。benchmark workerが既存``LocalGameRunner``の``trace_sink``から
offense recordを回収でき、既存``SeatRoundStats``と整合し、serial実行と
spawn並列実行が同じraw result / recordを再現することだけを確認する。
重い``UkeirePolicy``は使わず、wall-clock thresholdも設けない。
"""

import unittest

from lisjong.policies import ShantenPolicy

from lisjong_arena.model import PolicySpec
from lisjong_arena.pure_offense_benchmark.execution import (
    benchmark_plan,
    run_benchmark_arm,
)

_SEED = 12345


class PureOffenseBenchmarkIntegrationTest(unittest.TestCase):
    def test_serial_and_parallel_arm_are_identical(self) -> None:
        plan = benchmark_plan(
            PolicySpec(identity="shanten", factory=ShantenPolicy), (_SEED,)
        )
        serial = run_benchmark_arm(plan, max_workers=1)
        parallel = run_benchmark_arm(plan, max_workers=2)

        self.assertEqual(len(serial.offense_records), 4)
        self.assertEqual(
            [record.focal_seat for record in serial.offense_records],
            [result.candidate_seat for result in serial.evaluation.game_results],
        )
        self.assertEqual(
            serial.evaluation.game_results, parallel.evaluation.game_results
        )
        self.assertEqual(serial.offense_records, parallel.offense_records)


if __name__ == "__main__":
    unittest.main()
