"""engine `ActionDescriptor` <-> lisjong `InternalAction`のdecision-local mapping。"""

import unittest

from _engine_fixtures import (
    honor,
    manzu,
    observation,
    pinzu,
    pon_meld,
    seat_melds,
    souzu,
)
from lisjong.policy_contract import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    Seat,
    Tile,
    TileCategory,
    TileType,
    TsumoAction,
)
from lisjong_engine.action_descriptor import (
    ACTION_DESCRIPTOR_TYPES,
    AnkanActionDescriptor,
    ChiActionDescriptor,
    DaiminkanActionDescriptor,
    DiscardActionDescriptor,
    KakanActionDescriptor,
    NineTerminalsActionDescriptor,
    PassActionDescriptor,
    PonActionDescriptor,
    RiichiActionDescriptor,
    RonActionDescriptor,
    TsumoActionDescriptor,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.engine.action_mapping import (
    _TRANSLATORS,
    EngineActionMapping,
    build_action_mapping,
    internal_action_from_descriptor,
)
from lisjong_arena.engine.errors import (
    AmbiguousActionMappingError,
    EngineBridgeError,
    KakanProvenanceError,
    SeatIdentityError,
    UnmappedActionError,
    UnsupportedEngineValueError,
)


def _tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red)


def _translate(descriptor, observation_value=None):
    source = observation() if observation_value is None else observation_value
    return internal_action_from_descriptor(
        descriptor,
        Seat(list(EngineSeat).index(source.viewer_seat)),
        source,
    )


class ActionVariantTest(unittest.TestCase):
    def test_every_descriptor_variant_has_a_translator(self) -> None:
        self.assertEqual(set(_TRANSLATORS), set(ACTION_DESCRIPTOR_TYPES))

    def test_discard(self) -> None:
        self.assertEqual(
            _translate(DiscardActionDescriptor(souzu(5, is_red=True), True)),
            DiscardAction(
                actor=Seat.SEAT_0,
                tile=_tile(TileCategory.SOUZU, 5, is_red=True),
                tsumogiri=True,
            ),
        )

    def test_discard_keeps_tsumogiri_false(self) -> None:
        action = _translate(DiscardActionDescriptor(manzu(1), False))
        self.assertFalse(action.tsumogiri)

    def test_riichi(self) -> None:
        self.assertEqual(
            _translate(RiichiActionDescriptor()),
            RiichiAction(actor=Seat.SEAT_0),
        )

    def test_chi(self) -> None:
        self.assertEqual(
            _translate(
                ChiActionDescriptor(
                    souzu(5, is_red=True), (souzu(4), souzu(6)), EngineSeat.NORTH
                )
            ),
            ChiAction(
                actor=Seat.SEAT_0,
                target=Seat.SEAT_3,
                called_tile=_tile(TileCategory.SOUZU, 5, is_red=True),
                consumed_tiles=(
                    _tile(TileCategory.SOUZU, 4),
                    _tile(TileCategory.SOUZU, 6),
                ),
            ),
        )

    def test_pon(self) -> None:
        self.assertEqual(
            _translate(
                PonActionDescriptor(manzu(3), (manzu(3), manzu(3)), EngineSeat.SOUTH)
            ),
            PonAction(
                actor=Seat.SEAT_0,
                target=Seat.SEAT_1,
                called_tile=_tile(TileCategory.MANZU, 3),
                consumed_tiles=(
                    _tile(TileCategory.MANZU, 3),
                    _tile(TileCategory.MANZU, 3),
                ),
            ),
        )

    def test_daiminkan(self) -> None:
        self.assertEqual(
            _translate(
                DaiminkanActionDescriptor(
                    pinzu(7), (pinzu(7), pinzu(7), pinzu(7)), EngineSeat.WEST
                )
            ),
            DaiminkanAction(
                actor=Seat.SEAT_0,
                target=Seat.SEAT_2,
                called_tile=_tile(TileCategory.PINZU, 7),
                consumed_tiles=(_tile(TileCategory.PINZU, 7),) * 3,
            ),
        )

    def test_ankan(self) -> None:
        self.assertEqual(
            _translate(AnkanActionDescriptor((honor(1),) * 4)),
            AnkanAction(
                actor=Seat.SEAT_0,
                tiles=(_tile(TileCategory.HONOR, 1),) * 4,
            ),
        )

    def test_kakan(self) -> None:
        source = observation(
            melds=seat_melds(east=(pon_meld(manzu(3), EngineSeat.SOUTH),))
        )
        self.assertEqual(
            _translate(KakanActionDescriptor(manzu(3)), source),
            KakanAction(
                actor=Seat.SEAT_0,
                added_tile=_tile(TileCategory.MANZU, 3),
                from_seat=Seat.SEAT_1,
                called_tile=_tile(TileCategory.MANZU, 3),
            ),
        )

    def test_ron(self) -> None:
        self.assertEqual(
            _translate(RonActionDescriptor(pinzu(2), EngineSeat.WEST)),
            RonAction(
                actor=Seat.SEAT_0,
                target=Seat.SEAT_2,
                winning_tile=_tile(TileCategory.PINZU, 2),
            ),
        )

    def test_tsumo(self) -> None:
        self.assertEqual(
            _translate(TsumoActionDescriptor(pinzu(2))),
            TsumoAction(
                actor=Seat.SEAT_0,
                winning_tile=_tile(TileCategory.PINZU, 2),
            ),
        )

    def test_pass(self) -> None:
        self.assertEqual(
            _translate(PassActionDescriptor(pinzu(2), EngineSeat.WEST)),
            PassAction(actor=Seat.SEAT_0),
        )

    def test_nine_terminals_becomes_kyuushu_kyuuhai(self) -> None:
        self.assertEqual(
            _translate(NineTerminalsActionDescriptor()),
            KyuushuKyuuhaiAction(actor=Seat.SEAT_0),
        )

    def test_rejects_a_non_descriptor(self) -> None:
        with self.assertRaises(TypeError):
            _translate(object())

    def test_unsupported_descriptor_type_fails_closed(self) -> None:
        class ForeignDescriptor(DiscardActionDescriptor):
            pass

        foreign = ForeignDescriptor(manzu(1), False)
        self.assertIsInstance(foreign, ACTION_DESCRIPTOR_TYPES)
        with self.assertRaises(UnsupportedEngineValueError):
            _translate(foreign)


