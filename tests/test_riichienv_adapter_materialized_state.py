import json
import unittest

from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType

from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import SeatMaterializedState

MANZU_1 = Tile(TileType(TileCategory.MANZU, 1))
MANZU_2 = Tile(TileType(TileCategory.MANZU, 2))
PINZU_1 = Tile(TileType(TileCategory.PINZU, 1))
PINZU_3 = Tile(TileType(TileCategory.PINZU, 3))


class _FakeObservation:
    """new_events()だけを提供する最小限のObservation double。"""

    def __init__(self, player_id: int, events: list[dict]) -> None:
        self.player_id = player_id
        self._events = events

    def new_events(self) -> list[str]:
        return [json.dumps(event) for event in self._events]


_START_KYOKU_EAST_1 = {
    "type": "start_kyoku",
    "bakaze": "E",
    "kyoku": 1,
    "honba": 0,
    "oya": 0,
    "dora_marker": "1p",
}


def _tracker_after_start_kyoku(
    self_seat: Seat = Seat.SEAT_0, event: dict = _START_KYOKU_EAST_1
) -> SeatMaterializedState:
    tracker = SeatMaterializedState(self_seat)
    tracker.apply_observation(_FakeObservation(int(self_seat), [event]))
    return tracker


class SeatMaterializedStateConstructionTest(unittest.TestCase):
    def test_rejects_non_seat(self) -> None:
        with self.assertRaises(TypeError):
            SeatMaterializedState(0)

    def test_starts_with_no_kyoku_identity(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        self.assertIsNone(tracker.kyoku_identity)


class StartKyokuResetTest(unittest.TestCase):
    def test_sets_kyoku_identity_and_seeds_dora_indicator(self) -> None:
        tracker = _tracker_after_start_kyoku()
        identity = tracker.kyoku_identity
        self.assertEqual(identity.hand_number, 1)
        self.assertEqual(identity.honba, 0)
        self.assertEqual(identity.dealer_seat, Seat.SEAT_0)
        self.assertEqual(
            tracker.dora_indicators, (Tile(TileType(TileCategory.PINZU, 1)),)
        )
        self.assertEqual(tracker.tsumo_count, 0)
        self.assertEqual(tracker.discards, ((), (), (), ()))
        self.assertEqual(tracker.riichi_state, (RiichiState.NONE,) * 4)

    def test_resets_prior_kyoku_state(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {"type": "tsumo", "actor": 0, "pai": "1m"},
                    {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True},
                    {"type": "reach", "actor": 1},
                ],
            )
        )
        self.assertEqual(tracker.tsumo_count, 1)
        self.assertEqual(len(tracker.discards[0]), 1)
        self.assertEqual(tracker.riichi_state[1], RiichiState.DECLARED)

        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {
                        "type": "start_kyoku",
                        "bakaze": "S",
                        "kyoku": 2,
                        "honba": 1,
                        "oya": 1,
                        "dora_marker": "E",
                    }
                ],
            )
        )
        self.assertEqual(tracker.tsumo_count, 0)
        self.assertEqual(tracker.discards, ((), (), (), ()))
        self.assertEqual(tracker.riichi_state, (RiichiState.NONE,) * 4)
        self.assertEqual(
            tracker.dora_indicators, (Tile(TileType(TileCategory.HONOR, 1)),)
        )
        identity = tracker.kyoku_identity
        self.assertEqual(identity.hand_number, 2)
        self.assertEqual(identity.honba, 1)
        self.assertEqual(identity.dealer_seat, Seat.SEAT_1)


class TsumoAndDahaiTest(unittest.TestCase):
    def test_tsumo_counts_even_when_pai_is_masked(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(0, [{"type": "tsumo", "actor": 1, "pai": "?"}])
        )
        self.assertEqual(tracker.tsumo_count, 1)

    def test_dahai_assigns_monotonic_order_across_all_seats(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": False},
                    {"type": "dahai", "actor": 1, "pai": "3p", "tsumogiri": True},
                ],
            )
        )
        self.assertEqual(tracker.discards[0][0].order, 0)
        self.assertEqual(tracker.discards[0][0].tile, MANZU_1)
        self.assertFalse(tracker.discards[0][0].tsumogiri)
        self.assertEqual(tracker.discards[1][0].order, 1)
        self.assertEqual(tracker.discards[1][0].tile, PINZU_3)
        self.assertTrue(tracker.discards[1][0].tsumogiri)

    def test_dahai_leaves_called_by_none(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0, [{"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": False}]
            )
        )
        self.assertIsNone(tracker.discards[0][0].called_by)


