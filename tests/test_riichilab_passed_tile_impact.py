"""Issue #503: 確定安全牌候補の影響範囲の計数を、合成logで固定する。"""

import unittest
from dataclasses import replace

from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState
from test_riichilab_r1_choice_loss_diagnostic import (
    BOT_ID,
    _discard_choices,
    _passed_tile_log,
)

from lisjong_arena.riichilab_passed_tile_impact.impact import (
    CATEGORY_NO_SAFE,
    CATEGORY_S1,
    ImpactCounter,
    classify_applicability,
)


def _discards(observation):
    return tuple(
        action
        for action in observation.decision.legal_actions
        if isinstance(action, DiscardAction)
    )


class ApplicabilityTests(unittest.TestCase):
    def test_tile_passed_after_the_declaration_is_s1(self):
        # seat 0のリーチ後にseat 1が切った1sが、seat 2の合法打牌にある。
        observation = _discard_choices(_passed_tile_log(), 2)[0]

        result = classify_applicability(
            observation.decision.input, _discards(observation)
        )

        self.assertEqual(result.category, CATEGORY_S1)
        self.assertTrue(result.declaration_known)
        self.assertEqual(result.riichi_players, 1)

    def test_no_safe_tile_among_legal_discards(self):
        observation = _discard_choices(_passed_tile_log(), 1)[0]

        result = classify_applicability(
            observation.decision.input, _discards(observation)
        )

        self.assertEqual(result.category, CATEGORY_NO_SAFE)

    def test_category_does_not_depend_on_the_played_discard(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        other = next(
            action
            for action in _discards(observation)
            if action != observation.teacher_action
        )
        swapped = replace(observation, teacher_action=other)

        self.assertEqual(
            classify_applicability(swapped.decision.input, _discards(swapped)),
            classify_applicability(observation.decision.input, _discards(observation)),
        )

    def test_no_riichi_player_has_no_category(self):
        observation = _discard_choices(_passed_tile_log(), 2)[0]
        policy_input = observation.decision.input
        players = tuple(
            replace(
                player,
                riichi=RiichiState.NONE,
                riichi_declaration_discard_order=None,
            )
            for player in policy_input.players
        )

        result = classify_applicability(
            replace(policy_input, players=players), _discards(observation)
        )

        self.assertIsNone(result.category)
        self.assertEqual(result.riichi_players, 0)


class CounterTests(unittest.TestCase):
    def test_counts_target_rows_without_using_analysis_or_the_played_discard(self):
        counter = ImpactCounter()
        counter.analyze_game(
            "dev",
            game_id="fixture",
            seat=2,
            bot_id=BOT_ID,
            events=_passed_tile_log(),
        )

        counts = counter.document(("dev",))["counts"]

        self.assertEqual(counts["rows|S1"], 1)
        self.assertEqual(counts["rows|engaged"], 1)
        self.assertEqual(counts["r1_rows|S1"], 1)
        self.assertEqual(counts["legacy_passed_only|minimum_shanten_1plus"], 1)
        self.assertNotIn("unexpected_change_outside_engaged", counts)
        # 候補とChampionの打牌の比較は、対象局面の数とは別に数える。
        self.assertLessEqual(counts.get("changed|S1", 0), counts["rows|S1"])


if __name__ == "__main__":
    unittest.main()