class ReactionTest(unittest.TestCase):
    """reaction decisionのtarget seatとcalled tileを維持する。"""

    def _reaction_observation(self):
        return observation(
            viewer_seat=EngineSeat.SOUTH,
            decision_kind=ObservationDecisionKind.DISCARD_REACTION,
            drawn_tile=None,
        )

    def test_reaction_variants_keep_target_and_tile(self) -> None:
        source = self._reaction_observation()
        cases = (
            (
                PassActionDescriptor(pinzu(2), EngineSeat.EAST),
                PassAction(actor=Seat.SEAT_1),
            ),
            (
                RonActionDescriptor(pinzu(2), EngineSeat.EAST),
                RonAction(
                    actor=Seat.SEAT_1,
                    target=Seat.SEAT_0,
                    winning_tile=_tile(TileCategory.PINZU, 2),
                ),
            ),
            (
                ChiActionDescriptor(pinzu(2), (pinzu(3), pinzu(4)), EngineSeat.EAST),
                ChiAction(
                    actor=Seat.SEAT_1,
                    target=Seat.SEAT_0,
                    called_tile=_tile(TileCategory.PINZU, 2),
                    consumed_tiles=(
                        _tile(TileCategory.PINZU, 3),
                        _tile(TileCategory.PINZU, 4),
                    ),
                ),
            ),
            (
                PonActionDescriptor(pinzu(2), (pinzu(2), pinzu(2)), EngineSeat.EAST),
                PonAction(
                    actor=Seat.SEAT_1,
                    target=Seat.SEAT_0,
                    called_tile=_tile(TileCategory.PINZU, 2),
                    consumed_tiles=(_tile(TileCategory.PINZU, 2),) * 2,
                ),
            ),
            (
                DaiminkanActionDescriptor(
                    pinzu(2), (pinzu(2), pinzu(2), pinzu(2)), EngineSeat.EAST
                ),
                DaiminkanAction(
                    actor=Seat.SEAT_1,
                    target=Seat.SEAT_0,
                    called_tile=_tile(TileCategory.PINZU, 2),
                    consumed_tiles=(_tile(TileCategory.PINZU, 2),) * 3,
                ),
            ),
        )
        for descriptor, expected in cases:
            with self.subTest(descriptor=type(descriptor).__name__):
                self.assertEqual(_translate(descriptor, source), expected)

    def test_chi_target_must_be_the_actors_kamicha(self) -> None:
        """lisjong `ChiAction`の上家制約とengineのseat順が整合する。"""
        source = observation(
            viewer_seat=EngineSeat.SOUTH,
            decision_kind=ObservationDecisionKind.DISCARD_REACTION,
            drawn_tile=None,
        )
        with self.assertRaises(ValueError):
            _translate(
                ChiActionDescriptor(pinzu(2), (pinzu(3), pinzu(4)), EngineSeat.WEST),
                source,
            )

    def test_ankan_reaction_ron_keeps_the_ankan_seat_as_target(self) -> None:
        source = observation(
            viewer_seat=EngineSeat.NORTH,
            decision_kind=ObservationDecisionKind.ANKAN_REACTION,
            drawn_tile=None,
        )
        action = _translate(RonActionDescriptor(honor(1), EngineSeat.WEST), source)
        self.assertEqual(
            action,
            RonAction(
                actor=Seat.SEAT_3,
                target=Seat.SEAT_2,
                winning_tile=_tile(TileCategory.HONOR, 1),
            ),
        )


