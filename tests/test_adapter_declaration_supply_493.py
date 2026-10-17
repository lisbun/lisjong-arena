"""Public declaration acquisition and fail-closed boundaries for #493."""

import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from _lisjong_engine_fixtures import (
    discard,
    manzu,
    observation,
    seat_discards,
    seat_melds,
)
from lisjong.policy_contract import RiichiState, Seat
from lisjong_engine.public_state import (
    PublicMeld,
    PublicMeldType,
    PublicRiichiStatus,
    SeatRiichiState,
)
from lisjong_engine.seat import Seat as EngineSeat
from test_riichienv_adapter_policy_input import _FakeMeld, _initial_observation

from lisjong_arena.learned_policy_input.feature import build_policy_input_feature
from lisjong_arena.learned_policy_input.tensor import tensor_values
from lisjong_arena.lisjong_engine.errors import ObservationProjectionError
from lisjong_arena.lisjong_engine.policy_input import build_policy_input as engine_input
from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import SeatMaterializedState
from lisjong_arena.riichienv.adapter.policy_input import build_policy_input as env_input


def states(**statuses):
    return tuple(
        SeatRiichiState(s, statuses.get(s.value, PublicRiichiStatus.NONE))
        for s in EngineSeat
    )


def dahai(actor, pai="1m", *, tsumogiri=False):
    return {"type": "dahai", "actor": actor, "pai": pai, "tsumogiri": tsumogiri}


class RiichiEnvDeclarationTest(unittest.TestCase):
    def test_declared_before_and_after_discard_and_acceptance(self):
        for tsumogiri in (False, True):
            with self.subTest(tsumogiri=tsumogiri):
                tracker = SeatMaterializedState(Seat.SEAT_0)
                first = _initial_observation()
                first._events.append({"type": "reach", "actor": 1})
                before = env_input(tracker, first)
                self.assertIs(before.players[1].riichi, RiichiState.DECLARED)
                self.assertIsNone(before.players[1].riichi_declaration_discard_order)
                after = env_input(
                    tracker,
                    _initial_observation(
                        events=[dahai(1, tsumogiri=tsumogiri)],
                        discards=[[], [0], [], []],
                    ),
                )
                self.assertIs(after.players[1].riichi, RiichiState.DECLARED)
                self.assertEqual(after.players[1].riichi_declaration_discard_order, 0)
                accepted = env_input(
                    tracker,
                    _initial_observation(
                        events=[{"type": "reach_accepted", "actor": 1}],
                        discards=[[], [0], [], []],
                        riichi_declared=(False, True, False, False),
                    ),
                )
                self.assertEqual(
                    accepted.players[1].riichi_declaration_discard_order, 0
                )

    def test_called_declaration_multiple_seats_ankan_and_round_reset(self):
        tracker = SeatMaterializedState(Seat.SEAT_0)
        env_input(tracker, _initial_observation())
        view = env_input(
            tracker,
            _initial_observation(
                events=[
                    dahai(0, "2m"),
                    {"type": "reach", "actor": 1},
                    dahai(1),
                    {
                        "type": "pon",
                        "actor": 2,
                        "target": 1,
                        "pai": "1m",
                        "consumed": ["1m", "1m"],
                    },
                    {"type": "reach_accepted", "actor": 1},
                    dahai(2, "3m"),
                    {"type": "reach", "actor": 3},
                    dahai(3, "4m", tsumogiri=True),
                    {"type": "reach_accepted", "actor": 3},
                ],
                discards=[[4], [0], [8], [12]],
                riichi_declared=(False, True, False, True),
            ),
        )
        self.assertEqual(
            tuple(p.riichi_declaration_discard_order for p in view.players),
            (None, 1, None, 3),
        )
        self.assertIs(view.players[1].discards[0].called_by, Seat.SEAT_2)
        after = env_input(
            tracker,
            _initial_observation(
                events=[
                    {"type": "ankan", "actor": 1, "consumed": ["5p"] * 4},
                    dahai(1, "6m", tsumogiri=True),
                ],
                discards=[[4], [0, 20], [8], [12]],
                melds=[[], [_FakeMeld("Ankan", [52, 53, 54, 55], None, -1)], [], []],
                riichi_declared=(False, True, False, True),
            ),
        )
        self.assertEqual(after.players[1].riichi_declaration_discard_order, 1)
        reset = env_input(tracker, _initial_observation())
        self.assertEqual(
            tuple(p.riichi_declaration_discard_order for p in reset.players),
            (None,) * 4,
        )

    def test_rejects_acceptance_without_declaration_discard(self):
        tracker = SeatMaterializedState(Seat.SEAT_0)
        value = _initial_observation()
        value._events.extend(
            [{"type": "reach", "actor": 1}, {"type": "reach_accepted", "actor": 1}]
        )
        with self.assertRaisesRegex(AdapterSyncError, "declaration discard order"):
            env_input(tracker, value)

    def test_rejects_non_tsumogiri_after_declaration_before_or_after_acceptance(self):
        for accepted in (False, True):
            tracker = SeatMaterializedState(Seat.SEAT_0)
            value = _initial_observation()
            value._events.extend([{"type": "reach", "actor": 1}, dahai(1)])
            if accepted:
                value._events.append({"type": "reach_accepted", "actor": 1})
            value._events.append(dahai(1, "2m"))
            with self.assertRaisesRegex(AdapterSyncError, "post-riichi"):
                env_input(tracker, value)

    def test_hidden_hands_and_wall_do_not_change_projection(self):
        values = []
        for hidden in ("one", "another"):
            obs = _initial_observation()
            obs._events.extend([{"type": "reach", "actor": 1}, dahai(1)])
            obs.discards[1] = [0]
            obs.hands = hidden
            obs.wall = hidden
            values.append(env_input(SeatMaterializedState(Seat.SEAT_0), obs))
        self.assertEqual(*values)


