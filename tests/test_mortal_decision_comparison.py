import unittest

from lisjong.policy_contract import (
    DecisionTrace,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)
from riichienv import Action, ActionType

from lisjong_arena.mortal_decision_comparison import (
    MortalDecisionComparisonError,
    MortalDecisionComparisonRecord,
    MortalDecisionComparisonSummary,
    NormalizedRiichiEnvAction,
    RiichiEnvActionKind,
    normalize_legal_riichienv_action,
)

_TILE = Tile(TileType(TileCategory.MANZU, 1))


class _Observation:
    def __init__(self, legal_actions, *, drawn_tile=None) -> None:
        self.player_id = 0
        self.drawn_tile = drawn_tile
        self._legal_actions = list(legal_actions)

    def legal_actions(self):
        return list(self._legal_actions)


def _policy_input() -> PolicyInput:
    player = PlayerPublicState(
        score=25000,
        discards=(),
        melds=(),
        riichi=RiichiState.NONE,
    )
    return PolicyInput(
        self_seat=Seat.SEAT_0,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(_TILE,),
            live_wall_tiles_remaining=70,
        ),
        players=(player, player, player, player),
        own_hand=OwnHandState(concealed_tiles=(_TILE,), drawn_tile=None),
    )


def _normalized(kind: RiichiEnvActionKind) -> NormalizedRiichiEnvAction:
    return NormalizedRiichiEnvAction(
        kind=kind,
        actor=Seat.SEAT_0,
        tile=None,
        consume_tiles=(),
        tsumogiri=None,
    )


def _record(
    ordinal: int,
    driver: RiichiEnvActionKind,
    shadow: RiichiEnvActionKind,
) -> MortalDecisionComparisonRecord:
    selected = PassAction(actor=Seat.SEAT_0)
    driver_action = _normalized(driver)
    shadow_action = _normalized(shadow)
    return MortalDecisionComparisonRecord(
        seed=3,
        rotation=0,
        mortal_seat=Seat.SEAT_0,
        decision_ordinal=ordinal,
        shadow_policy_identity="combined",
        policy_input=_policy_input(),
        decision_trace=DecisionTrace(
            legal_actions=(selected,), selected_action=selected
        ),
        driver_mortal_action=driver_action,
        shadow_policy_action=shadow_action,
        agreement=driver_action == shadow_action,
    )


class MortalDecisionActionNormalizationTest(unittest.TestCase):
    def test_representation_only_physical_copy_difference_is_agreement(self) -> None:
        first_copy = Action(type=ActionType.DISCARD, actor=0, tile=0)
        second_copy = Action(type=ActionType.DISCARD, actor=0, tile=1)
        observation = _Observation([first_copy, second_copy])

        first = normalize_legal_riichienv_action(observation, first_copy)
        second = normalize_legal_riichienv_action(observation, second_copy)

        self.assertEqual(first, second)
        self.assertEqual(first.kind, RiichiEnvActionKind.DISCARD)
        self.assertFalse(first.tsumogiri)

    def test_semantically_different_actions_are_not_equal(self) -> None:
        discard = Action(type=ActionType.DISCARD, actor=0, tile=0)
        passed = Action(type=ActionType.PASS, actor=0)
        observation = _Observation([discard, passed])

        self.assertNotEqual(
            normalize_legal_riichienv_action(observation, discard),
            normalize_legal_riichienv_action(observation, passed),
        )

    def test_consume_order_and_physical_copies_do_not_change_semantics(self) -> None:
        first = Action(type=ActionType.ANKAN, actor=0, consume_tiles=[0, 1, 2, 3])
        reordered = Action(type=ActionType.ANKAN, actor=0, consume_tiles=[3, 2, 1, 0])
        observation = _Observation([first, reordered])

        self.assertEqual(
            normalize_legal_riichienv_action(observation, first),
            normalize_legal_riichienv_action(observation, reordered),
        )

    def test_selected_action_not_legal_on_observation_fails_closed(self) -> None:
        passed = Action(type=ActionType.PASS, actor=0)
        illegal = Action(type=ActionType.DISCARD, actor=0, tile=0)

        with self.assertRaisesRegex(MortalDecisionComparisonError, "not legal"):
            normalize_legal_riichienv_action(_Observation([passed]), illegal)


class MortalDecisionComparisonSummaryTest(unittest.TestCase):
    def test_aggregation_and_disagreement_extraction_are_deterministic(self) -> None:
        agreement = _record(0, RiichiEnvActionKind.PASS, RiichiEnvActionKind.PASS)
        disagreement_one = _record(
            1, RiichiEnvActionKind.RIICHI, RiichiEnvActionKind.PASS
        )
        disagreement_two = _record(
            2, RiichiEnvActionKind.PASS, RiichiEnvActionKind.RIICHI
        )

        summary = MortalDecisionComparisonSummary.from_records(
            (agreement, disagreement_one, disagreement_two)
        )

        self.assertEqual(summary.total_paired_decisions, 3)
        self.assertEqual(summary.agreements, 1)
        self.assertEqual(summary.disagreements_count, 2)
        self.assertEqual(summary.agreement_rate, 1 / 3)
        self.assertEqual(
            [
                (
                    item.driver_mortal_kind.value,
                    item.shadow_policy_kind.value,
                    item.count,
                )
                for item in summary.action_kind_pairs
            ],
            [
                ("pass", "pass", 1),
                ("pass", "riichi", 1),
                ("riichi", "pass", 1),
            ],
        )
        self.assertEqual(summary.disagreements(), (disagreement_one, disagreement_two))
        self.assertEqual(summary.disagreements(first=1), (disagreement_one,))
        self.assertEqual(
            summary.disagreements(driver_kind=RiichiEnvActionKind.PASS),
            (disagreement_two,),
        )
        self.assertIs(summary.disagreements()[0], disagreement_one)


if __name__ == "__main__":
    unittest.main()