class KakanProvenanceTest(unittest.TestCase):
    def test_normal_added_tile_resolves_the_source_pon(self) -> None:
        source = observation(
            melds=seat_melds(east=(pon_meld(pinzu(5), EngineSeat.NORTH),))
        )
        action = _translate(KakanActionDescriptor(pinzu(5)), source)
        self.assertIs(action.from_seat, Seat.SEAT_3)
        self.assertEqual(action.called_tile, _tile(TileCategory.PINZU, 5))
        self.assertEqual(action.added_tile, _tile(TileCategory.PINZU, 5))

    def test_red_added_tile_resolves_a_non_red_source_pon_by_tile_type(self) -> None:
        source = observation(
            melds=seat_melds(east=(pon_meld(pinzu(5), EngineSeat.SOUTH),))
        )
        action = _translate(KakanActionDescriptor(pinzu(5, is_red=True)), source)
        self.assertEqual(action.added_tile, _tile(TileCategory.PINZU, 5, is_red=True))
        # 元Pon自身のcalled tileのred semanticを維持する。
        self.assertEqual(action.called_tile, _tile(TileCategory.PINZU, 5))
        self.assertIs(action.from_seat, Seat.SEAT_1)

    def test_non_red_added_tile_resolves_a_red_source_pon_by_tile_type(self) -> None:
        source = observation(
            melds=seat_melds(
                east=(
                    pon_meld(
                        pinzu(5),
                        EngineSeat.WEST,
                        called_tile=pinzu(5, is_red=True),
                    ),
                )
            )
        )
        action = _translate(KakanActionDescriptor(pinzu(5)), source)
        self.assertEqual(action.added_tile, _tile(TileCategory.PINZU, 5))
        self.assertEqual(action.called_tile, _tile(TileCategory.PINZU, 5, is_red=True))

    def test_no_source_pon_fails_closed(self) -> None:
        with self.assertRaises(KakanProvenanceError):
            _translate(KakanActionDescriptor(pinzu(5)), observation())

    def test_pon_of_a_different_tile_type_is_not_a_source(self) -> None:
        source = observation(
            melds=seat_melds(east=(pon_meld(manzu(5), EngineSeat.SOUTH),))
        )
        with self.assertRaises(KakanProvenanceError):
            _translate(KakanActionDescriptor(pinzu(5)), source)

    def test_another_seats_pon_is_not_a_source(self) -> None:
        source = observation(
            melds=seat_melds(south=(pon_meld(pinzu(5), EngineSeat.EAST),))
        )
        with self.assertRaises(KakanProvenanceError):
            _translate(KakanActionDescriptor(pinzu(5)), source)

    def test_ambiguous_source_fails_closed(self) -> None:
        source = observation(
            melds=seat_melds(
                east=(
                    pon_meld(pinzu(5), EngineSeat.SOUTH),
                    pon_meld(pinzu(5), EngineSeat.WEST),
                )
            )
        )
        with self.assertRaises(KakanProvenanceError):
            _translate(KakanActionDescriptor(pinzu(5)), source)

    def test_existing_kakan_meld_is_not_treated_as_a_source_pon(self) -> None:
        from lisjong_engine.public_state import PublicMeld as EnginePublicMeld
        from lisjong_engine.public_state import PublicMeldType

        source = observation(
            melds=seat_melds(
                east=(
                    EnginePublicMeld(
                        meld_type=PublicMeldType.KAKAN,
                        tiles=(pinzu(5),) * 4,
                        from_seat=EngineSeat.SOUTH,
                        called_tile=pinzu(5),
                    ),
                )
            )
        )
        with self.assertRaises(KakanProvenanceError):
            _translate(KakanActionDescriptor(pinzu(5)), source)


