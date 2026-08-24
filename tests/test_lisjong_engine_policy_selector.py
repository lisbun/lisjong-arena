"""lisjong Policyをengine `ActionSelector`として提示するArena-side callable。"""

import unittest

from _lisjong_engine_fixtures import manzu, observation, pinzu, souzu
from lisjong.policies.minimal import MinimalPolicy
from lisjong.policy_contract import (
    DiscardAction,
    PolicyActionValidationError,
    RiichiAction,
    Seat,
    Tile,
    TileCategory,
    TileType,
)
from lisjong_engine.action_descriptor import (
    DiscardActionDescriptor,
    RiichiActionDescriptor,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.errors import SeatIdentityError, UnmappedActionError
from lisjong_arena.lisjong_engine.policy_selector import (
    PolicySeatSelector,
    build_seat_selectors,
)


def _tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red)


class _RecordingPolicy:
    """呼び出されたDecisionContextを記録し、指定indexの候補を返すstub Policy。"""

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.decisions = []

    def choose_action(self, decision):
        self.decisions.append(decision)
        return decision.legal_actions[self.index]


class _RaisingPolicy:
    class Failure(RuntimeError):
        pass

    def choose_action(self, decision):
        raise self.Failure("policy failed")


class _IllegalActionPolicy:
    def __init__(self, action) -> None:
        self._action = action

    def choose_action(self, decision):
        return self._action


class SelectorTest(unittest.TestCase):
    def test_returns_the_original_descriptor_object(self) -> None:
        options = (
            DiscardActionDescriptor(manzu(1), True),
            RiichiActionDescriptor(),
        )
        selector = PolicySeatSelector(EngineSeat.EAST, _RecordingPolicy(index=1))
        self.assertIs(selector(observation(), options), options[1])

    def test_passes_the_projected_decision_context_to_the_policy(self) -> None:
        policy = _RecordingPolicy()
        selector = PolicySeatSelector(EngineSeat.EAST, policy)
        selector(
            observation(hand_tiles=(manzu(1),), drawn_tile=manzu(1)),
            (DiscardActionDescriptor(manzu(1), True),),
        )
        (decision,) = policy.decisions
        self.assertIs(decision.input.self_seat, Seat.SEAT_0)
        self.assertEqual(
            decision.input.own_hand.drawn_tile, _tile(TileCategory.MANZU, 1)
        )
        self.assertEqual(
            decision.legal_actions,
            (
                DiscardAction(
                    actor=Seat.SEAT_0,
                    tile=_tile(TileCategory.MANZU, 1),
                    tsumogiri=True,
                ),
            ),
        )

    def test_uses_execute_policy_validation_for_an_illegal_result(self) -> None:
        selector = PolicySeatSelector(
            EngineSeat.EAST,
            _IllegalActionPolicy(RiichiAction(actor=Seat.SEAT_0)),
        )
        with self.assertRaises(PolicyActionValidationError):
            selector(observation(), (DiscardActionDescriptor(manzu(1), True),))

    def test_rejects_a_result_for_another_seat(self) -> None:
        selector = PolicySeatSelector(
            EngineSeat.EAST,
            _IllegalActionPolicy(
                DiscardAction(
                    actor=Seat.SEAT_1,
                    tile=_tile(TileCategory.MANZU, 1),
                    tsumogiri=True,
                )
            ),
        )
        with self.assertRaises(PolicyActionValidationError):
            selector(observation(), (DiscardActionDescriptor(manzu(1), True),))

    def test_propagates_policy_exceptions_without_fallback(self) -> None:
        selector = PolicySeatSelector(EngineSeat.EAST, _RaisingPolicy())
        with self.assertRaises(_RaisingPolicy.Failure):
            selector(
                observation(),
                (
                    DiscardActionDescriptor(manzu(1), True),
                    RiichiActionDescriptor(),
                ),
            )

    def test_rejects_an_observation_for_another_seat(self) -> None:
        selector = PolicySeatSelector(EngineSeat.SOUTH, _RecordingPolicy())
        with self.assertRaises(SeatIdentityError):
            selector(observation(), (DiscardActionDescriptor(manzu(1), True),))

    def test_rejects_a_non_observation(self) -> None:
        selector = PolicySeatSelector(EngineSeat.EAST, _RecordingPolicy())
        with self.assertRaises(TypeError):
            selector(object(), (DiscardActionDescriptor(manzu(1), True),))

    def test_rejects_a_non_policy(self) -> None:
        with self.assertRaises(TypeError):
            PolicySeatSelector(EngineSeat.EAST, object())

    def test_rejects_a_lisjong_seat(self) -> None:
        with self.assertRaises(TypeError):
            PolicySeatSelector(Seat.SEAT_0, _RecordingPolicy())

    def test_builds_a_fresh_decision_local_mapping_per_call(self) -> None:
        """selectorはdecision間でmappingを保持しない。"""
        policy = _RecordingPolicy()
        selector = PolicySeatSelector(EngineSeat.EAST, policy)
        first = (DiscardActionDescriptor(manzu(1), True),)
        second = (DiscardActionDescriptor(pinzu(2), False),)
        self.assertIs(selector(observation(), first), first[0])
        self.assertIs(selector(observation(), second), second[0])
        self.assertEqual(len(policy.decisions), 2)
        self.assertNotEqual(
            policy.decisions[0].legal_actions,
            policy.decisions[1].legal_actions,
        )
        self.assertEqual(set(vars(type(selector))["__slots__"]), {"_seat", "_policy"})

    def test_a_stale_action_from_another_decision_is_not_resolved(self) -> None:
        policy = _RecordingPolicy()
        selector = PolicySeatSelector(EngineSeat.EAST, policy)
        selector(observation(), (DiscardActionDescriptor(manzu(1), True),))
        stale = policy.decisions[0].legal_actions[0]
        stale_selector = PolicySeatSelector(
            EngineSeat.EAST, _IllegalActionPolicy(stale)
        )
        with self.assertRaises((PolicyActionValidationError, UnmappedActionError)):
            stale_selector(observation(), (DiscardActionDescriptor(pinzu(2), False),))


