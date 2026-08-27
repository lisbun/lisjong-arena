"""lisjong-project #20 preregistered paired main measurement tests。"""

import unittest
from unittest.mock import patch

from lisjong.policy_contract import DiscardAction
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.public_state import public_tile
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

import lisjong_arena.oracle_sensitivity_main as measurement
from lisjong_arena.lisjong_engine.domain_conversion import tile_from_public_tile
from lisjong_arena.lisjong_engine.policy_input import build_policy_input

_SEED = 20260827


def _result(
    seed: int,
    *,
    total: int = 100,
    eligible: int = 50,
    buildable: int = 50,
    active: int = 20,
    divergent: int = 2,
    better: int = 1,
    same: int = 0,
    worse: int = 1,
    delta_sum: int = 0,
) -> measurement.OracleSensitivityMainSeedResult:
    return measurement.OracleSensitivityMainSeedResult(
        seed=seed,
        total_decisions=total,
        discard_eligible_decisions=eligible,
        oracle_buildable_decisions=buildable,
        consumer_active_decisions=active,
        action_divergences=divergent,
        proxy_oracle_better=better,
        proxy_same=same,
        proxy_oracle_worse=worse,
        proxy_delta_sum=delta_sum,
        active_kind_counts=(("turn", active),),
        divergence_kind_counts=(("turn", divergent),),
    )


class WilsonIntervalTest(unittest.TestCase):
    def test_preregistered_five_percent_classification_boundaries(self) -> None:
        insensitive = measurement._wilson_95(10, 400)
        inconclusive = measurement._wilson_95(20, 400)
        sensitive = measurement._wilson_95(30, 400)

        self.assertIsNotNone(insensitive)
        self.assertIsNotNone(inconclusive)
        self.assertIsNotNone(sensitive)
        self.assertLess(insensitive.high, 0.05)
        self.assertLess(inconclusive.low, 0.05)
        self.assertGreater(inconclusive.high, 0.05)
        self.assertGreater(sensitive.low, 0.05)

    def test_wilson_rejects_invalid_counts(self) -> None:
        with self.assertRaises(ValueError):
            measurement._wilson_95(2, 1)
        with self.assertRaises(TypeError):
            measurement._wilson_95(1.0, 2)


class MainAggregationTest(unittest.TestCase):
    def test_aggregate_preserves_paired_denominators_and_proxy_counts(self) -> None:
        summary = measurement._aggregate_main_results(
            (
                _result(20, active=200, divergent=10, better=4, same=3, worse=3),
                _result(21, active=200, divergent=10, better=5, same=2, worse=3),
            )
        )

        self.assertEqual(summary.seeds, (20, 21))
        self.assertEqual(summary.total_decisions, 200)
        self.assertEqual(summary.discard_eligible_decisions, 100)
        self.assertEqual(summary.oracle_buildable_decisions, 100)
        self.assertEqual(summary.consumer_active_decisions, 400)
        self.assertEqual(summary.action_divergences, 20)
        self.assertEqual(summary.proxy_compared_positions, 20)
        self.assertEqual(summary.proxy_oracle_better, 9)
        self.assertEqual(summary.proxy_same, 5)
        self.assertEqual(summary.proxy_oracle_worse, 6)
        self.assertEqual(summary.action_divergence_rate, 0.05)
        self.assertEqual(summary.overall_divergence_rate, 0.2)
        self.assertEqual(
            summary.materiality_classification,
            "inconclusive relative to 5% threshold",
        )

    def test_cli_dataset_is_locked_to_fresh_seeds_20_through_29(self) -> None:
        with patch.object(
            measurement,
            "run_oracle_sensitivity_main_seed",
            side_effect=lambda seed: _result(seed, active=50, divergent=0, better=0, worse=0),
        ) as run_seed:
            summary = measurement.run_oracle_sensitivity_main()

        self.assertEqual(summary.seeds, tuple(range(20, 30)))
        self.assertEqual(
            [call.args[0] for call in run_seed.call_args_list],
            list(range(20, 30)),
        )
        self.assertEqual(summary.consumer_active_decisions, 500)


class LiveWallProxyBoundaryTest(unittest.TestCase):
    def test_proxy_counts_only_current_live_wall_copies_of_effective_type(self) -> None:
        match_state = MatchState(seed=_SEED, rules=RuleSet.default())
        match_state.start_round()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        round_state.draw(EngineSeat.EAST)

        observation = build_seat_observation(match_state, EngineSeat.EAST)
        policy_input = build_policy_input(observation)
        action = DiscardAction(
            actor=policy_input.self_seat,
            tile=policy_input.own_hand.concealed_tiles[0],
            tsumogiri=False,
        )
        target_type = tile_from_public_tile(
            public_tile(round_state.remaining_tiles[0])
        ).tile_type
        expected = sum(
            1
            for tile in round_state.remaining_tiles
            if tile_from_public_tile(public_tile(tile)).tile_type == target_type
        )

        with patch.object(
            measurement,
            "_effective_tile_types",
            return_value=(target_type,),
        ):
            actual = measurement._live_wall_structural_ukeire(
                match_state,
                policy_input,
                action,
            )

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