class DecisionLocalMappingTest(unittest.TestCase):
    def _turn_options(self):
        return (
            DiscardActionDescriptor(manzu(1), True),
            DiscardActionDescriptor(pinzu(2), False),
            RiichiActionDescriptor(),
        )

    def test_candidates_keep_the_offered_option_order(self) -> None:
        options = self._turn_options()
        mapping = build_action_mapping(observation(), options)
        self.assertEqual(
            mapping.candidates,
            (
                DiscardAction(
                    actor=Seat.SEAT_0,
                    tile=_tile(TileCategory.MANZU, 1),
                    tsumogiri=True,
                ),
                DiscardAction(
                    actor=Seat.SEAT_0,
                    tile=_tile(TileCategory.PINZU, 2),
                    tsumogiri=False,
                ),
                RiichiAction(actor=Seat.SEAT_0),
            ),
        )

    def test_round_trips_every_candidate_back_to_its_descriptor(self) -> None:
        options = self._turn_options()
        mapping = build_action_mapping(observation(), options)
        for candidate, descriptor in zip(mapping.candidates, options, strict=True):
            self.assertIs(mapping.resolve(candidate), descriptor)

    def test_self_seat_comes_from_the_observation_viewer_seat(self) -> None:
        mapping = build_action_mapping(
            observation(viewer_seat=EngineSeat.WEST, drawn_tile=None),
            (DiscardActionDescriptor(manzu(1), False),),
        )
        self.assertIs(mapping.self_seat, Seat.SEAT_2)
        self.assertEqual(
            tuple(action.actor for action in mapping.candidates),
            (Seat.SEAT_2,),
        )

    def test_resolve_rejects_an_action_that_was_not_offered(self) -> None:
        mapping = build_action_mapping(
            observation(), (DiscardActionDescriptor(manzu(1), True),)
        )
        with self.assertRaises(UnmappedActionError):
            mapping.resolve(RiichiAction(actor=Seat.SEAT_0))

    def test_resolve_rejects_another_seats_action(self) -> None:
        mapping = build_action_mapping(
            observation(), (DiscardActionDescriptor(manzu(1), True),)
        )
        with self.assertRaises(SeatIdentityError):
            mapping.resolve(
                DiscardAction(
                    actor=Seat.SEAT_1,
                    tile=_tile(TileCategory.MANZU, 1),
                    tsumogiri=True,
                )
            )

    def test_a_new_decision_builds_an_independent_mapping(self) -> None:
        """mappingは1 seat・1 decisionに閉じ、別decisionへ再利用しない。"""
        first = build_action_mapping(
            observation(), (DiscardActionDescriptor(manzu(1), True),)
        )
        second = build_action_mapping(
            observation(), (DiscardActionDescriptor(pinzu(2), False),)
        )
        self.assertIsNot(first, second)
        self.assertNotEqual(first.candidates, second.candidates)
        with self.assertRaises(UnmappedActionError):
            second.resolve(first.candidates[0])

    def test_duplicate_semantic_mapping_fails_closed(self) -> None:
        """複数descriptorが同じInternalActionへcollapseしたらrepresentativeを選ばない。"""
        options = (
            PassActionDescriptor(pinzu(2), EngineSeat.EAST),
            PassActionDescriptor(manzu(9), EngineSeat.EAST),
        )
        source = observation(
            viewer_seat=EngineSeat.SOUTH,
            decision_kind=ObservationDecisionKind.DISCARD_REACTION,
            drawn_tile=None,
        )
        with self.assertRaises(AmbiguousActionMappingError):
            build_action_mapping(source, options)

    def test_empty_options_fail_closed(self) -> None:
        with self.assertRaises(EngineBridgeError):
            build_action_mapping(observation(), ())

    def test_rejects_a_non_observation(self) -> None:
        with self.assertRaises(TypeError):
            build_action_mapping(object(), (DiscardActionDescriptor(manzu(1), True),))

    def test_rejects_non_iterable_options(self) -> None:
        with self.assertRaises(TypeError):
            build_action_mapping(observation(), 3)

    def test_mapping_requires_at_least_one_candidate(self) -> None:
        with self.assertRaises(EngineBridgeError):
            EngineActionMapping(self_seat=Seat.SEAT_0, descriptors_by_action={})


if __name__ == "__main__":
    unittest.main()