class CallResolutionTest(unittest.TestCase):
    def test_chi_sets_called_by_on_targets_most_recent_discard(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {"type": "dahai", "actor": 0, "pai": "3m", "tsumogiri": False},
                    {
                        "type": "chi",
                        "actor": 1,
                        "target": 0,
                        "pai": "3m",
                        "consumed": ["2m", "4m"],
                    },
                ],
            )
        )
        self.assertEqual(tracker.discards[0][0].called_by, Seat.SEAT_1)

    def test_pon_and_daiminkan_also_resolve_called_by(self) -> None:
        for event_type in ("pon", "daiminkan"):
            with self.subTest(event_type=event_type):
                tracker = _tracker_after_start_kyoku()
                tracker.apply_observation(
                    _FakeObservation(
                        0,
                        [
                            {
                                "type": "dahai",
                                "actor": 2,
                                "pai": "2m",
                                "tsumogiri": False,
                            },
                            {
                                "type": event_type,
                                "actor": 3,
                                "target": 2,
                                "pai": "2m",
                                "consumed": ["2m", "2m"],
                            },
                        ],
                    )
                )
                self.assertEqual(tracker.discards[2][0].called_by, Seat.SEAT_3)

    def test_rejects_call_when_target_has_no_discards(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(
                    0,
                    [
                        {
                            "type": "chi",
                            "actor": 1,
                            "target": 0,
                            "pai": "3m",
                            "consumed": ["2m", "4m"],
                        }
                    ],
                )
            )

    def test_rejects_call_when_most_recent_discard_tile_differs(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(
                    0,
                    [
                        {
                            "type": "dahai",
                            "actor": 0,
                            "pai": "1m",
                            "tsumogiri": False,
                        },
                        {
                            "type": "chi",
                            "actor": 1,
                            "target": 0,
                            "pai": "3m",
                            "consumed": ["2m", "4m"],
                        },
                    ],
                )
            )

    def test_rejects_call_on_an_already_called_discard(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(
                    0,
                    [
                        {
                            "type": "dahai",
                            "actor": 0,
                            "pai": "3m",
                            "tsumogiri": False,
                        },
                        {
                            "type": "chi",
                            "actor": 1,
                            "target": 0,
                            "pai": "3m",
                            "consumed": ["2m", "4m"],
                        },
                        {
                            "type": "pon",
                            "actor": 2,
                            "target": 0,
                            "pai": "3m",
                            "consumed": ["3m", "3m"],
                        },
                    ],
                )
            )


class RiichiTransitionTest(unittest.TestCase):
    def test_reach_then_reach_accepted(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(_FakeObservation(0, [{"type": "reach", "actor": 2}]))
        self.assertEqual(tracker.riichi_state[2], RiichiState.DECLARED)

        tracker.apply_observation(
            _FakeObservation(0, [{"type": "reach_accepted", "actor": 2}])
        )
        self.assertEqual(tracker.riichi_state[2], RiichiState.ACCEPTED)

    def test_rejects_reach_when_already_declared(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(_FakeObservation(0, [{"type": "reach", "actor": 2}]))
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(0, [{"type": "reach", "actor": 2}])
            )

    def test_rejects_reach_accepted_without_preceding_reach(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(0, [{"type": "reach_accepted", "actor": 2}])
            )


class DoraEventTest(unittest.TestCase):
    def test_dora_event_appends_indicator_in_order(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(0, [{"type": "dora", "dora_marker": "E"}])
        )
        self.assertEqual(
            tracker.dora_indicators,
            (
                Tile(TileType(TileCategory.PINZU, 1)),
                Tile(TileType(TileCategory.HONOR, 1)),
            ),
        )


class NoOpEventTest(unittest.TestCase):
    def test_ankan_kakan_and_end_of_kyoku_events_do_not_raise(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {"type": "ankan", "actor": 0, "pai": "E", "consumed": []},
                    {"type": "kakan", "actor": 0, "pai": "1p", "consumed": []},
                    {"type": "hora", "actor": 0},
                    {"type": "ryukyoku"},
                    {"type": "end_kyoku"},
                    {"type": "end_game"},
                ],
            )
        )
        self.assertEqual(tracker.tsumo_count, 0)


