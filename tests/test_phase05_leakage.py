"""lisjong-project #22 Phase 0.5 leakage boundary tests。

Issue #22が最低限要求する4つのcheckを固定する。

1. feature encoder type boundary
2. feature / label separation
3. player-safe replay equivalence
4. forbidden information
"""

import copy
import inspect
import unittest

from lisjong.belief import derive_remaining_tile_inventory
from lisjong.policy_contract import PolicyInput, RiichiState, TileType, Wind
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase05_belief_slice.feature import (
    OpponentDiscardBucket,
    Phase05Feature,
    TurnBucket,
    encode_phase05_anchor_features,
)
from lisjong_arena.phase05_belief_slice.label import build_phase05_labels

_SEED = 20260827
_ALLOWED_FEATURE_VALUE_TYPES = (
    Wind,
    TileType,
    RiichiState,
    TurnBucket,
    OpponentDiscardBucket,
    int,
)


def _dealer_turn_state() -> tuple[MatchState, PolicyInput]:
    match_state = MatchState(seed=_SEED, rules=RuleSet.default())
    match_state.start_round()
    round_state = match_state.active_round
    if round_state is None:
        raise AssertionError("start_round must produce an active round")
    round_state.draw(EngineSeat.EAST)
    return match_state, build_policy_input(
        build_seat_observation(match_state, EngineSeat.EAST)
    )


class FeatureEncoderTypeBoundaryTest(unittest.TestCase):
    """leakage check 1: encoderはPolicyInputだけを受け取る。"""

    def test_encoder_signature_takes_only_a_policy_input(self) -> None:
        signature = inspect.signature(encode_phase05_anchor_features)

        self.assertEqual(list(signature.parameters), ["policy_input"])
        self.assertIs(
            signature.parameters["policy_input"].annotation,
            PolicyInput,
        )

    def test_encoder_rejects_the_omniscient_match_state(self) -> None:
        match_state, _ = _dealer_turn_state()

        with self.assertRaises(TypeError):
            encode_phase05_anchor_features(match_state)


class FeatureLabelSeparationTest(unittest.TestCase):
    """leakage check 2: label生成はfeature valueを書き換えない。"""

    def test_features_are_identical_before_and_after_label_attachment(self) -> None:
        match_state, policy_input = _dealer_turn_state()

        before = encode_phase05_anchor_features(policy_input)
        snapshot = copy.deepcopy(before)
        label_result = build_phase05_labels(match_state, policy_input)
        after = encode_phase05_anchor_features(policy_input)

        self.assertIsNotNone(label_result.labels)
        self.assertEqual(before, snapshot)
        self.assertEqual(before, after)


class PlayerSafeReplayEquivalenceTest(unittest.TestCase):
    """leakage check 3: player-safe projectionからのreplayが同じfeatureになる。"""

    def test_replayed_seat_safe_projection_reproduces_the_features(self) -> None:
        match_state, policy_input = _dealer_turn_state()

        direct = encode_phase05_anchor_features(policy_input)
        replayed = encode_phase05_anchor_features(copy.deepcopy(policy_input))
        rebuilt = encode_phase05_anchor_features(
            build_policy_input(build_seat_observation(match_state, EngineSeat.EAST))
        )

        self.assertEqual(direct, replayed)
        self.assertEqual(direct, rebuilt)


class ForbiddenInformationTest(unittest.TestCase):
    """leakage check 4: hidden truthがfeatureへ入らない。"""

    def test_feature_fields_are_limited_to_public_projection_values(self) -> None:
        forbidden = {
            "ron_capable",
            "pon_capable",
            "chi_capable",
            "kan_capable",
            "drawn_tile",
            "hidden_hand",
            "opponent_hand",
            "wall",
            "future",
        }
        field_names = set(Phase05Feature.__dataclass_fields__)

        self.assertEqual(field_names & forbidden, set())
        self.assertEqual(
            field_names,
            {
                "viewer_wind",
                "opponent_wind",
                "tile_type",
                "remaining_tile_count",
                "opponent_meld_count",
                "opponent_riichi_state",
                "turn_bucket",
                "opponent_discard_bucket",
            },
        )

    def test_feature_values_carry_no_object_references_to_hidden_state(self) -> None:
        _, policy_input = _dealer_turn_state()

        features = encode_phase05_anchor_features(policy_input)

        for feature in features.features:
            for name in Phase05Feature.__dataclass_fields__:
                value = getattr(feature, name)
                self.assertIsInstance(value, _ALLOWED_FEATURE_VALUE_TYPES)

    def test_remaining_count_never_exceeds_the_viewer_safe_inventory(self) -> None:
        """featureのremaining countはviewer-safe accountingそのものである。"""
        _, policy_input = _dealer_turn_state()

        features = encode_phase05_anchor_features(policy_input)
        expected = derive_remaining_tile_inventory(policy_input).remaining_tile_counts

        self.assertEqual(features.remaining_tile_counts, expected)
        for offset in range(len(features.opponent_winds)):
            for tile_index, count in enumerate(expected):
                self.assertEqual(
                    features.feature(offset, tile_index).remaining_tile_count,
                    count,
                )


if __name__ == "__main__":
    unittest.main()
