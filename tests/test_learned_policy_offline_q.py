"""Offline Q macro-transition dataset contract tests (Issue #140).

macro-transition boundary（same actor / same round次decision探索、terminal
score binding、cross-round bootstrapしないこと）を、synthetic
`LocalGameInspection`で検証する。実RiichiEnv実行は行わない。
"""

import unittest

from _learned_policy_offline_q_fixtures import (
    eligible_discard_decision,
    forced_discard_decision,
    make_recording,
    make_round_state,
    make_step,
    riichi_choice_decision,
)
from lisjong.policy_contract import Seat, Wind

from lisjong_arena.learned_policy_offline_q.errors import OfflineQTransitionError
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
)
from lisjong_arena.learned_policy_offline_q.transitions import (
    build_macro_transitions,
    is_eligible_ordinary_discard,
)
from lisjong_arena.learned_policy_stage2.recording import iter_recorded_decisions

_SEED = 245
_ALL_FLAT = (25000, 25000, 25000, 25000)


def _two_round_scenario():
    round_a = make_round_state(Wind.EAST, 1)
    round_b = make_round_state(Wind.EAST, 2)

    step0 = make_step(
        0,
        (
            eligible_discard_decision(
                Seat.SEAT_0, round_a, _ALL_FLAT, legal_ranks=(1, 2, 3), selected_rank=1
            ),
        ),
    )
    step1 = make_step(
        1,
        (
            eligible_discard_decision(
                Seat.SEAT_1, round_a, _ALL_FLAT, legal_ranks=(4, 5), selected_rank=4
            ),
        ),
    )
    step2 = make_step(
        2,
        (forced_discard_decision(Seat.SEAT_0, round_a, _ALL_FLAT, rank=6),),
    )
    step3 = make_step(
        3,
        (
            eligible_discard_decision(
                Seat.SEAT_0, round_a, _ALL_FLAT, legal_ranks=(2, 3), selected_rank=2
            ),
        ),
    )
    step4 = make_step(
        4,
        (riichi_choice_decision(Seat.SEAT_0, round_a, _ALL_FLAT),),
    )
    round_b_scores = (26000, 24000, 25000, 25000)
    step5 = make_step(
        5,
        (
            eligible_discard_decision(
                Seat.SEAT_0,
                round_b,
                round_b_scores,
                legal_ranks=(5, 6),
                selected_rank=5,
            ),
        ),
    )

    final_scores = (27000, 23000, 25000, 25000)
    recording = make_recording(
        _SEED,
        Split.TRAIN,
        (step0, step1, step2, step3, step4, step5),
        final_scores,
    )
    return recording


class EligibilityTest(unittest.TestCase):
    def test_all_discard_choice_decision_is_eligible(self):
        round_a = make_round_state(Wind.EAST, 1)
        decision = eligible_discard_decision(
            Seat.SEAT_0, round_a, _ALL_FLAT, legal_ranks=(1, 2), selected_rank=1
        )
        recording = make_recording(
            _SEED, Split.TRAIN, (make_step(0, (decision,)),), _ALL_FLAT
        )
        (recorded,) = iter_recorded_decisions(recording)
        self.assertTrue(is_eligible_ordinary_discard(recorded))

    def test_forced_single_action_decision_is_not_eligible(self):
        round_a = make_round_state(Wind.EAST, 1)
        decision = forced_discard_decision(Seat.SEAT_0, round_a, _ALL_FLAT, rank=1)
        recording = make_recording(
            _SEED, Split.TRAIN, (make_step(0, (decision,)),), _ALL_FLAT
        )
        (recorded,) = iter_recorded_decisions(recording)
        self.assertFalse(is_eligible_ordinary_discard(recorded))

    def test_mixed_riichi_and_discard_choice_is_not_eligible(self):
        round_a = make_round_state(Wind.EAST, 1)
        decision = riichi_choice_decision(Seat.SEAT_0, round_a, _ALL_FLAT)
        recording = make_recording(
            _SEED, Split.TRAIN, (make_step(0, (decision,)),), _ALL_FLAT
        )
        (recorded,) = iter_recorded_decisions(recording)
        self.assertFalse(is_eligible_ordinary_discard(recorded))


