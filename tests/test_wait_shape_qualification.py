import unittest

from lisjong.belief import SCALE
from lisjong.belief.hand_belief import HandBelief
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.tile import Tile, TileCategory, TileType

from lisjong_arena.wait_shape_qualification import (
    DEFENSE_DIAGNOSTIC_STATUS,
    PILOT_SEEDS,
    PRIMARY_SHAPES,
    SCIENTIFIC_EVAL_SEEDS,
    SCIENTIFIC_SELECT_SEEDS,
    SCIENTIFIC_TRAIN_SEEDS,
    WaitShapeAvailability,
    WaitShapeQualificationError,
    build_wait_shape_target,
    project_exact_wait_shapes,
    protocol_lock_document,
    protocol_lock_identity,
)
from lisjong_arena.wait_shape_qualification.protocol import (
    PUBLIC_RIICHI_ELIGIBILITY,
    TEACHER_LISJONG_REVISION,
)


def _tile(category: TileCategory, rank: int) -> Tile:
    return Tile(TileType(category, rank))


def _run(category: TileCategory, low_rank: int) -> tuple[Tile, ...]:
    return tuple(_tile(category, rank) for rank in range(low_rank, low_rank + 3))


def _exact_belief(
    *,
    wait: tuple[int, ...],
    tanki: tuple[int, ...],
    shanpon: tuple[int, ...],
    kanchan: tuple[int, ...],
    penchan: tuple[int, ...],
    ryanmen_low: tuple[int, ...],
    ryanmen_high: tuple[int, ...],
    kokushi: tuple[int, ...],
) -> HandBelief:
    return HandBelief(
        expected_count_raw=(0,) * 34,
        red_five_probability_raw=(0,) * 3,
        wait_probability_raw=wait,
        tanki_wait_probability_raw=tanki,
        shanpon_wait_probability_raw=shanpon,
        kanchan_wait_probability_raw=kanchan,
        penchan_wait_probability_raw=penchan,
        ryanmen_low_side_probability_raw=ryanmen_low,
        ryanmen_high_side_probability_raw=ryanmen_high,
        kokushi_wait_probability_raw=kokushi,
    )


class ProtocolLockTest(unittest.TestCase):
    def test_seed_populations_and_teacher_are_prelocked(self) -> None:
        self.assertEqual(PILOT_SEEDS, tuple(range(2000, 2096)))
        self.assertEqual(SCIENTIFIC_TRAIN_SEEDS, tuple(range(2100, 2196)))
        self.assertEqual(SCIENTIFIC_SELECT_SEEDS, tuple(range(2196, 2220)))
        self.assertEqual(SCIENTIFIC_EVAL_SEEDS, tuple(range(2220, 2260)))
        self.assertEqual(
            PRIMARY_SHAPES, ("TANKI", "SHANPON", "KANCHAN", "PENCHAN", "RYANMEN")
        )
        self.assertIs(PUBLIC_RIICHI_ELIGIBILITY, RiichiState.ACCEPTED)
        self.assertEqual(
            TEACHER_LISJONG_REVISION,
            "15799e5f0fe47f2e2b2c39060de804d99c51492d",
        )
        self.assertEqual(
            DEFENSE_DIAGNOSTIC_STATUS,
            "DEFENSE DIAGNOSTIC NOT APPLICABLE",
        )

    def test_protocol_document_is_deterministic_and_separates_pilot(self) -> None:
        first = protocol_lock_document()
        second = protocol_lock_document()
        self.assertEqual(first, second)
        self.assertEqual(protocol_lock_identity(), protocol_lock_identity())

        pilot = set(first["pilot"]["ordered_seeds"])
        scientific = first["scientific_population"]
        scientific_seeds = set(scientific["train_seeds"])
        scientific_seeds.update(scientific["select_seeds"])
        scientific_seeds.update(scientific["eval_seeds"])
        self.assertTrue(pilot.isdisjoint(scientific_seeds))
        self.assertTrue(first["pilot"]["scientific_reuse_forbidden"])


