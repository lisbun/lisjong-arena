from __future__ import annotations

import unittest

from lisjong.policy_contract import Seat

from lisjong_arena.champion_hand_value_v2_screen.evidence import classify_interval
from lisjong_arena.champion_hand_value_v2_screen.protocol import (
    BASELINE_CLASS_NAME,
    CANDIDATE_CLASS_NAME,
    ChampionHandValueV2ProtocolError,
    baseline_factory,
    candidate_factory,
    require_population,
    seed_freshness_block,
)
from lisjong_arena.champion_hand_value_v2_screen.trace import (
    CompositionDecisionRecord,
    GameCompositionDiagnostics,
    aggregate_composition_diagnostics,
)


class ProtocolTest(unittest.TestCase):
    def test_population_is_exact_contiguous_100(self) -> None:
        seeds = tuple(range(52_200, 52_300))
        self.assertEqual(require_population(seeds), seeds)
        with self.assertRaises(ChampionHandValueV2ProtocolError):
            require_population(seeds[:-1])
        with self.assertRaises(ChampionHandValueV2ProtocolError):
            require_population(seeds[:-1] + (60_000,))

    def test_issue_270_population_is_rejected(self) -> None:
        with self.assertRaises(ChampionHandValueV2ProtocolError):
            seed_freshness_block(
                tuple(range(50_000, 50_100)),
                external_freshness_confirmed=True,
            )

    def test_candidate_range_requires_external_audit(self) -> None:
        seeds = tuple(range(52_200, 52_300))
        with self.assertRaises(ChampionHandValueV2ProtocolError):
            seed_freshness_block(
                seeds,
                external_freshness_confirmed=False,
            )
        freshness = seed_freshness_block(
            seeds,
            external_freshness_confirmed=True,
        )
        self.assertTrue(freshness["fresh"])

    def test_exact_policy_classes_resolve(self) -> None:
        self.assertEqual(type(candidate_factory()).__name__, CANDIDATE_CLASS_NAME)
        self.assertEqual(type(baseline_factory()).__name__, BASELINE_CLASS_NAME)


class ClassificationTest(unittest.TestCase):
    def test_three_way_classification(self) -> None:
        self.assertIn("POSITIVE", classify_interval(0.1, 1.0))
        self.assertIn("NEGATIVE", classify_interval(-1.0, -0.1))
        self.assertIn("INCONCLUSIVE", classify_interval(-0.1, 0.1))


class CompositionAggregationTest(unittest.TestCase):
    def test_aggregate_uses_existing_trace_fields(self) -> None:
        records = (
            CompositionDecisionRecord(
                seed=52_200,
                rotation=0,
                ordinal=3,
                candidate_seat=Seat(0),
                selection_source="CHAMPION_TARGETED_HONOR_RELEASE",
                champion_activation_stage="R5_HONOR_ONLY_SWITCH",
                hand_value_v2_attempted=False,
                action_changed_vs_champion=False,
                action_changed_vs_former_parent=True,
            ),
            CompositionDecisionRecord(
                seed=52_200,
                rotation=0,
                ordinal=5,
                candidate_seat=Seat(0),
                selection_source="HAND_VALUE_V2",
                champion_activation_stage=None,
                hand_value_v2_attempted=True,
                action_changed_vs_champion=True,
                action_changed_vs_former_parent=True,
            ),
            CompositionDecisionRecord(
                seed=52_200,
                rotation=0,
                ordinal=6,
                candidate_seat=Seat(0),
                selection_source="SHARED_ACTION",
                champion_activation_stage=None,
                hand_value_v2_attempted=True,
                action_changed_vs_champion=False,
                action_changed_vs_former_parent=False,
            ),
        )
        game = GameCompositionDiagnostics(
            seed=52_200,
            rotation=0,
            candidate_seat=Seat(0),
            focal_decision_count=7,
            discard_decision_count=3,
            choice_discard_decision_count=2,
            forced_discard_decision_count=1,
            candidate_runtime_total_seconds=1.0,
            game_wall_clock_seconds=2.0,
            records=records,
        )
        aggregate = aggregate_composition_diagnostics(
            (game,),
            replay_wall_clock_seconds=2.0,
        )
        self.assertEqual(aggregate.champion_targeted_preserved_count, 1)
        self.assertEqual(aggregate.hand_value_v2_attempted_count, 2)
        self.assertEqual(aggregate.action_changed_vs_champion_count, 1)
        self.assertEqual(
            aggregate.selection_source_counts,
            {
                "CHAMPION_TARGETED_HONOR_RELEASE": 1,
                "HAND_VALUE_V2": 1,
                "SHARED_ACTION": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