class EngineDeclarationTest(unittest.TestCase):
    def test_pending_before_discard_and_declared_or_established_afterwards(self):
        before = engine_input(
            observation(riichi_states=states(east=PublicRiichiStatus.PENDING))
        )
        self.assertIsNone(before.players[0].riichi_declaration_discard_order)
        for tsumogiri in (False, True):
            for status in (PublicRiichiStatus.PENDING, PublicRiichiStatus.ESTABLISHED):
                with self.subTest(tsumogiri=tsumogiri, status=status):
                    view = engine_input(
                        observation(
                            discards=seat_discards(
                                east=(
                                    discard(
                                        manzu(1),
                                        0,
                                        is_tsumogiri=tsumogiri,
                                        is_riichi_declaration=True,
                                        called_by=EngineSeat.SOUTH,
                                    ),
                                )
                            ),
                            riichi_states=states(east=status),
                        )
                    )
                    self.assertEqual(
                        view.players[0].riichi_declaration_discard_order, 0
                    )
                    self.assertIs(view.players[0].discards[0].called_by, Seat.SEAT_1)

    def test_multiple_seats_keep_distinct_global_orders(self):
        view = engine_input(
            observation(
                discards=seat_discards(
                    east=(
                        discard(manzu(1), 0, is_riichi_declaration=True),
                        discard(manzu(2), 2, is_tsumogiri=True),
                    ),
                    south=(discard(manzu(3), 1, is_riichi_declaration=True),),
                ),
                riichi_states=states(
                    east=PublicRiichiStatus.ESTABLISHED,
                    south=PublicRiichiStatus.PENDING,
                ),
            )
        )
        self.assertEqual(
            tuple(p.riichi_declaration_discard_order for p in view.players),
            (0, 1, None, None),
        )
        self.assertIsNone(
            engine_input(observation()).players[0].riichi_declaration_discard_order
        )

    def test_rejects_missing_accepted_position_multiple_marks_and_later_hand_discard(
        self,
    ):
        bad = [
            (),
            (
                discard(manzu(1), 0, is_riichi_declaration=True),
                discard(manzu(2), 1, is_riichi_declaration=True),
            ),
            (discard(manzu(1), 0, is_riichi_declaration=True), discard(manzu(2), 1)),
        ]
        for discards in bad:
            with (
                self.subTest(discards=discards),
                self.assertRaises(ObservationProjectionError),
            ):
                engine_input(
                    observation(
                        discards=seat_discards(east=discards),
                        riichi_states=states(east=PublicRiichiStatus.ESTABLISHED),
                    )
                )

    def test_cancelled_declaration_does_not_supply_a_position(self):
        view = engine_input(
            observation(
                discards=seat_discards(
                    east=(discard(manzu(1), 0, is_riichi_declaration=True),)
                )
            )
        )
        self.assertIsNone(view.players[0].riichi_declaration_discard_order)

    def test_position_survives_closed_kan_and_rinshan_discard(self):
        values = dict(
            discards=seat_discards(
                east=(discard(manzu(1), 0, is_riichi_declaration=True),)
            ),
            riichi_states=states(east=PublicRiichiStatus.ESTABLISHED),
        )
        before = engine_input(observation(**values))
        kan = PublicMeld(PublicMeldType.ANKAN, (manzu(2),) * 4, None, None)
        values["melds"] = seat_melds(east=(kan,))
        values["discards"] = seat_discards(
            east=(
                discard(manzu(1), 0, is_riichi_declaration=True),
                discard(manzu(3), 1, is_tsumogiri=True),
            )
        )
        after = engine_input(observation(**values))
        self.assertEqual(
            before.players[0].riichi_declaration_discard_order,
            after.players[0].riichi_declaration_discard_order,
        )
        self.assertEqual(after.players[0].riichi_declaration_discard_order, 0)

    def test_hidden_tiles_and_wall_do_not_change_real_engine_projection(self):
        from lisjong_engine.match_state import MatchState
        from lisjong_engine.observation_builder import build_seat_observation
        from lisjong_engine.tile import STANDARD_TILES
        from lisjong_engine.wall import Wall

        values = []
        for swapped in (False, True):
            deck = list(STANDARD_TILES)
            if swapped:
                # South's concealed tile and a later live-wall tile, never East's draw.
                deck[4], deck[70] = deck[70], deck[4]
            match = MatchState(seed=0)
            with patch(
                "lisjong_engine.match_state.create_round_wall",
                return_value=Wall(deck[:-14], deck[-14:]),
            ):
                state = match.start_round()
            state.draw(EngineSeat.EAST)
            values.append(engine_input(build_seat_observation(match, EngineSeat.EAST)))
        self.assertEqual(*values)

    def test_existing_feature_and_tensor_values_ignore_position(self):
        view = engine_input(
            observation(
                discards=seat_discards(
                    east=(discard(manzu(1), 0, is_riichi_declaration=True),)
                ),
                riichi_states=states(east=PublicRiichiStatus.ESTABLISHED),
            )
        )
        legacy = replace(
            view,
            players=tuple(
                replace(p, riichi_declaration_discard_order=None) for p in view.players
            ),
        )
        current_feature = build_policy_input_feature(view)
        old_feature = build_policy_input_feature(legacy)
        self.assertEqual(current_feature, old_feature)
        self.assertEqual(tensor_values(current_feature), tensor_values(old_feature))


