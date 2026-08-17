"""実RiichiEnvを使うsingle-round evaluation integration test。

ここではcandidateの強さを評価しない。Arenaが実際に

    lisjong-arena -> lisjong -> RiichiEnv

の経路でcandidate 1体 + baseline 3体のABBB single-round評価を実行でき、
fixed seedのもとで同じ``SingleRoundEvaluationPlan``がgame_resultsと
candidate_metricsを再現することだけを確認する。

seedは既存のcomparison integration testと揃えて``12345``を使う。1 seedあたり
4 rotations = 4局であり、それを再現性確認のため2回実行する。``UkeirePolicy``は
discard候補ごとに多数の向聴数計算を行い実行時間が大きいため、ここには含めない。
環境差でflakyになる厳密なwall-clock thresholdも設けない。artifact保存はここでは
扱わない。
"""

import unittest

from lisjong.policies import MinimalPolicy, ShantenPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    ROTATION_COUNT,
    run_single_round_evaluation,
)

_SEED = 12345


class SingleRoundEvaluationIntegrationTest(unittest.TestCase):
    def test_fixed_seed_single_round_evaluation_is_reproducible(self) -> None:
        plan = SingleRoundEvaluationPlan(
            candidate=PolicySpec(identity="minimal", factory=MinimalPolicy),
            baseline=PolicySpec(identity="shanten", factory=ShantenPolicy),
            seeds=(_SEED,),
        )

        first = run_single_round_evaluation(plan)
        second = run_single_round_evaluation(plan)

        self.assertEqual(first.game_results, second.game_results)
        self.assertEqual(first.candidate_metrics, second.candidate_metrics)

        self.assertEqual(len(first.game_results), ROTATION_COUNT)
        self.assertEqual(
            [game_result.candidate_seat for game_result in first.game_results],
            [Seat.SEAT_0, Seat.SEAT_1, Seat.SEAT_2, Seat.SEAT_3],
        )
        self.assertTrue(
            all(game_result.seed == _SEED for game_result in first.game_results)
        )
        self.assertTrue(
            all(
                game_result.game_mode == GAME_MODE for game_result in first.game_results
            )
        )

        self.assertEqual(first.candidate_metrics.candidate_identity, "minimal")
        self.assertEqual(first.candidate_metrics.game_count, ROTATION_COUNT)


if __name__ == "__main__":
    unittest.main()
