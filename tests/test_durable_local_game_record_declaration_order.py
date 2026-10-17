"""lisjong-arena #493: リーチ宣言牌の位置を持つ入力を、既存の記録形式へ黙って保存しない。

Arenaの記録形式（durable local game record schema version 2と、そのencoderを使う
source）はリーチ宣言牌の位置を持たない。値を持つ入力は保存前に拒否する。
"""

import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from test_durable_local_game_record import (
    _identities,
    _inspection,
    _policy_input,
    _provenance,
)

from lisjong_arena.durable_local_game_record import (
    DECISIONS_FILENAME,
    DurableLocalGameRecordError,
    _parse_policy_input,
    _policy_input_to_value,
    load_local_game_record,
    save_local_game_record,
)

FIELD = "riichi_declaration_discard_order"
_DECLARATION_TILE = Tile(TileType(TileCategory.SOUZU, 9))

if FIELD in PlayerPublicState.__dataclass_fields__:
    _PlayerWithDeclaration = PlayerPublicState
else:
    # lisjongのpinがfield追加（lisjong #301）より前でも、値を持つ入力を作れるようにする。

    @dataclass(frozen=True, slots=True)
    class _PlayerWithDeclaration(PlayerPublicState):
        riichi_declaration_discard_order: int | None = None


def _with_declaration(policy_input, seat):
    """`seat`がorder 0の宣言牌でリーチしている入力。"""
    players = list(policy_input.players)
    player = players[seat]
    players[seat] = _PlayerWithDeclaration(
        score=player.score,
        discards=(Discard(_DECLARATION_TILE, True, 0, None),),
        melds=player.melds,
        riichi=RiichiState.ACCEPTED,
        riichi_declaration_discard_order=0,
    )
    return replace(policy_input, players=tuple(players))


class EncoderTest(unittest.TestCase):
    def test_rejects_a_declaration_order_on_any_seat(self) -> None:
        for self_seat in (Seat.SEAT_0, Seat.SEAT_2):
            for seat in range(4):
                with self.subTest(self_seat=self_seat, seat=seat):
                    policy_input = _with_declaration(_policy_input(self_seat), seat)
                    with self.assertRaises(DurableLocalGameRecordError) as raised:
                        _policy_input_to_value(policy_input)
                    self.assertIn(
                        f"policy_input.players[{seat}].{FIELD}", str(raised.exception)
                    )

    def test_input_without_declaration_orders_keeps_its_projection(self) -> None:
        policy_input = _policy_input(Seat.SEAT_1)

        value = _policy_input_to_value(policy_input)

        for player in value["players"]:
            self.assertEqual(set(player), {"discards", "melds", "riichi", "score"})
        self.assertEqual(_parse_policy_input(value, "policy_input"), policy_input)

    def test_accepted_riichi_without_a_declaration_order_is_still_stored(self) -> None:
        # 古い記録と同じ形（成立済みで位置なし）は、従来どおり保存できる。
        policy_input = _policy_input(Seat.SEAT_0)
        players = list(policy_input.players)
        players[1] = replace(
            players[1],
            discards=(Discard(_DECLARATION_TILE, True, 0, None),),
            riichi=RiichiState.ACCEPTED,
        )
        policy_input = replace(policy_input, players=tuple(players))

        value = _policy_input_to_value(policy_input)

        self.assertEqual(value["players"][1]["riichi"], "accepted")
        self.assertNotIn(FIELD, value["players"][1])


class ProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _inspection_with_declaration(self):
        inspection = _inspection()
        step = inspection.step_observations[0]
        decision = step.seat_decisions[1]
        decisions = list(step.seat_decisions)
        decisions[1] = replace(
            decision,
            policy_input=_with_declaration(decision.policy_input, 3),
        )
        steps = list(inspection.step_observations)
        steps[0] = replace(step, seat_decisions=tuple(decisions))
        return replace(inspection, step_observations=tuple(steps))

    def test_save_propagates_the_rejection_and_writes_nothing(self) -> None:
        path = self.root / "record"

        with self.assertRaises(DurableLocalGameRecordError) as raised:
            save_local_game_record(
                self._inspection_with_declaration(),
                path,
                policy_identities=_identities(),
                max_steps=10,
                provenance=_provenance(),
            )

        self.assertIn(f"policy_input.players[3].{FIELD}", str(raised.exception))
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_save_and_load_without_declaration_orders_are_unchanged(self) -> None:
        path = self.root / "record"
        save_local_game_record(
            _inspection(),
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
        )

        record = load_local_game_record(path)
        decisions = json.loads((path / DECISIONS_FILENAME).read_text("utf-8"))

        for step in decisions["steps"]:
            for decision in step["seat_decisions"]:
                for player in decision["policy_input"]["players"]:
                    self.assertNotIn(FIELD, player)
        self.assertEqual(record.inspection, _inspection())


if __name__ == "__main__":
    unittest.main()
