"""`SeatObservation` -> lisjong `PolicyInput`射影のcontract。

first-party engineでは、consumer-side history materializerなしにcurrent
decisionの`PolicyInput`を構成できることを固定する。
"""

import unittest

from _lisjong_engine_fixtures import (
    discard,
    honor,
    manzu,
    observation,
    pinzu,
    pon_meld,
    seat_discards,
    seat_melds,
    souzu,
)
from lisjong.policy_contract import (
    MeldKind,
    PolicyInput,
    RiichiState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.public_state import (
    PublicMeld as EnginePublicMeld,
)
from lisjong_engine.public_state import (
    PublicMeldType,
    PublicRiichiStatus,
    SeatDiscards,
    SeatMelds,
    SeatRiichiState,
    SeatScore,
)
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena.lisjong_engine.errors import ObservationProjectionError
from lisjong_arena.lisjong_engine.policy_input import build_policy_input


def _tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red)


class SelfSeatAndRoundTest(unittest.TestCase):
    def test_self_seat_comes_from_the_viewer_seat(self) -> None:
        for engine_seat, seat in zip(EngineSeat, Seat, strict=True):
            policy_input = build_policy_input(
                observation(viewer_seat=engine_seat, drawn_tile=None)
            )
            self.assertIs(policy_input.self_seat, seat)

    def test_prevailing_wind_becomes_round_wind(self) -> None:
        policy_input = build_policy_input(
            observation(prevailing_wind=EngineWind.SOUTH, hand_number=3)
        )
        self.assertIs(policy_input.round.round_wind, Wind.SOUTH)
        self.assertEqual(policy_input.round.hand_number, 3)

    def test_west_round_is_representable(self) -> None:
        policy_input = build_policy_input(observation(prevailing_wind=EngineWind.WEST))
        self.assertIs(policy_input.round.round_wind, Wind.WEST)

    def test_dealer_honba_and_riichi_sticks(self) -> None:
        policy_input = build_policy_input(
            observation(dealer_seat=EngineSeat.WEST, honba=4, riichi_sticks=2)
        )
        self.assertIs(policy_input.round.dealer_seat, Seat.SEAT_2)
        self.assertEqual(policy_input.round.honba, 4)
        self.assertEqual(policy_input.round.riichi_sticks, 2)

    def test_dora_indicator_order_is_preserved(self) -> None:
        indicators = (pinzu(3), manzu(9), honor(5), souzu(5, is_red=True))
        policy_input = build_policy_input(observation(dora_indicators=indicators))
        self.assertEqual(
            policy_input.round.dora_indicators,
            (
                _tile(TileCategory.PINZU, 3),
                _tile(TileCategory.MANZU, 9),
                _tile(TileCategory.HONOR, 5),
                _tile(TileCategory.SOUZU, 5, is_red=True),
            ),
        )

    def test_remaining_live_wall_count_becomes_live_wall_tiles_remaining(self) -> None:
        policy_input = build_policy_input(observation(remaining_live_wall_count=17))
        self.assertEqual(policy_input.round.live_wall_tiles_remaining, 17)


