"""`lisjong_arena.riichilab.mjai_response`のprotocol-facing correctness(Arena-owned、Issue #27)。

lisjong Issue #38で確立したcontractをbehavior-preservingにArenaへcanonical
physical migrationしたものである。
"""

import unittest

from lisjong.policy_contract import (
    ChiAction,
    DiscardAction,
    KakanAction,
    PonAction,
    RonAction,
    Seat,
    Tile,
    TileCategory,
    TileType,
    TsumoAction,
)
from riichienv import Action as RiichiEnvAction
from riichienv import ActionType

from lisjong_arena.riichilab.mjai_response import build_mjai_response

# id割り当ては tests/test_riichilab_session.py 等の既存Arena testと同じ実測規則。
_MANZU_3_ID = 8
_MANZU_5_RED_ID = 16
_MANZU_2_ID = 4
_MANZU_4_ID = 12
_PINZU_2_ID = 40

MANZU_3 = Tile(TileType(TileCategory.MANZU, 3))
MANZU_5_RED = Tile(TileType(TileCategory.MANZU, 5), is_red=True)
MANZU_2 = Tile(TileType(TileCategory.MANZU, 2))
MANZU_4 = Tile(TileType(TileCategory.MANZU, 4))
PINZU_2 = Tile(TileType(TileCategory.PINZU, 2))


def _external_action(action_type: ActionType, actor: int, tile=None, consume=()):
    action = RiichiEnvAction(
        action_type, tile if tile is not None else 0, list(consume)
    )
    action.actor = actor
    return action


class BuildMjaiResponseTest(unittest.TestCase):
    def test_discard_response_includes_tsumogiri_from_selected_action(self) -> None:
        external = _external_action(ActionType.DISCARD, 0, tile=_MANZU_3_ID)
        selected = DiscardAction(actor=Seat.SEAT_0, tile=MANZU_3, tsumogiri=True)

        response = build_mjai_response(external, selected)

        self.assertEqual(response["type"], "dahai")
        self.assertEqual(response["actor"], 0)
        self.assertEqual(response["pai"], "3m")
        self.assertIs(response["tsumogiri"], True)

    def test_chi_response_includes_target_missing_from_to_mjai(self) -> None:
        external = _external_action(
            ActionType.CHI, 1, tile=_MANZU_3_ID, consume=(_MANZU_2_ID, _MANZU_4_ID)
        )
        selected = ChiAction(
            actor=Seat.SEAT_1,
            target=Seat.SEAT_0,
            called_tile=MANZU_3,
            consumed_tiles=(MANZU_2, MANZU_4),
        )

        response = build_mjai_response(external, selected)

        self.assertEqual(response["type"], "chi")
        self.assertEqual(response["target"], 0)
        self.assertEqual(response["actor"], 1)

    def test_pon_response_includes_target(self) -> None:
        external = _external_action(
            ActionType.PON, 2, tile=_PINZU_2_ID, consume=(_PINZU_2_ID, _PINZU_2_ID)
        )
        selected = PonAction(
            actor=Seat.SEAT_2,
            target=Seat.SEAT_1,
            called_tile=PINZU_2,
            consumed_tiles=(PINZU_2, PINZU_2),
        )

        response = build_mjai_response(external, selected)

        self.assertEqual(response["target"], 1)

    def test_kakan_response_keeps_native_pai_from_to_mjai(self) -> None:
        external = _external_action(ActionType.KAKAN, 1, tile=_PINZU_2_ID)
        selected = KakanAction(
            actor=Seat.SEAT_1,
            added_tile=PINZU_2,
            from_seat=Seat.SEAT_0,
            called_tile=PINZU_2,
        )

        response = build_mjai_response(external, selected)

        self.assertEqual(response["type"], "kakan")
        self.assertEqual(response["pai"], "2p")

    def test_ron_response_adds_pai_and_target_missing_from_to_mjai(self) -> None:
        external = _external_action(ActionType.RON, 0)
        selected = RonAction(
            actor=Seat.SEAT_0, target=Seat.SEAT_1, winning_tile=MANZU_5_RED
        )

        response = build_mjai_response(external, selected)

        self.assertEqual(response["type"], "hora")
        self.assertEqual(response["actor"], 0)
        self.assertEqual(response["target"], 1)
        self.assertEqual(response["pai"], "5mr")

    def test_tsumo_response_targets_the_actor_itself(self) -> None:
        external = _external_action(ActionType.TSUMO, 3)
        selected = TsumoAction(actor=Seat.SEAT_3, winning_tile=MANZU_5_RED)

        response = build_mjai_response(external, selected)

        self.assertEqual(response["type"], "hora")
        self.assertEqual(response["target"], 3)
        self.assertEqual(response["pai"], "5mr")


if __name__ == "__main__":
    unittest.main()
