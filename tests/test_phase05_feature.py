"""lisjong-project #22 Phase 0.5 feature encoder tests。"""

import unittest

from lisjong.belief import tile_type_from_index, tile_type_index
from lisjong.policy_contract import (
    RiichiState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.phase05_belief_slice.feature import (
    OPPONENT_COUNT,
    OpponentDiscardBucket,
    Phase05Feature,
    TurnBucket,
    encode_phase05_anchor_features,
)
from tests import _phase05_fixtures as fixtures


def _manzu_index(rank: int) -> int:
    return tile_type_index(fixtures.manzu(rank).tile_type)


def _is_suited_five(tile_type: TileType) -> bool:
    """赤5 inventoryを壊さないよう、数牌の5だけpoolの枚数を減らすための判定。"""
    return tile_type.category is not TileCategory.HONOR and tile_type.rank == 5


class FeatureValueTest(unittest.TestCase):
    def test_feature_rejects_viewer_wind_as_opponent(self) -> None:
        with self.assertRaises(ValueError):
            Phase05Feature(
                viewer_wind=Wind.EAST,
                opponent_wind=Wind.EAST,
                tile_type=fixtures.manzu(1).tile_type,
                remaining_tile_count=4,
                opponent_meld_count=0,
                opponent_riichi_state=RiichiState.NONE,
                turn_bucket=TurnBucket.EARLY,
                opponent_discard_bucket=OpponentDiscardBucket.NONE,
            )

    def test_feature_rejects_out_of_range_remaining_count(self) -> None:
        with self.assertRaises(ValueError):
            Phase05Feature(
                viewer_wind=Wind.EAST,
                opponent_wind=Wind.SOUTH,
                tile_type=fixtures.manzu(1).tile_type,
                remaining_tile_count=5,
                opponent_meld_count=0,
                opponent_riichi_state=RiichiState.NONE,
                turn_bucket=TurnBucket.EARLY,
                opponent_discard_bucket=OpponentDiscardBucket.NONE,
            )


class AnchorFeatureEncodingTest(unittest.TestCase):
    def test_encoder_requires_a_policy_input(self) -> None:
        with self.assertRaises(TypeError):
            encode_phase05_anchor_features(object())

    def test_opponent_rows_exclude_the_viewer_wind_in_canonical_order(self) -> None:
        features = encode_phase05_anchor_features(
            fixtures.policy_input(
                self_seat=Seat.SEAT_1,
                dealer_seat=Seat.SEAT_0,
            )
        )

        self.assertEqual(features.viewer_wind, Wind.SOUTH)
        self.assertEqual(
            features.opponent_winds,
            (Wind.EAST, Wind.WEST, Wind.NORTH),
        )
        self.assertEqual(len(features.features), OPPONENT_COUNT * 34)

    def test_remaining_count_reflects_viewer_safe_public_accounting(self) -> None:
        target = fixtures.manzu(3)
        features = encode_phase05_anchor_features(
            fixtures.policy_input(
                players=(
                    fixtures.player(),
                    fixtures.player(discards=(fixtures.discard(target, 0),)),
                    fixtures.player(),
                    fixtures.player(),
                ),
                own_tiles=(target,),
            )
        )

        index = _manzu_index(3)
        self.assertEqual(features.remaining_tile_counts[index], 2)
        self.assertEqual(features.feature(0, index).remaining_tile_count, 2)

    def test_red_five_shares_the_base_tile_kind_axis(self) -> None:
        features = encode_phase05_anchor_features(
            fixtures.policy_input(
                own_tiles=(
                    fixtures.manzu(5, is_red=True),
                    fixtures.manzu(5),
                ),
            )
        )

        self.assertEqual(features.remaining_tile_counts[_manzu_index(5)], 2)

    def test_opponent_meld_count_and_riichi_state_are_public_only(self) -> None:
        features = encode_phase05_anchor_features(
            fixtures.policy_input(
                players=(
                    fixtures.player(),
                    fixtures.player(),
                    fixtures.player(
                        melds=(fixtures.pon(fixtures.manzu(9), from_seat=Seat.SEAT_1),),
                        riichi=RiichiState.ACCEPTED,
                    ),
                    fixtures.player(),
                ),
            )
        )

        west_offset = features.opponent_winds.index(Wind.WEST)
        self.assertEqual(features.opponent_meld_counts[west_offset], 1)
        self.assertEqual(
            features.feature(west_offset, 0).opponent_riichi_state,
            RiichiState.ACCEPTED,
        )
        south_offset = features.opponent_winds.index(Wind.SOUTH)
        self.assertEqual(
            features.feature(south_offset, 0).opponent_riichi_state,
            RiichiState.NONE,
        )


class BucketBoundaryTest(unittest.TestCase):
    def _turn_bucket(self, total_discards: int) -> TurnBucket:
        """物理的に成立する範囲でround-global discard数だけを変える。"""
        pool = [
            tile_type
            for index in range(34)
            for tile_type in (tile_type_from_index(index),)
            if not _is_suited_five(tile_type)
            for _ in range(4)
        ]
        per_seat = [total_discards // 4] * 4
        for index in range(total_discards % 4):
            per_seat[index] += 1

        order = 0
        players = []
        for count in per_seat:
            discards = []
            for _ in range(count):
                tile_type = pool[order]
                discards.append(
                    fixtures.discard(Tile(tile_type), order),
                )
                order += 1
            players.append(fixtures.player(discards=tuple(discards)))

        features = encode_phase05_anchor_features(
            fixtures.policy_input(players=tuple(players))
        )
        return features.feature(0, 0).turn_bucket

    def test_turn_bucket_boundaries_follow_the_locked_definition(self) -> None:
        self.assertEqual(self._turn_bucket(0), TurnBucket.EARLY)
        self.assertEqual(self._turn_bucket(23), TurnBucket.EARLY)
        self.assertEqual(self._turn_bucket(24), TurnBucket.MIDDLE)
        self.assertEqual(self._turn_bucket(47), TurnBucket.MIDDLE)
        self.assertEqual(self._turn_bucket(48), TurnBucket.LATE)

    def _discard_bucket(self, copies: int) -> OpponentDiscardBucket:
        target = fixtures.manzu(2)
        players = (
            fixtures.player(),
            fixtures.player(
                discards=tuple(
                    fixtures.discard(target, order) for order in range(copies)
                )
            ),
            fixtures.player(),
            fixtures.player(),
        )
        features = encode_phase05_anchor_features(
            fixtures.policy_input(players=players)
        )
        offset = features.opponent_winds.index(Wind.SOUTH)
        return features.feature(offset, _manzu_index(2)).opponent_discard_bucket

    def test_opponent_discard_bucket_boundaries(self) -> None:
        self.assertEqual(self._discard_bucket(0), OpponentDiscardBucket.NONE)
        self.assertEqual(self._discard_bucket(1), OpponentDiscardBucket.ONE)
        self.assertEqual(self._discard_bucket(2), OpponentDiscardBucket.MANY)
        self.assertEqual(self._discard_bucket(3), OpponentDiscardBucket.MANY)

    def test_discard_bucket_is_per_opponent_not_global(self) -> None:
        target = fixtures.manzu(2)
        players = (
            fixtures.player(),
            fixtures.player(discards=(fixtures.discard(target, 0),)),
            fixtures.player(discards=(fixtures.discard(target, 1),)),
            fixtures.player(),
        )
        features = encode_phase05_anchor_features(
            fixtures.policy_input(players=players)
        )

        north_offset = features.opponent_winds.index(Wind.NORTH)
        self.assertEqual(
            features.feature(north_offset, _manzu_index(2)).opponent_discard_bucket,
            OpponentDiscardBucket.NONE,
        )


if __name__ == "__main__":
    unittest.main()
