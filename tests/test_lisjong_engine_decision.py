"""engine decisionからlisjong `DecisionContext`と対応mappingを構築する境界。"""

import unittest

from _lisjong_engine_fixtures import manzu, observation, pinzu
from lisjong.policy_contract import DecisionContext, DiscardAction, RiichiAction, Seat
from lisjong_engine.action_descriptor import (
    DiscardActionDescriptor,
    PassActionDescriptor,
    RiichiActionDescriptor,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.action_mapping import build_action_mapping
from lisjong_arena.lisjong_engine.decision import EngineDecision, build_decision
from lisjong_arena.lisjong_engine.errors import (
    AmbiguousActionMappingError,
    SeatIdentityError,
)


class BuildDecisionTest(unittest.TestCase):
    def test_pairs_a_decision_context_with_its_mapping(self) -> None:
        options = (
            DiscardActionDescriptor(manzu(1), True),
            RiichiActionDescriptor(),
        )
        decision = build_decision(observation(), options)
        self.assertIsInstance(decision.context, DecisionContext)
        self.assertEqual(decision.context.legal_actions, decision.mapping.candidates)
        self.assertIs(decision.context.input.self_seat, Seat.SEAT_0)
        self.assertIs(decision.mapping.self_seat, Seat.SEAT_0)

    def test_all_three_seat_identities_agree(self) -> None:
        """observation viewer seat / mapping actor / legal action actorが一致する。"""
        source = observation(viewer_seat=EngineSeat.NORTH, drawn_tile=None)
        decision = build_decision(source, (DiscardActionDescriptor(manzu(1), False),))
        self.assertIs(decision.context.input.self_seat, Seat.SEAT_3)
        self.assertIs(decision.mapping.self_seat, Seat.SEAT_3)
        self.assertEqual(
            {action.actor for action in decision.context.legal_actions},
            {Seat.SEAT_3},
        )

    def test_legal_actions_keep_the_offered_order(self) -> None:
        options = (
            DiscardActionDescriptor(pinzu(2), False),
            DiscardActionDescriptor(manzu(1), True),
            RiichiActionDescriptor(),
        )
        decision = build_decision(observation(), options)
        self.assertEqual(
            tuple(type(action) for action in decision.context.legal_actions),
            (DiscardAction, DiscardAction, RiichiAction),
        )
        self.assertEqual(
            decision.context.legal_actions[0].tile,
            decision.mapping.candidates[0].tile,
        )

    def test_rejects_a_non_observation(self) -> None:
        with self.assertRaises(TypeError):
            build_decision(object(), (DiscardActionDescriptor(manzu(1), True),))

    def test_duplicate_semantic_actions_fail_before_building_a_context(self) -> None:
        source = observation(
            viewer_seat=EngineSeat.SOUTH,
            decision_kind=ObservationDecisionKind.DISCARD_REACTION,
            drawn_tile=None,
        )
        with self.assertRaises(AmbiguousActionMappingError):
            build_decision(
                source,
                (
                    PassActionDescriptor(pinzu(2), EngineSeat.EAST),
                    PassActionDescriptor(manzu(9), EngineSeat.EAST),
                ),
            )


class TwoStageRiichiTest(unittest.TestCase):
    """立直宣言と宣言牌打牌は、engine上で独立した2 decisionである。"""

    def test_turn_decision_offers_riichi_without_a_declaration_tile(self) -> None:
        decision = build_decision(
            observation(decision_kind=ObservationDecisionKind.TURN),
            (
                DiscardActionDescriptor(manzu(1), True),
                RiichiActionDescriptor(),
            ),
        )
        self.assertIn(RiichiAction(actor=Seat.SEAT_0), decision.context.legal_actions)
        self.assertIs(
            decision.mapping.resolve(RiichiAction(actor=Seat.SEAT_0)).__class__,
            RiichiActionDescriptor,
        )

    def test_riichi_discard_decision_only_offers_discards(self) -> None:
        options = (
            DiscardActionDescriptor(manzu(1), True),
            DiscardActionDescriptor(pinzu(2), False),
        )
        decision = build_decision(
            observation(decision_kind=ObservationDecisionKind.RIICHI_DISCARD),
            options,
        )
        self.assertEqual(
            tuple(type(action) for action in decision.context.legal_actions),
            (DiscardAction, DiscardAction),
        )
        # Arenaが宣言牌を選び直さず、engineが提示した候補をそのまま渡す。
        self.assertEqual(len(decision.context.legal_actions), len(options))


class EngineDecisionValueTest(unittest.TestCase):
    def test_rejects_a_context_and_mapping_from_different_seats(self) -> None:
        east = build_decision(observation(), (DiscardActionDescriptor(manzu(1), True),))
        west_mapping = build_action_mapping(
            observation(viewer_seat=EngineSeat.WEST, drawn_tile=None),
            (DiscardActionDescriptor(manzu(1), False),),
        )
        with self.assertRaises(SeatIdentityError):
            EngineDecision(context=east.context, mapping=west_mapping)

    def test_rejects_a_context_whose_actions_are_not_from_the_mapping(self) -> None:
        decision = build_decision(
            observation(),
            (
                DiscardActionDescriptor(manzu(1), True),
                RiichiActionDescriptor(),
            ),
        )
        other_mapping = build_action_mapping(
            observation(), (DiscardActionDescriptor(manzu(1), True),)
        )
        with self.assertRaises(SeatIdentityError):
            EngineDecision(context=decision.context, mapping=other_mapping)

    def test_rejects_non_value_arguments(self) -> None:
        decision = build_decision(
            observation(), (DiscardActionDescriptor(manzu(1), True),)
        )
        with self.assertRaises(TypeError):
            EngineDecision(context=object(), mapping=decision.mapping)
        with self.assertRaises(TypeError):
            EngineDecision(context=decision.context, mapping=object())


if __name__ == "__main__":
    unittest.main()