class TwoStageRiichiSelectorTest(unittest.TestCase):
    def test_riichi_choice_returns_the_riichi_descriptor_not_a_discard(self) -> None:
        options = (
            DiscardActionDescriptor(manzu(1), True),
            RiichiActionDescriptor(),
        )
        selector = PolicySeatSelector(EngineSeat.EAST, _RecordingPolicy(index=1))
        self.assertIsInstance(
            selector(observation(decision_kind=ObservationDecisionKind.TURN), options),
            RiichiActionDescriptor,
        )

    def test_declaration_tile_is_chosen_in_the_follow_up_decision(self) -> None:
        options = (
            DiscardActionDescriptor(manzu(1), True),
            DiscardActionDescriptor(souzu(5, is_red=True), False),
        )
        policy = _RecordingPolicy(index=1)
        selector = PolicySeatSelector(EngineSeat.EAST, policy)
        chosen = selector(
            observation(decision_kind=ObservationDecisionKind.RIICHI_DISCARD),
            options,
        )
        self.assertIs(chosen, options[1])
        (decision,) = policy.decisions
        self.assertEqual(
            tuple(type(action) for action in decision.legal_actions),
            (DiscardAction, DiscardAction),
        )


class BuildSeatSelectorsTest(unittest.TestCase):
    def test_builds_one_selector_per_engine_seat(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        selectors = build_seat_selectors(policies)
        self.assertEqual(set(selectors), set(EngineSeat))
        for seat, selector in selectors.items():
            self.assertIsInstance(selector, PolicySeatSelector)
            self.assertIs(selector.seat, seat)
            self.assertIs(selector.policy, policies[seat])

    def test_missing_seat_fails_closed(self) -> None:
        policies = {
            seat: MinimalPolicy() for seat in EngineSeat if seat is not EngineSeat.NORTH
        }
        with self.assertRaises(ValueError):
            build_seat_selectors(policies)

    def test_extra_key_fails_closed(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        policies["extra"] = MinimalPolicy()
        with self.assertRaises(ValueError):
            build_seat_selectors(policies)

    def test_rejects_a_non_mapping(self) -> None:
        with self.assertRaises(TypeError):
            build_seat_selectors([MinimalPolicy()] * 4)

    def test_rejects_a_non_policy_value(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        policies[EngineSeat.WEST] = object()
        with self.assertRaises(TypeError):
            build_seat_selectors(policies)

    def test_keeps_each_seats_own_policy_instance(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        selectors = build_seat_selectors(policies)
        instances = [id(selectors[seat].policy) for seat in EngineSeat]
        self.assertEqual(len(set(instances)), 4)


if __name__ == "__main__":
    unittest.main()