class PlayersTest(unittest.TestCase):
    def test_players_keep_the_lisjong_canonical_seat_order(self) -> None:
        policy_input = build_policy_input(
            observation(
                viewer_seat=EngineSeat.WEST,
                drawn_tile=None,
                scores=(
                    SeatScore(EngineSeat.EAST, 10_000),
                    SeatScore(EngineSeat.SOUTH, 20_000),
                    SeatScore(EngineSeat.WEST, 30_000),
                    SeatScore(EngineSeat.NORTH, 40_000),
                ),
            )
        )
        self.assertEqual(
            tuple(player.score for player in policy_input.players),
            (10_000, 20_000, 30_000, 40_000),
        )
        # self_seatをindex 0へrotateしない。
        self.assertIs(policy_input.self_seat, Seat.SEAT_2)
        self.assertEqual(policy_input.players[Seat.SEAT_2].score, 30_000)

    def test_negative_scores_are_preserved(self) -> None:
        policy_input = build_policy_input(
            observation(
                scores=tuple(
                    SeatScore(seat, -1_000 if seat is EngineSeat.NORTH else 25_000)
                    for seat in EngineSeat
                )
            )
        )
        self.assertEqual(policy_input.players[Seat.SEAT_3].score, -1_000)

    def test_discard_fields_are_projected(self) -> None:
        policy_input = build_policy_input(
            observation(
                discards=seat_discards(
                    south=(
                        discard(manzu(1), 0),
                        discard(
                            souzu(5, is_red=True),
                            2,
                            is_tsumogiri=True,
                            called_by=EngineSeat.NORTH,
                        ),
                    ),
                    west=(discard(pinzu(4), 1, is_riichi_declaration=True),),
                )
            )
        )
        south = policy_input.players[Seat.SEAT_1].discards
        self.assertEqual(len(south), 2)
        self.assertEqual(south[0].tile, _tile(TileCategory.MANZU, 1))
        self.assertFalse(south[0].tsumogiri)
        self.assertEqual(south[0].order, 0)
        self.assertIsNone(south[0].called_by)
        self.assertEqual(south[1].tile, _tile(TileCategory.SOUZU, 5, is_red=True))
        self.assertTrue(south[1].tsumogiri)
        self.assertEqual(south[1].order, 2)
        self.assertIs(south[1].called_by, Seat.SEAT_3)

    def test_round_global_discard_order_is_kept_across_seats(self) -> None:
        policy_input = build_policy_input(
            observation(
                discards=seat_discards(
                    east=(discard(manzu(1), 0), discard(manzu(2), 4)),
                    south=(discard(pinzu(1), 1),),
                    west=(discard(pinzu(2), 2),),
                    north=(discard(pinzu(3), 3),),
                )
            )
        )
        orders = sorted(
            entry.order for player in policy_input.players for entry in player.discards
        )
        self.assertEqual(orders, [0, 1, 2, 3, 4])

    def test_riichi_declaration_flag_is_not_smuggled_into_the_discard(self) -> None:
        """engineの`is_riichi_declaration`はlisjong `Discard`契約に対応fieldがない。"""
        policy_input = build_policy_input(
            observation(
                discards=seat_discards(
                    east=(discard(manzu(1), 0, is_riichi_declaration=True),)
                )
            )
        )
        entry = policy_input.players[Seat.SEAT_0].discards[0]
        self.assertFalse(hasattr(entry, "is_riichi_declaration"))
        self.assertFalse(entry.tsumogiri)
        self.assertIsNone(entry.called_by)

    def test_all_five_meld_types_are_projected(self) -> None:
        melds = (
            EnginePublicMeld(
                meld_type=PublicMeldType.CHI,
                tiles=(souzu(4), souzu(5), souzu(6)),
                from_seat=EngineSeat.NORTH,
                called_tile=souzu(4),
            ),
            pon_meld(manzu(3), EngineSeat.SOUTH),
            EnginePublicMeld(
                meld_type=PublicMeldType.DAIMINKAN,
                tiles=(pinzu(7),) * 4,
                from_seat=EngineSeat.WEST,
                called_tile=pinzu(7),
            ),
            EnginePublicMeld(
                meld_type=PublicMeldType.ANKAN,
                tiles=(honor(1),) * 4,
                from_seat=None,
                called_tile=None,
            ),
            EnginePublicMeld(
                meld_type=PublicMeldType.KAKAN,
                tiles=(pinzu(5), pinzu(5), pinzu(5), pinzu(5, is_red=True)),
                from_seat=EngineSeat.NORTH,
                called_tile=pinzu(5),
            ),
        )
        policy_input = build_policy_input(observation(melds=seat_melds(east=melds)))
        projected = policy_input.players[Seat.SEAT_0].melds
        self.assertEqual(
            tuple(meld.kind for meld in projected),
            (
                MeldKind.CHI,
                MeldKind.PON,
                MeldKind.DAIMINKAN,
                MeldKind.ANKAN,
                MeldKind.KAKAN,
            ),
        )
        self.assertIsNone(projected[3].from_seat)
        self.assertIsNone(projected[3].called_tile)

    def test_kakan_meld_keeps_the_original_pon_provenance(self) -> None:
        policy_input = build_policy_input(
            observation(
                melds=seat_melds(
                    east=(
                        EnginePublicMeld(
                            meld_type=PublicMeldType.KAKAN,
                            tiles=(
                                pinzu(5),
                                pinzu(5),
                                pinzu(5),
                                pinzu(5, is_red=True),
                            ),
                            from_seat=EngineSeat.SOUTH,
                            called_tile=pinzu(5),
                        ),
                    )
                )
            )
        )
        meld = policy_input.players[Seat.SEAT_0].melds[0]
        self.assertIs(meld.from_seat, Seat.SEAT_1)
        self.assertEqual(meld.called_tile, _tile(TileCategory.PINZU, 5))

    def test_riichi_states_are_projected_for_every_seat(self) -> None:
        policy_input = build_policy_input(
            observation(
                riichi_states=(
                    SeatRiichiState(EngineSeat.EAST, PublicRiichiStatus.NONE),
                    SeatRiichiState(EngineSeat.SOUTH, PublicRiichiStatus.PENDING),
                    SeatRiichiState(EngineSeat.WEST, PublicRiichiStatus.ESTABLISHED),
                    SeatRiichiState(EngineSeat.NORTH, PublicRiichiStatus.NONE),
                )
            )
        )
        self.assertEqual(
            tuple(player.riichi for player in policy_input.players),
            (
                RiichiState.NONE,
                RiichiState.DECLARED,
                RiichiState.ACCEPTED,
                RiichiState.NONE,
            ),
        )


