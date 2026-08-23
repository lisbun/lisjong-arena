import json
import unittest

from lisjong.policy_contract.meld import MeldKind
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind

from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import SeatMaterializedState
from lisjong_arena.riichienv.adapter.policy_input import build_policy_input

MANZU_1 = Tile(TileType(TileCategory.MANZU, 1))
PINZU_1 = Tile(TileType(TileCategory.PINZU, 1))


class _FakeMeldType:
    """`str(riichienv Meld.meld_type)`が`"MeldType.Pon"`等になる形を再現する。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return f"MeldType.{self._name}"


class _FakeMeld:
    def __init__(
        self, meld_type_name: str, tiles: list[int], called_tile, from_who: int
    ):
        self.meld_type = _FakeMeldType(meld_type_name)
        self.tiles = tiles
        self.called_tile = called_tile
        self.from_who = from_who


class _FakeObservation:
    """build_policy_inputが実際に読む属性だけを提供するObservation double。

    `hands`、`tsumogiri_flags`、`last_tedashis`、`last_discard`、`waits`、
    `is_tenpai`は意図的に持たない。build_policy_inputがこれらへ触れると
    AttributeErrorになり、他家非公開情報を読んでいないことを構造的に保証する。
    """

    def __init__(
        self,
        player_id: int,
        events: list[dict],
        *,
        hand: list[int],
        drawn_tile,
        melds=None,
        discards=None,
        scores=(25000, 25000, 25000, 25000),
        riichi_declared=(False, False, False, False),
        riichi_sticks=0,
        honba=0,
        round_wind=0,
        oya=0,
        kyoku_index=0,
        dora_indicators=(18,),
    ):
        self.player_id = player_id
        self._events = events
        self.hand = hand
        self.drawn_tile = drawn_tile
        self.melds = melds if melds is not None else [[], [], [], []]
        self.discards = discards if discards is not None else [[], [], [], []]
        self.scores = list(scores)
        self.riichi_declared = list(riichi_declared)
        self.riichi_sticks = riichi_sticks
        self.honba = honba
        self.round_wind = round_wind
        self.oya = oya
        self.kyoku_index = kyoku_index
        self.dora_indicators = list(dora_indicators)

    def new_events(self) -> list[str]:
        return [json.dumps(event) for event in self._events]


_START_KYOKU = {
    "type": "start_kyoku",
    "bakaze": "E",
    "kyoku": 1,
    "honba": 0,
    "oya": 0,
    "dora_marker": "1p",
}


def _initial_observation(**overrides) -> _FakeObservation:
    kwargs = dict(
        player_id=0,
        events=[_START_KYOKU, {"type": "tsumo", "actor": 0, "pai": "1m"}],
        hand=[0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48],
        drawn_tile=0,
    )
    kwargs.update(overrides)
    return _FakeObservation(**kwargs)


class SnapshotProjectionTest(unittest.TestCase):
    def test_builds_all_required_round_fields(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        policy_input = build_policy_input(tracker, _initial_observation())

        self.assertIsInstance(policy_input, PolicyInput)
        self.assertEqual(policy_input.self_seat, Seat.SEAT_0)
        self.assertEqual(policy_input.round.round_wind, Wind.EAST)
        self.assertEqual(policy_input.round.hand_number, 1)
        self.assertEqual(policy_input.round.dealer_seat, Seat.SEAT_0)
        self.assertEqual(policy_input.round.honba, 0)
        self.assertEqual(policy_input.round.riichi_sticks, 0)
        self.assertEqual(policy_input.round.dora_indicators, (PINZU_1,))
        # 84 - 1回のtsumo event = 83
        self.assertEqual(policy_input.round.live_wall_tiles_remaining, 83)

    def test_builds_own_hand_from_physical_ids(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        policy_input = build_policy_input(tracker, _initial_observation())

        self.assertIn(MANZU_1, policy_input.own_hand.concealed_tiles)
        self.assertEqual(policy_input.own_hand.drawn_tile, MANZU_1)
        self.assertEqual(len(policy_input.own_hand.concealed_tiles), 13)

    def test_normalizes_drawn_tile_not_in_hand_to_none_when_kakan_just_occurred(
        self,
    ) -> None:
        # 槍槓のron応答機会の実測(RiichiEnv 0.4.8): 直前にkakan eventが観測
        # されている場合に限り、drawn_tileが自席の手牌にない値になることがある。
        # その場合はNoneへ正規化する。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[
                _START_KYOKU,
                {"type": "tsumo", "actor": 0, "pai": "1m"},
                {"type": "kakan", "actor": 1, "pai": "1p", "consumed": []},
            ],
            hand=[0, 4, 8],
            # physical id 36は"1p"であり、直前kakanのpai("1p")と一致する。
            drawn_tile=36,
        )
        policy_input = build_policy_input(tracker, observation)
        self.assertIsNone(policy_input.own_hand.drawn_tile)

    def test_rejects_drawn_tile_not_matching_the_preceding_kakan_tile(self) -> None:
        # actorだけでなく、drawn_tileのsemantic valueが直前kakanの加槓牌と
        # 一致することまで確認する。牌種が異なる場合はfail closedする。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[
                _START_KYOKU,
                {"type": "tsumo", "actor": 0, "pai": "1m"},
                {"type": "kakan", "actor": 1, "pai": "1p", "consumed": []},
            ],
            hand=[0, 4, 8],
            # physical id 44は"3p"であり、直前kakanのpai("1p")と一致しない。
            drawn_tile=44,
        )
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_builds_melds_directly_from_observation_melds(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_1)
        observation = _initial_observation(
            player_id=1,
            melds=[
                [],
                [_FakeMeld("Pon", [40, 41, 43], 43, 0)],
                [],
                [],
            ],
        )
        policy_input = build_policy_input(tracker, observation)
        meld = policy_input.players[1].melds[0]
        self.assertEqual(meld.kind, MeldKind.PON)
        self.assertEqual(meld.from_seat, Seat.SEAT_0)

    def test_builds_kakan_meld_keeping_original_pon_from_seat(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            melds=[
                [_FakeMeld("Kakan", [36, 37, 38, 39], 38, 1)],
                [],
                [],
                [],
            ]
        )
        policy_input = build_policy_input(tracker, observation)
        meld = policy_input.players[0].melds[0]
        self.assertEqual(meld.kind, MeldKind.KAKAN)
        self.assertEqual(meld.from_seat, Seat.SEAT_1)
        self.assertEqual(len(meld.tiles), 4)

    def test_builds_ankan_meld_with_none_from_seat_and_called_tile(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            melds=[
                [_FakeMeld("Ankan", [116, 117, 118, 119], None, -1)],
                [],
                [],
                [],
            ]
        )
        policy_input = build_policy_input(tracker, observation)
        meld = policy_input.players[0].melds[0]
        self.assertEqual(meld.kind, MeldKind.ANKAN)
        self.assertIsNone(meld.from_seat)
        self.assertIsNone(meld.called_tile)

    def test_discards_come_from_materialized_state_with_called_by(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[
                _START_KYOKU,
                {"type": "tsumo", "actor": 0, "pai": "1m"},
                {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True},
                {
                    "type": "chi",
                    "actor": 1,
                    "target": 0,
                    "pai": "1m",
                    "consumed": ["2m", "3m"],
                },
            ],
            hand=[4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48],
            drawn_tile=None,
            discards=[[0], [], [], []],
        )
        policy_input = build_policy_input(tracker, observation)
        discard = policy_input.players[0].discards[0]
        self.assertEqual(discard.tile, MANZU_1)
        self.assertTrue(discard.tsumogiri)
        self.assertEqual(discard.called_by, Seat.SEAT_1)


class FailClosedSyncTest(unittest.TestCase):
    def test_rejects_observation_player_id_mismatch_with_tracker(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(player_id=1)
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_when_kyoku_identity_disagrees_with_observation(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        # eventのoya(0)とObservation.oya(1)が食い違う。
        observation = _initial_observation(oya=1)
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_dora_indicator_count_mismatch(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(dora_indicators=(18, 40))
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_discard_multiset_mismatch(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[
                _START_KYOKU,
                {"type": "tsumo", "actor": 0, "pai": "1m"},
                {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True},
            ],
            hand=[4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48],
            drawn_tile=None,
            # Observation側は2p(id 40)を捨てたことになっているが、materialized
            # stateは1mを記録しており一致しない。
            discards=[[40], [], [], []],
        )
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_riichi_declared_true_while_materialized_is_none(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(riichi_declared=(True, False, False, False))
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_allows_riichi_declared_true_while_materialized_is_declared(self) -> None:
        # RiichiEnv 0.4.8実測: reach宣言牌がchi/pon可能な場合、reach_accepted
        # eventがこのseatへ届く1 Observation前にriichi_declaredがTrueへ切り替わる
        # ことがある。DECLAREDとの組み合わせはfail closedしない。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[_START_KYOKU, {"type": "reach", "actor": 0}],
            riichi_declared=(True, False, False, False),
        )
        policy_input = build_policy_input(tracker, observation)
        self.assertEqual(policy_input.players[0].riichi, RiichiState.DECLARED)

    def test_rejects_riichi_declared_false_while_materialized_is_accepted(
        self,
    ) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            events=[
                _START_KYOKU,
                {"type": "reach", "actor": 0},
                {"type": "reach_accepted", "actor": 0},
            ],
            riichi_declared=(False, False, False, False),
        )
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_unrecognized_riichienv_meld_type(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(
            melds=[[_FakeMeld("Nuki", [120], None, -1)], [], [], []]
        )
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)

    def test_rejects_drawn_tile_not_in_hand_without_a_preceding_kakan(self) -> None:
        # 「handにないdrawn_tile」というだけでは槍槓と断定しない。直前に
        # kakan eventが観測されていない場合はfail closedする。#28レビュー
        # (drawn_tile正規化を槍槓以外へ一般化しない)に対応する回帰test。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation(hand=[0, 4, 8], drawn_tile=44)
        with self.assertRaises(AdapterSyncError):
            build_policy_input(tracker, observation)


class InformationBoundaryTest(unittest.TestCase):
    def test_only_reads_the_attributes_it_needs(self) -> None:
        # _FakeObservationは他家非公開情報になり得るhands等のattributeを
        # 意図的に持たない。build_policy_inputがそれらへ触れればAttributeErrorに
        # なるため、このtestが通ること自体が非依存を保証する。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        build_policy_input(tracker, _initial_observation())

    def test_snapshot_is_independent_of_later_mutation_of_source_lists(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _initial_observation()
        policy_input = build_policy_input(tracker, observation)

        original_concealed_tiles = policy_input.own_hand.concealed_tiles
        observation.hand.clear()
        observation.hand.extend([999])

        self.assertEqual(
            policy_input.own_hand.concealed_tiles, original_concealed_tiles
        )

    def test_cross_seat_trackers_stay_independent(self) -> None:
        tracker_0 = SeatMaterializedState(Seat.SEAT_0)
        tracker_1 = SeatMaterializedState(Seat.SEAT_1)

        build_policy_input(tracker_0, _initial_observation())
        build_policy_input(
            tracker_1,
            _initial_observation(
                player_id=1,
                hand=[1, 5, 9, 13, 17, 21, 25, 29, 33, 37, 41, 45, 49],
                drawn_tile=1,
            ),
        )

        self.assertEqual(len(tracker_0.discards[1]), 0)
        self.assertEqual(len(tracker_1.discards[0]), 0)


if __name__ == "__main__":
    unittest.main()
