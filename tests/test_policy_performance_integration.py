"""実RiichiEnvと実first-party lisjong Policyを使う``policy_performance``のintegration test。

``tests.test_single_round_evaluation_parallel_integration``と同じ理由で、
instrumentation wrapperが実際のPolicy execution pathを通ることを実RiichiEnv
上で確認する。CI runtimeを抑えるためseed数は1に留める。ここで固定するのは
instrumentation on/offでobjective execution resultが変わらないという
semantic-preservation要件と、profile modeが実際にlisjong moduleのhotspotを
観測できることである。
"""

import unittest

from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.policy_performance import (
    run_policy_hotspot_profile,
    run_policy_timing_profile,
)
from lisjong_arena.single_round_evaluation import run_single_round_evaluation

_SEED = 12345


def _plan() -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity="shanten", factory=ShantenPolicy),
        baseline=PolicySpec(identity="minimal", factory=MinimalPolicy),
        seeds=(_SEED,),
    )


class PolicyPerformanceIntegrationTest(unittest.TestCase):
    def test_timing_and_profile_modes_preserve_the_objective_execution_result(
        self,
    ) -> None:
        plan = _plan()

        uninstrumented = run_single_round_evaluation(plan)
        timing = run_policy_timing_profile(plan)
        profile = run_policy_hotspot_profile(plan)

        self.assertEqual(uninstrumented.game_results, timing.result.game_results)
        self.assertEqual(
            uninstrumented.candidate_metrics, timing.result.candidate_metrics
        )
        self.assertEqual(uninstrumented.game_results, profile.result.game_results)
        self.assertEqual(
            uninstrumented.candidate_metrics, profile.result.candidate_metrics
        )

    def test_timing_mode_measures_a_positive_number_of_candidate_decisions_only(
        self,
    ) -> None:
        plan = _plan()

        result = run_policy_timing_profile(plan)

        metrics = result.candidate_decision_metrics
        self.assertGreater(metrics.decision_count, 0)
        self.assertGreaterEqual(metrics.total_decision_time_ns, 0)
        self.assertGreaterEqual(metrics.mean_decision_latency_ns, 0.0)
        self.assertGreaterEqual(result.evaluation_elapsed_seconds, 0.0)
        self.assertGreaterEqual(result.games_per_second, 0.0)

    def test_profile_mode_observes_first_party_lisjong_hotspots(self) -> None:
        plan = _plan()

        result = run_policy_hotspot_profile(plan)

        self.assertGreater(len(result.function_stats), 0)
        lisjong_hotspots = [
            stat for stat in result.function_stats if stat.module.startswith("lisjong")
        ]
        self.assertTrue(
            lisjong_hotspots,
            "expected at least one lisjong.* hotspot in the candidate decision profile",
        )


if __name__ == "__main__":
    unittest.main()