class OwnHandTest(unittest.TestCase):
    def test_hand_tiles_become_concealed_tiles(self) -> None:
        hand = (manzu(1), manzu(1), pinzu(5, is_red=True), honor(7))
        policy_input = build_policy_input(
            observation(hand_tiles=hand, drawn_tile=honor(7))
        )
        self.assertEqual(
            sorted(
                policy_input.own_hand.concealed_tiles,
                key=lambda tile: (tile.tile_type.rank, tile.is_red),
            ),
            sorted(
                (
                    _tile(TileCategory.MANZU, 1),
                    _tile(TileCategory.MANZU, 1),
                    _tile(TileCategory.PINZU, 5, is_red=True),
                    _tile(TileCategory.HONOR, 7),
                ),
                key=lambda tile: (tile.tile_type.rank, tile.is_red),
            ),
        )

    def test_drawn_tile_comes_from_the_observation(self) -> None:
        policy_input = build_policy_input(
            observation(
                hand_tiles=(manzu(1), souzu(5, is_red=True)),
                drawn_tile=souzu(5, is_red=True),
            )
        )
        self.assertEqual(
            policy_input.own_hand.drawn_tile,
            _tile(TileCategory.SOUZU, 5, is_red=True),
        )

    def test_reaction_observations_keep_drawn_tile_absent(self) -> None:
        """Arena側でdrawn tileを推測しない。"""
        policy_input = build_policy_input(
            observation(
                decision_kind=ObservationDecisionKind.DISCARD_REACTION,
                hand_tiles=(manzu(1), manzu(2)),
                drawn_tile=None,
            )
        )
        self.assertIsNone(policy_input.own_hand.drawn_tile)

    def test_red_and_non_red_copies_stay_distinct(self) -> None:
        policy_input = build_policy_input(
            observation(
                hand_tiles=(souzu(5), souzu(5, is_red=True)),
                drawn_tile=souzu(5),
            )
        )
        self.assertEqual(
            sorted(
                (tile.is_red for tile in policy_input.own_hand.concealed_tiles),
            ),
            [False, True],
        )


class FailClosedTest(unittest.TestCase):
    def test_rejects_a_non_observation(self) -> None:
        with self.assertRaises(TypeError):
            build_policy_input(object())

    def test_rejects_seat_tuples_that_are_not_in_canonical_order(self) -> None:
        """境界側でもseat orderを検証し、暗黙のindex一致へ依存しない。

        `SeatObservation`自身がconstructorでseat orderを検証するため、
        検証済みvalueへ`object.__setattr__`で不正orderを書き込み、Arena境界の
        独立したfail closedを固定する。
        """
        for field_name, reordered in (
            (
                "scores",
                tuple(SeatScore(seat, 25_000) for seat in reversed(EngineSeat)),
            ),
            (
                "discards",
                tuple(SeatDiscards(seat, ()) for seat in reversed(EngineSeat)),
            ),
            ("melds", tuple(SeatMelds(seat, ()) for seat in reversed(EngineSeat))),
            (
                "riichi_states",
                tuple(
                    SeatRiichiState(seat, PublicRiichiStatus.NONE)
                    for seat in reversed(EngineSeat)
                ),
            ),
        ):
            with self.subTest(field_name=field_name):
                corrupted = observation()
                object.__setattr__(corrupted, field_name, reordered)
                with self.assertRaises(ObservationProjectionError):
                    build_policy_input(corrupted)

    def test_returns_a_policy_input(self) -> None:
        self.assertIsInstance(build_policy_input(observation()), PolicyInput)


if __name__ == "__main__":
    unittest.main()
