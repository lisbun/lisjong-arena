"""lisjong-project #20 coverage-only oracle sensitivity pilot tests。"""

import unittest
from unittest.mock import patch

from lisjong.belief import SCALE, estimate_conditional_uniform_hand_belief
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

import lisjong_arena.oracle_sensitivity_pilot as pilot
from lisjong_arena.lisjong_engine.policy_input import policy_input_from_observation

_SEED = 20260827


class OracleBeliefBoundaryTest(unittest.TestCase):
    def test_oracle_replaces_only_stable_opponents_from_omniscient_state(self) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)

        observation = build_seat_observation(match_state, EngineSeat.EAST)
        policy_input = policy_input_from_observation(observation)
        baseline = estimate_conditional_uniform_hand_belief(
            policy_input,
            pilot._opponent_slot_counts_by_wind(policy_input),
        )
        oracle = pilot._build_oracle_belief(match_state, policy_input, baseline)

        self.assertIsNotNone(oracle)
        self_wind_number = 0
        self.assertIs(oracle.hands[self_wind_number], baseline.hands[self_wind_number])
        for wind_number in (1, 2, 3):
            with self.subTest(wind_number=wind_number):
                self.assertEqual(
                    sum(oracle.hands[wind_number].expected_count_raw),
                    13 * SCALE,
                )

    def test_public_slot_counts_do_not_use_current_self_draw_as_opponent_mass(
        self,
    ) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)

        observation = build_seat_observation(match_state, EngineSeat.EAST)
        policy_input = policy_input_from_observation(observation)

        self.assertEqual(
            pilot._opponent_slot_counts_by_wind(policy_input),
            (0, 13, 13, 13),
        )


class PilotAggregationTest(unittest.TestCase):
    def test_aggregate_contains_coverage_only(self) -> None:
        results = (
            pilot.OracleSensitivityPilotSeedResult(
                seed=1,
                total_decisions=10,
                discard_eligible_decisions=6,
                oracle_buildable_decisions=5,
                consumer_active_decisions=2,
                unstable_state_exclusions=1,
                decision_kind_counts=(("turn", 10),),
                unstable_exclusion_kind_counts=(("turn", 1),),
            ),
            pilot.OracleSensitivityPilotSeedResult(
                seed=2,
                total_decisions=12,
                discard_eligible_decisions=7,
                oracle_buildable_decisions=7,
                consumer_active_decisions=3,
                unstable_state_exclusions=0,
                decision_kind_counts=(("discard_reaction", 4), ("turn", 8)),
                unstable_exclusion_kind_counts=(),
            ),
        )
        with patch.object(
            pilot,
            "run_oracle_sensitivity_pilot_seed",
            side_effect=results,
        ):
            summary = pilot.run_oracle_sensitivity_pilot((1, 2))

        self.assertEqual(summary.total_decisions, 22)
        self.assertEqual(summary.discard_eligible_decisions, 13)
        self.assertEqual(summary.oracle_buildable_decisions, 12)
        self.assertEqual(summary.consumer_active_decisions, 5)
        self.assertEqual(summary.unstable_state_exclusions, 1)
        self.assertEqual(
            summary.decision_kind_counts,
            (("discard_reaction", 4), ("turn", 18)),
        )
        self.assertEqual(summary.unstable_exclusion_kind_counts, (("turn", 1),))
        self.assertFalse(hasattr(summary, "action_divergence"))
        self.assertFalse(hasattr(summary, "oracle_action"))

    def test_rejects_empty_seed_set(self) -> None:
        with self.assertRaises(ValueError):
            pilot.run_oracle_sensitivity_pilot(())


if __name__ == "__main__":
    unittest.main()