class CanonicalProjectionTest(unittest.TestCase):
    def test_projection_preserves_multi_label_and_merges_ryanmen_sides(self) -> None:
        zero = (0,) * 34
        wait = list(zero)
        tanki = list(zero)
        shanpon = list(zero)
        ryanmen_low = list(zero)
        ryanmen_high = list(zero)

        wait[0] = SCALE
        wait[1] = SCALE
        wait[2] = SCALE
        tanki[0] = SCALE
        shanpon[0] = SCALE
        ryanmen_low[1] = SCALE
        ryanmen_high[2] = SCALE

        projection = project_exact_wait_shapes(
            _exact_belief(
                wait=tuple(wait),
                tanki=tuple(tanki),
                shanpon=tuple(shanpon),
                kanchan=zero,
                penchan=zero,
                ryanmen_low=tuple(ryanmen_low),
                ryanmen_high=tuple(ryanmen_high),
                kokushi=zero,
            )
        )

        self.assertEqual(projection.ordinary, (1, 1, 0, 0, 1))
        self.assertEqual(projection.kokushi, 0)
        self.assertFalse(projection.is_kokushi_only)

    def test_projection_rejects_non_exact_probability_tables(self) -> None:
        zero = (0,) * 34
        wait = (1,) + (0,) * 33
        tanki = (1,) + (0,) * 33
        belief = _exact_belief(
            wait=wait,
            tanki=tanki,
            shanpon=zero,
            kanchan=zero,
            penchan=zero,
            ryanmen_low=zero,
            ryanmen_high=zero,
            kokushi=zero,
        )
        with self.assertRaises(WaitShapeQualificationError):
            project_exact_wait_shapes(belief)

    def test_projection_rejects_wait_or_mechanism_mismatch(self) -> None:
        zero = (0,) * 34
        tanki = (SCALE,) + (0,) * 33
        belief = _exact_belief(
            wait=zero,
            tanki=tanki,
            shanpon=zero,
            kanchan=zero,
            penchan=zero,
            ryanmen_low=zero,
            ryanmen_high=zero,
            kokushi=zero,
        )
        with self.assertRaises(WaitShapeQualificationError):
            project_exact_wait_shapes(belief)


class PublicRiichiTargetTest(unittest.TestCase):
    def test_accepted_tanki_target_uses_canonical_builder(self) -> None:
        concealed = (
            _run(TileCategory.MANZU, 1)
            + _run(TileCategory.PINZU, 4)
            + _run(TileCategory.SOUZU, 7)
            + _run(TileCategory.PINZU, 1)
            + (_tile(TileCategory.MANZU, 5),)
        )
        target = build_wait_shape_target(
            public_riichi=RiichiState.ACCEPTED,
            privileged_riichi_declared=True,
            concealed_tiles=concealed,
            melds=(),
        )

        self.assertIs(target.availability, WaitShapeAvailability.AVAILABLE)
        assert target.projection is not None
        self.assertEqual(target.projection.ordinary, (1, 0, 0, 0, 0))
        self.assertEqual(target.projection.kokushi, 0)

    def test_kokushi_only_is_valid_even_when_five_ordinary_shapes_are_zero(
        self,
    ) -> None:
        concealed = (
            _tile(TileCategory.MANZU, 1),
            _tile(TileCategory.MANZU, 9),
            _tile(TileCategory.PINZU, 1),
            _tile(TileCategory.PINZU, 9),
            _tile(TileCategory.SOUZU, 1),
            _tile(TileCategory.SOUZU, 9),
            *(_tile(TileCategory.HONOR, rank) for rank in range(1, 8)),
        )
        target = build_wait_shape_target(
            public_riichi=RiichiState.ACCEPTED,
            privileged_riichi_declared=True,
            concealed_tiles=concealed,
            melds=(),
        )

        self.assertIs(target.availability, WaitShapeAvailability.AVAILABLE)
        assert target.projection is not None
        self.assertEqual(target.projection.ordinary, (0, 0, 0, 0, 0))
        self.assertEqual(target.projection.kokushi, 1)
        self.assertTrue(target.projection.is_kokushi_only)

    def test_non_accepted_riichi_is_masked_before_hidden_truth_is_required(
        self,
    ) -> None:
        target = build_wait_shape_target(
            public_riichi=RiichiState.DECLARED,
            privileged_riichi_declared=True,
            concealed_tiles=None,
            melds=None,
        )
        self.assertIs(
            target.availability,
            WaitShapeAvailability.NOT_ACCEPTED_RIICHI,
        )
        self.assertIsNone(target.projection)

    def test_accepted_riichi_binding_mismatch_is_reason_coded(self) -> None:
        target = build_wait_shape_target(
            public_riichi=RiichiState.ACCEPTED,
            privileged_riichi_declared=False,
            concealed_tiles=None,
            melds=None,
        )
        self.assertIs(
            target.availability,
            WaitShapeAvailability.RIICHI_BINDING_MISMATCH,
        )
        self.assertIsNone(target.projection)


if __name__ == "__main__":
    unittest.main()