class DiagnosticComparisonTest(unittest.TestCase):
    def test_singletons_match_and_disagreement_stops_the_audit(self):
        from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
            _RiichiDeclarationTracker,
        )

        spec = importlib.util.spec_from_file_location(
            "audit_493",
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "validate_declaration_positions_493.py",
        )
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        tracker = _RiichiDeclarationTracker()
        before = engine_input(observation(viewer_seat=EngineSeat.SOUTH))
        self.assertFalse(audit.compare_positions(before, tracker))
        after = engine_input(
            observation(
                viewer_seat=EngineSeat.SOUTH,
                discards=seat_discards(
                    east=(discard(manzu(1), 0, is_riichi_declaration=True),)
                ),
                riichi_states=states(east=PublicRiichiStatus.ESTABLISHED),
            )
        )
        self.assertEqual(
            audit.compare_positions(after, tracker)["singleton_player_rows_matched"], 1
        )
        ambiguous = engine_input(
            observation(
                viewer_seat=EngineSeat.SOUTH,
                discards=seat_discards(
                    east=(
                        discard(
                            manzu(1), 0, is_tsumogiri=True, is_riichi_declaration=True
                        ),
                        discard(manzu(2), 1, is_tsumogiri=True),
                    )
                ),
                riichi_states=states(east=PublicRiichiStatus.ESTABLISHED),
            )
        )
        result = audit.compare_positions(ambiguous, _RiichiDeclarationTracker())
        self.assertEqual(result, {"ambiguous_player_rows": 1})
        invalid = replace(
            after,
            players=tuple(
                replace(p, riichi_declaration_discard_order=None) for p in after.players
            ),
        )
        with self.assertRaisesRegex(ValueError, "differs"):
            audit.compare_positions(invalid, tracker)


if __name__ == "__main__":
    unittest.main()