class PendingChankanStateTest(unittest.TestCase):
    def test_none_before_any_kakan(self) -> None:
        tracker = _tracker_after_start_kyoku()
        self.assertIsNone(tracker.pending_chankan_actor)
        self.assertIsNone(tracker.pending_chankan_tile)

    def test_set_to_kakan_actor_and_tile_immediately_after_kakan_event(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0, [{"type": "kakan", "actor": 2, "pai": "1p", "consumed": []}]
            )
        )
        self.assertEqual(tracker.pending_chankan_actor, Seat.SEAT_2)
        self.assertEqual(tracker.pending_chankan_tile, PINZU_1)

    def test_cleared_by_any_subsequent_event(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {"type": "kakan", "actor": 2, "pai": "1p", "consumed": []},
                    {"type": "tsumo", "actor": 1, "pai": "?"},
                ],
            )
        )
        self.assertIsNone(tracker.pending_chankan_actor)
        self.assertIsNone(tracker.pending_chankan_tile)

    def test_cleared_by_start_kyoku(self) -> None:
        tracker = _tracker_after_start_kyoku()
        tracker.apply_observation(
            _FakeObservation(
                0, [{"type": "kakan", "actor": 2, "pai": "1p", "consumed": []}]
            )
        )
        tracker.apply_observation(
            _FakeObservation(
                0,
                [
                    {
                        "type": "start_kyoku",
                        "bakaze": "E",
                        "kyoku": 2,
                        "honba": 0,
                        "oya": 1,
                        "dora_marker": "2p",
                    }
                ],
            )
        )
        self.assertIsNone(tracker.pending_chankan_actor)
        self.assertIsNone(tracker.pending_chankan_tile)


class FailClosedTest(unittest.TestCase):
    def test_rejects_event_before_any_start_kyoku(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(
                    0, [{"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True}]
                )
            )

    def test_rejects_unrecognized_event_type(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(
                _FakeObservation(0, [{"type": "totally_unknown_event"}])
            )

    def test_rejects_malformed_event_missing_required_field(self) -> None:
        tracker = _tracker_after_start_kyoku()
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(_FakeObservation(0, [{"type": "dahai"}]))

    def test_rejects_start_kyoku_with_unrecognized_bakaze(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        bad_event = dict(_START_KYOKU_EAST_1, bakaze="X")
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(_FakeObservation(0, [bad_event]))

    def test_rejects_observation_player_id_mismatch(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(_FakeObservation(1, [_START_KYOKU_EAST_1]))

    def test_rejects_reapplying_the_same_observation_instance(self) -> None:
        tracker = SeatMaterializedState(Seat.SEAT_0)
        observation = _FakeObservation(0, [_START_KYOKU_EAST_1])
        tracker.apply_observation(observation)
        with self.assertRaises(AdapterSyncError):
            tracker.apply_observation(observation)

    def test_allows_a_different_observation_instance_with_equivalent_content(
        self,
    ) -> None:
        # 二重適用防止はinstance単位であり、同内容の別instanceまでは拒否しない
        # (RiichiEnvのObservationは1 decisionにつき新しいinstanceを返す)。
        tracker = SeatMaterializedState(Seat.SEAT_0)
        tracker.apply_observation(_FakeObservation(0, [_START_KYOKU_EAST_1]))
        tracker.apply_observation(
            _FakeObservation(0, [{"type": "tsumo", "actor": 0, "pai": "1m"}])
        )
        self.assertEqual(tracker.tsumo_count, 1)


class SeatIsolationTest(unittest.TestCase):
    def test_trackers_for_different_seats_do_not_share_mutable_state(self) -> None:
        tracker_0 = _tracker_after_start_kyoku(Seat.SEAT_0)
        tracker_1 = _tracker_after_start_kyoku(Seat.SEAT_1, _START_KYOKU_EAST_1)

        tracker_0.apply_observation(
            _FakeObservation(
                0, [{"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": False}]
            )
        )

        self.assertEqual(len(tracker_0.discards[0]), 1)
        self.assertEqual(tracker_1.discards, ((), (), (), ()))


if __name__ == "__main__":
    unittest.main()