class MacroTransitionBoundaryTest(unittest.TestCase):
    def test_nonterminal_transition_binds_the_next_eligible_decision_for_the_same_actor(
        self,
    ):
        rows = list(build_macro_transitions(_two_round_scenario()))
        seat0_round_a = [
            row for row in rows if row.actor_seat == 0 and row.round_ordinal == 0
        ]
        self.assertEqual(len(seat0_round_a), 2)

        first, second = seat0_round_a
        self.assertFalse(first.terminal)
        self.assertEqual(first.decision_ordinal, 0)
        self.assertEqual(first.next_decision_ordinal, 3)
        self.assertEqual(first.next_step_ordinal, 3)
        self.assertAlmostEqual(first.reward, 0.0)
        self.assertIsNotNone(first.next_feature_values)
        self.assertIsNotNone(first.next_legal_mask)

        self.assertTrue(second.terminal)
        self.assertEqual(second.decision_ordinal, 3)
        self.assertIsNone(second.next_decision_ordinal)
        self.assertIsNone(second.next_feature_values)
        self.assertIsNone(second.next_legal_mask)
        # round A settled score for seat 0 = 26000 (round B's first snapshot),
        # current score at the terminal decision = 25000.
        self.assertAlmostEqual(second.reward, 0.1)

    def test_terminal_transition_at_round_boundary_uses_the_next_rounds_settled_score(
        self,
    ):
        rows = list(build_macro_transitions(_two_round_scenario()))
        (seat1_row,) = [
            row for row in rows if row.actor_seat == 1 and row.round_ordinal == 0
        ]
        self.assertTrue(seat1_row.terminal)
        # round A settled score for seat 1 = 24000, current score = 25000.
        self.assertAlmostEqual(seat1_row.reward, -0.1)

    def test_terminal_transition_at_the_final_round_uses_local_game_result_scores(self):
        rows = list(build_macro_transitions(_two_round_scenario()))
        (seat0_round_b,) = [
            row for row in rows if row.actor_seat == 0 and row.round_ordinal == 1
        ]
        self.assertTrue(seat0_round_b.terminal)
        # final scores = 27000 for seat 0, current score at that decision = 26000.
        self.assertAlmostEqual(seat0_round_b.reward, 0.1)

    def test_ineligible_decisions_do_not_appear_as_their_own_rows_or_break_binding(
        self,
    ):
        rows = list(build_macro_transitions(_two_round_scenario()))
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertGreaterEqual(row.legal_action_count, 2)

    def test_reward_never_crosses_a_round_boundary_without_a_terminal_flag(self):
        rows = list(build_macro_transitions(_two_round_scenario()))
        for row in rows:
            if not row.terminal:
                self.assertEqual(row.round_ordinal, 0)
                self.assertEqual(row.next_decision_ordinal, 3)

    def test_reappearing_round_identity_fails_closed(self):
        round_a = make_round_state(Wind.EAST, 1)
        round_b = make_round_state(Wind.EAST, 2)
        step0 = make_step(
            0,
            (
                eligible_discard_decision(
                    Seat.SEAT_0, round_a, _ALL_FLAT, legal_ranks=(1, 2), selected_rank=1
                ),
            ),
        )
        step1 = make_step(
            1,
            (
                eligible_discard_decision(
                    Seat.SEAT_0, round_b, _ALL_FLAT, legal_ranks=(1, 2), selected_rank=1
                ),
            ),
        )
        step2 = make_step(
            2,
            (
                eligible_discard_decision(
                    Seat.SEAT_0, round_a, _ALL_FLAT, legal_ranks=(1, 2), selected_rank=1
                ),
            ),
        )
        recording = make_recording(_SEED, Split.TRAIN, (step0, step1, step2), _ALL_FLAT)
        with self.assertRaises(OfflineQTransitionError):
            list(build_macro_transitions(recording))


class MacroTransitionRowInvariantTest(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        feature = tuple(0.0 for _ in range(FEATURE_DIMENSION))
        mask = tuple(index in (0, 1) for index in range(VOCABULARY_SIZE))
        kwargs = dict(
            seed=_SEED,
            split=Split.TRAIN,
            round_ordinal=0,
            round_wind="east",
            hand_number=1,
            honba=0,
            actor_seat=0,
            step_ordinal=0,
            decision_ordinal=0,
            feature_values=feature,
            legal_mask=mask,
            behavior_action_index=0,
            behavior_action_family="discard",
            reward=0.0,
            terminal=True,
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )
        kwargs.update(overrides)
        return kwargs

    def test_terminal_row_with_a_next_state_fails_closed(self):
        feature = tuple(0.0 for _ in range(FEATURE_DIMENSION))
        mask = tuple(index in (0, 1) for index in range(VOCABULARY_SIZE))
        with self.assertRaises(OfflineQTransitionError):
            MacroTransitionRow(
                **self._base_kwargs(
                    terminal=True,
                    next_step_ordinal=1,
                    next_decision_ordinal=1,
                    next_feature_values=feature,
                    next_legal_mask=mask,
                )
            )

    def test_nonterminal_row_without_a_next_state_fails_closed(self):
        with self.assertRaises(OfflineQTransitionError):
            MacroTransitionRow(**self._base_kwargs(terminal=False))

    def test_next_decision_ordinal_must_be_strictly_later(self):
        feature = tuple(0.0 for _ in range(FEATURE_DIMENSION))
        mask = tuple(index in (0, 1) for index in range(VOCABULARY_SIZE))
        with self.assertRaises(OfflineQTransitionError):
            MacroTransitionRow(
                **self._base_kwargs(
                    terminal=False,
                    decision_ordinal=5,
                    next_step_ordinal=5,
                    next_decision_ordinal=5,
                    next_feature_values=feature,
                    next_legal_mask=mask,
                )
            )

    def test_behavior_action_must_be_legal(self):
        with self.assertRaises(OfflineQTransitionError):
            MacroTransitionRow(**self._base_kwargs(behavior_action_index=2))

    def test_a_single_legal_action_row_fails_closed(self):
        mask = tuple(index == 0 for index in range(VOCABULARY_SIZE))
        with self.assertRaises(OfflineQTransitionError):
            MacroTransitionRow(**self._base_kwargs(legal_mask=mask))


if __name__ == "__main__":
    unittest.main()
