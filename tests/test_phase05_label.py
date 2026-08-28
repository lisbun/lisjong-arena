"""lisjong-project #22 Phase 0.5 omniscient label builder tests。"""

import unittest
from unittest.mock import patch

from lisjong.belief import tile_type_index, wind_for_seat, wind_index
from lisjong.policy_contract import Wind
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.public_state import public_tile
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    tile_from_public_tile,
)
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase05_belief_slice.label import (
    Phase05LabelExclusionReason,
    Phase05LabelResult,
    Phase05Labels,
    build_phase05_labels,
)

_SEED = 20260827


def _dealer_turn_state() -> tuple[MatchState, object]:
    """親のTURN decision phaseまで進めた最小のreal engine state。"""
    match_state = MatchState(seed=_SEED, rules=RuleSet.default())
    match_state.start_round()
    round_state = match_state.active_round
    if round_state is None:
        raise AssertionError("start_round must produce an active round")
    round_state.draw(EngineSeat.EAST)
    observation = build_seat_observation(match_state, EngineSeat.EAST)
    return match_state, build_policy_input(observation)


class LabelResultContractTest(unittest.TestCase):
    def test_result_requires_exactly_one_of_labels_or_reason(self) -> None:
        with self.assertRaises(ValueError):
            Phase05LabelResult(labels=None, exclusion_reason=None)

    def test_labels_reject_row_sum_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            Phase05Labels(
                opponent_winds=(Wind.SOUTH, Wind.WEST, Wind.NORTH),
                counts=((0,) * 34,) * 3,
                concealed_sizes=(13, 13, 13),
            )


class ExactLabelTest(unittest.TestCase):
    def test_labels_match_the_realized_opponent_concealed_hands(self) -> None:
        match_state, policy_input = _dealer_turn_state()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)

        result = build_phase05_labels(match_state, policy_input)

        self.assertIsNone(result.exclusion_reason)
        labels = result.labels
        self.assertIsNotNone(labels)
        dealer_seat = policy_input.round.dealer_seat
        for offset, wind in enumerate(labels.opponent_winds):
            engine_seat = next(
                candidate
                for candidate in EngineSeat
                if wind_index(
                    wind_for_seat(seat_from_engine_seat(candidate), dealer_seat)
                )
                == wind_index(wind)
            )
            expected = [0] * 34
            for engine_tile in round_state.hand_tiles(engine_seat):
                tile = tile_from_public_tile(public_tile(engine_tile))
                expected[tile_type_index(tile.tile_type)] += 1
            self.assertEqual(labels.counts[offset], tuple(expected))
            self.assertEqual(labels.concealed_sizes[offset], 13)

    def test_viewer_row_is_not_included_in_labels(self) -> None:
        match_state, policy_input = _dealer_turn_state()

        labels = build_phase05_labels(match_state, policy_input).labels

        self.assertIsNotNone(labels)
        viewer_wind = wind_for_seat(
            policy_input.self_seat,
            policy_input.round.dealer_seat,
        )
        self.assertNotIn(viewer_wind, labels.opponent_winds)
        self.assertEqual(len(labels.opponent_winds), 3)

    def test_unstable_opponent_hand_size_is_reason_coded(self) -> None:
        match_state, policy_input = _dealer_turn_state()
        round_state = match_state.active_round
        self.assertIsNotNone(round_state)
        original_hand_tiles = type(round_state).hand_tiles

        def transient_extra_tile(self, seat: EngineSeat):
            tiles = original_hand_tiles(self, seat)
            if seat is EngineSeat.SOUTH:
                return tiles + (tiles[0],)
            return tiles

        with patch.object(type(round_state), "hand_tiles", transient_extra_tile):
            result = build_phase05_labels(match_state, policy_input)

        self.assertIsNone(result.labels)
        self.assertEqual(
            result.exclusion_reason,
            Phase05LabelExclusionReason.UNSTABLE_OPPONENT_HAND_SIZE,
        )

    def test_missing_active_round_is_reason_coded(self) -> None:
        match_state, policy_input = _dealer_turn_state()

        with patch.object(
            type(match_state),
            "active_round",
            property(lambda self: None),
        ):
            result = build_phase05_labels(match_state, policy_input)

        self.assertIsNone(result.labels)
        self.assertEqual(
            result.exclusion_reason,
            Phase05LabelExclusionReason.NO_ACTIVE_ROUND,
        )

    def test_builder_requires_engine_match_state(self) -> None:
        match_state, policy_input = _dealer_turn_state()

        with self.assertRaises(TypeError):
            build_phase05_labels(object(), policy_input)
        with self.assertRaises(TypeError):
            build_phase05_labels(match_state, object())


if __name__ == "__main__":
    unittest.main()
