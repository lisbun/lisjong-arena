"""Learned Policy Stage 1 feature, layout, and fail-closed contracts."""

import hashlib
import inspect
import math
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from _learned_policy_input_fixtures import (
    complex_policy_input,
    discard_population,
    honor,
    manzu,
    minimal_policy_input,
    pinzu,
    player,
    rotate_policy_input,
)
from _lisjong_engine_fixtures import observation as engine_observation
from lisjong.policy_contract import (
    MeldKind,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    PublicMeld,
    RiichiState,
    Seat,
    Wind,
)

import lisjong_arena.learned_policy_input.tensor as tensor_module
from lisjong_arena.learned_policy_input import (
    FEATURE_DIM,
    FEATURE_GROUPS,
    FEATURE_INDEX_DESCRIPTORS,
    FEATURE_SEMANTICS_ID,
    MAX_CONCEALED_TILES,
    MAX_DORA_INDICATORS,
    MAX_GLOBAL_DISCARDS,
    MAX_LIVE_WALL_TILES,
    MAX_MELDS_PER_PLAYER,
    TENSOR_DTYPE,
    TENSOR_SCHEMA_VERSION,
    TILE_AXIS_LABELS,
    FeatureDimensionError,
    PolicyInputFeatureValidationError,
    RelativeSeat,
    UnsupportedFeatureSemanticsError,
    UnsupportedTensorSchemaVersionError,
    build_policy_input_feature,
    schema_fingerprint,
    tensor_values,
)
from lisjong_arena.lisjong_engine.policy_input import (
    build_policy_input as build_engine_policy_input,
)
from lisjong_arena.phase6_snapshot.feature import (
    FEATURE_SEMANTICS_ID as PHASE6_FEATURE_SEMANTICS_ID,
)
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM as PHASE6_FEATURE_DIM

_EXPECTED_TILE_LABELS = tuple(
    [f"{rank}m" for rank in range(1, 10)]
    + [f"{rank}p" for rank in range(1, 10)]
    + [f"{rank}s" for rank in range(1, 10)]
    + [f"{rank}z" for rank in range(1, 8)]
    + ["5m-red", "5p-red", "5s-red"]
)
_EXPECTED_SEAT_LABELS = ("self", "shimocha", "toimen", "kamicha")
_EXPECTED_RIICHI_LABELS = ("none", "declared", "accepted")
_EXPECTED_MELD_LABELS = ("chi", "pon", "daiminkan", "ankan", "kakan")
_SCHEMA_FINGERPRINTS = {
    "arena-policy-input-tensor-v1": (
        "cb02f8ec43861d277deaed0a0592f3d08cc4f26e351d8e27550b173f9b2059de"
    )
}


def _expected_tile_one_hot(prefix: str) -> list[str]:
    return [f"{prefix}.tile[{label}]:one_hot" for label in _EXPECTED_TILE_LABELS]


def _expected_index_descriptors() -> tuple[str, ...]:
    """Serialize v1 index meaning without using production schema builders."""
    values: list[str] = []
    values.extend(
        f"round.round_wind[{wind}]:one_hot"
        for wind in ("east", "south", "west", "north")
    )
    values.extend(f"round.hand_number[{number}]:one_hot" for number in range(1, 5))
    values.extend(
        f"round.dealer_relative_seat[{seat}]:one_hot" for seat in _EXPECTED_SEAT_LABELS
    )
    values.extend(
        f"derived.self_wind[{wind}]:one_hot"
        for wind in ("east", "south", "west", "north")
    )
    values.extend(
        (
            "round.honba:scalar/10,no_clip",
            "round.riichi_sticks:scalar/10,no_clip",
            "round.live_wall_tiles_remaining:scalar/84,domain=0..84",
        )
    )
    for slot in range(5):
        prefix = f"round.dora_indicators[{slot}]"
        values.append(f"{prefix}.present:binary")
        values.extend(_expected_tile_one_hot(prefix))
    for seat in _EXPECTED_SEAT_LABELS:
        player_prefix = f"players[{seat}]"
        values.append(f"{player_prefix}.score:scalar/100000,no_clip")
        values.extend(
            f"{player_prefix}.riichi[{state}]:one_hot"
            for state in _EXPECTED_RIICHI_LABELS
        )
        for slot in range(4):
            prefix = f"{player_prefix}.melds[{slot}]"
            values.append(f"{prefix}.present:binary")
            values.extend(
                f"{prefix}.kind[{kind}]:one_hot" for kind in _EXPECTED_MELD_LABELS
            )
            values.extend(
                f"{prefix}.tile_counts[{tile}]:count/4"
                for tile in _EXPECTED_TILE_LABELS
            )
            values.append(f"{prefix}.from_seat_present:binary")
            values.extend(
                f"{prefix}.from_seat[{source}]:one_hot"
                for source in _EXPECTED_SEAT_LABELS
            )
            values.append(f"{prefix}.called_tile_present:binary")
            values.extend(_expected_tile_one_hot(f"{prefix}.called_tile"))
    for slot in range(136):
        prefix = f"discards_by_global_order[{slot}]"
        values.append(f"{prefix}.present:binary")
        values.extend(
            f"{prefix}.discarder[{seat}]:one_hot" for seat in _EXPECTED_SEAT_LABELS
        )
        values.extend(_expected_tile_one_hot(prefix))
        values.append(f"{prefix}.tsumogiri:binary")
        values.append(f"{prefix}.called_by_present:binary")
        values.extend(
            f"{prefix}.called_by[{seat}]:one_hot" for seat in _EXPECTED_SEAT_LABELS
        )
    values.extend(
        f"own_hand.tile_counts[{tile}]:count/4" for tile in _EXPECTED_TILE_LABELS
    )
    values.append("own_hand.drawn_tile_present:binary")
    values.extend(
        f"own_hand.drawn_tile.tile[{tile}]:one_hot" for tile in _EXPECTED_TILE_LABELS
    )
    return tuple(values)


class LearnedPolicyInputLayoutTest(unittest.TestCase):
    def test_version_dimension_groups_and_fingerprint_are_locked(self):
        self.assertEqual(FEATURE_SEMANTICS_ID, "arena-policy-input-feature-v1")
        self.assertEqual(TENSOR_SCHEMA_VERSION, "arena-policy-input-tensor-v1")
        self.assertEqual(TENSOR_DTYPE, "float32")
        self.assertEqual(FEATURE_DIM, 8204)
        self.assertEqual(
            tuple(
                (value.name, value.start, value.stop, value.logical_shape)
                for value in FEATURE_GROUPS
            ),
            (
                ("round_wind", 0, 4, (4,)),
                ("hand_number", 4, 8, (4,)),
                ("dealer_relative_seat", 8, 12, (4,)),
                ("self_wind", 12, 16, (4,)),
                ("honba", 16, 17, (1,)),
                ("riichi_sticks", 17, 18, (1,)),
                ("live_wall_tiles_remaining", 18, 19, (1,)),
                ("dora_indicators", 19, 209, (5, 38)),
                ("players", 209, 1601, (4, 348)),
                ("discards", 1601, 8129, (136, 48)),
                ("own_hand", 8129, 8204, (75,)),
            ),
        )
        expected_descriptors = _expected_index_descriptors()
        self.assertEqual(FEATURE_INDEX_DESCRIPTORS, expected_descriptors)
        payload = (
            "feature_semantics_id=arena-policy-input-feature-v1\n"
            "tensor_schema_version=arena-policy-input-tensor-v1\n"
            "dtype=float32\n"
            "feature_dim=8204\n"
            + "".join(
                f"{index}:{descriptor}\n"
                for index, descriptor in enumerate(expected_descriptors)
            )
        )
        independent_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(
            independent_hash,
            _SCHEMA_FINGERPRINTS[TENSOR_SCHEMA_VERSION],
        )
        self.assertEqual(schema_fingerprint(), independent_hash)

    def test_every_index_has_one_unique_descriptor_and_literal_anchors(self):
        self.assertEqual(len(FEATURE_INDEX_DESCRIPTORS), FEATURE_DIM)
        self.assertEqual(len(set(FEATURE_INDEX_DESCRIPTORS)), FEATURE_DIM)
        self.assertEqual(FEATURE_INDEX_DESCRIPTORS[0], "round.round_wind[east]:one_hot")
        self.assertEqual(
            FEATURE_INDEX_DESCRIPTORS[18],
            "round.live_wall_tiles_remaining:scalar/84,domain=0..84",
        )
        self.assertEqual(
            FEATURE_INDEX_DESCRIPTORS[19],
            "round.dora_indicators[0].present:binary",
        )
        self.assertEqual(
            FEATURE_INDEX_DESCRIPTORS[209],
            "players[self].score:scalar/100000,no_clip",
        )
        self.assertEqual(
            FEATURE_INDEX_DESCRIPTORS[1601],
            "discards_by_global_order[0].present:binary",
        )
        self.assertEqual(
            FEATURE_INDEX_DESCRIPTORS[8129],
            "own_hand.tile_counts[1m]:count/4",
        )
        self.assertEqual(
            TILE_AXIS_LABELS,
            _EXPECTED_TILE_LABELS,
        )

    def test_unsupported_versions_fail_before_input_validation(self):
        with self.assertRaises(UnsupportedFeatureSemanticsError):
            build_policy_input_feature(object(), version="future")
        with self.assertRaises(UnsupportedTensorSchemaVersionError):
            tensor_values(object(), version="future")
        with self.assertRaises(UnsupportedTensorSchemaVersionError):
            schema_fingerprint(version="future")


class LearnedPolicyInputFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy_input = complex_policy_input()
        self.feature = build_policy_input_feature(self.policy_input)
        self.values = tensor_values(self.feature)

    def test_full_policy_input_fields_and_tensor_values_are_represented(self):
        self.assertIs(self.feature.round_wind, Wind.WEST)
        self.assertEqual(self.feature.hand_number, 4)
        self.assertIs(self.feature.dealer_relative_seat, RelativeSeat.SHIMOCHA)
        self.assertIs(self.feature.self_wind, Wind.NORTH)
        self.assertEqual(self.feature.honba, 3)
        self.assertEqual(self.feature.riichi_sticks, 2)
        self.assertEqual(self.feature.live_wall_tiles_remaining, 42)
        self.assertEqual(
            self.feature.dora_indicators[:2], self.policy_input.round.dora_indicators
        )
        self.assertEqual(self.feature.dora_indicators[2:], (None, None, None))
        self.assertEqual(
            tuple(value.score for value in self.feature.players),
            (-1_000, 31_000, 40_000, 29_000),
        )
        self.assertEqual(
            tuple(value.riichi for value in self.feature.players),
            (
                RiichiState.NONE,
                RiichiState.DECLARED,
                RiichiState.ACCEPTED,
                RiichiState.NONE,
            ),
        )
        self.assertEqual(
            tuple(meld.kind for meld in self.feature.players[0].melds),
            (MeldKind.CHI, MeldKind.PON, MeldKind.DAIMINKAN, MeldKind.ANKAN),
        )
        self.assertIs(self.feature.players[1].melds[0].kind, MeldKind.KAKAN)
        self.assertEqual(
            tuple(value.discarder for value in self.feature.discards[:4]),
            tuple(RelativeSeat),
        )
        self.assertIs(self.feature.discards[1].called_by, RelativeSeat.TOIMEN)
        self.assertEqual(self.feature.discards[4:], (None,) * (MAX_GLOBAL_DISCARDS - 4))
        red_five_index = TILE_AXIS_LABELS.index("5m-red")
        self.assertEqual(self.feature.own_tile_counts[red_five_index], 1)
        self.assertTrue(self.feature.drawn_tile.is_red)

        self.assertEqual(len(self.values), FEATURE_DIM)
        self.assertTrue(all(math.isfinite(value) for value in self.values))
        self.assertEqual(self.values[2], 1.0)  # west round wind
        self.assertEqual(self.values[7], 1.0)  # hand number 4
        self.assertEqual(self.values[9], 1.0)  # dealer is shimocha
        self.assertEqual(self.values[15], 1.0)  # self wind is north
        self.assertEqual(self.values[16:19], (0.3, 0.2, 0.5))
        self.assertEqual(self.values[209], -0.01)
        self.assertEqual(self.values[1601], 1.0)
        for descriptor, value in zip(
            FEATURE_INDEX_DESCRIPTORS, self.values, strict=True
        ):
            if descriptor.endswith((":binary", ":one_hot")):
                self.assertIn(value, (0.0, 1.0), descriptor)
            if descriptor.endswith(":count/4"):
                self.assertGreaterEqual(value, 0.0, descriptor)
                self.assertLessEqual(value, 1.0, descriptor)

    def test_same_input_is_deterministic_and_absolute_seat_rotation_is_invariant(self):
        self.assertEqual(
            build_policy_input_feature(self.policy_input),
            build_policy_input_feature(self.policy_input),
        )
        self.assertEqual(
            self.values, tensor_values(build_policy_input_feature(self.policy_input))
        )
        for offset in (1, 2, 3):
            rotated = rotate_policy_input(self.policy_input, offset)
            self.assertEqual(build_policy_input_feature(rotated), self.feature)
            self.assertEqual(
                tensor_values(build_policy_input_feature(rotated)), self.values
            )

    def test_unordered_multisets_are_invariant_but_sequences_keep_order(self):
        reversed_hand = replace(
            self.policy_input,
            own_hand=OwnHandState(
                tuple(reversed(self.policy_input.own_hand.concealed_tiles)),
                self.policy_input.own_hand.drawn_tile,
            ),
        )
        first_meld = self.policy_input.players[0].melds[0]
        reversed_meld = PublicMeld(
            first_meld.kind,
            tuple(reversed(first_meld.tiles)),
            first_meld.from_seat,
            first_meld.called_tile,
        )
        reversed_hand_and_meld = replace(
            reversed_hand,
            players=(
                replace(
                    reversed_hand.players[0],
                    melds=(reversed_meld,) + reversed_hand.players[0].melds[1:],
                ),
                *reversed_hand.players[1:],
            ),
        )
        self.assertEqual(
            build_policy_input_feature(reversed_hand_and_meld),
            self.feature,
        )

        changed_dora = replace(
            self.policy_input,
            round=replace(
                self.policy_input.round,
                dora_indicators=tuple(
                    reversed(self.policy_input.round.dora_indicators)
                ),
            ),
        )
        self.assertNotEqual(build_policy_input_feature(changed_dora), self.feature)

        first, second = (
            self.policy_input.players[0].discards[0],
            self.policy_input.players[1].discards[0],
        )
        reordered = replace(
            self.policy_input,
            players=(
                replace(
                    self.policy_input.players[0], discards=(replace(first, order=1),)
                ),
                replace(
                    self.policy_input.players[1], discards=(replace(second, order=0),)
                ),
                *self.policy_input.players[2:],
            ),
        )
        self.assertNotEqual(build_policy_input_feature(reordered), self.feature)

    def test_drawn_tile_presence_red_five_and_padding_are_distinct(self):
        target = manzu(5, red=True)
        present = minimal_policy_input(own_tiles=(target,), drawn_tile=target)
        absent = replace(present, own_hand=OwnHandState((target,), None))
        normal = minimal_policy_input(own_tiles=(manzu(5),), drawn_tile=manzu(5))
        present_values = tensor_values(build_policy_input_feature(present))
        absent_values = tensor_values(build_policy_input_feature(absent))
        normal_values = tensor_values(build_policy_input_feature(normal))
        drawn_present_index = 8129 + 37
        self.assertEqual(present_values[drawn_present_index], 1.0)
        self.assertEqual(absent_values[drawn_present_index], 0.0)
        self.assertNotEqual(present_values, absent_values)
        self.assertNotEqual(present_values, normal_values)

    def test_public_api_needs_only_policy_input_and_base_import_is_torch_free(self):
        self.assertEqual(
            tuple(inspect.signature(build_policy_input_feature).parameters),
            ("policy_input", "version"),
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import lisjong_arena.learned_policy_input; "
                    "assert 'torch' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_existing_handbelief_schema_identity_is_unchanged(self):
        self.assertEqual(PHASE6_FEATURE_SEMANTICS_ID, "phase6-history-snapshot-v1")
        self.assertEqual(PHASE6_FEATURE_DIM, 919)

    def test_first_party_engine_projection_satisfies_the_exact_input_boundary(self):
        policy_input = build_engine_policy_input(engine_observation())
        feature = build_policy_input_feature(policy_input)
        self.assertEqual(len(tensor_values(feature)), FEATURE_DIM)


class LearnedPolicyInputBoundaryTest(unittest.TestCase):
    def test_unbounded_counter_and_score_scales_do_not_clip(self):
        base = minimal_policy_input()
        value = replace(
            base,
            round=replace(base.round, honba=11, riichi_sticks=12),
            players=(replace(base.players[0], score=125_000), *base.players[1:]),
        )
        values = tensor_values(build_policy_input_feature(value))
        self.assertEqual(values[16:18], (1.1, 1.2))
        self.assertEqual(values[209], 1.25)

    def test_all_fixed_physical_boundaries_are_accepted(self):
        dora = tuple(manzu((index % 9) + 1) for index in range(MAX_DORA_INDICATORS))
        own_tiles = (
            manzu(1),
            manzu(1),
            manzu(1),
            manzu(1),
            manzu(2),
            manzu(2),
            manzu(2),
            manzu(2),
            pinzu(1),
            pinzu(2),
            pinzu(3),
            pinzu(4),
            honor(1),
            honor(2),
        )
        value = minimal_policy_input(
            players=discard_population(MAX_GLOBAL_DISCARDS),
            own_tiles=own_tiles,
            dora_indicators=dora,
            live_wall_tiles_remaining=MAX_LIVE_WALL_TILES,
        )
        feature = build_policy_input_feature(value)
        self.assertIsNotNone(feature.discards[-1])
        self.assertEqual(len(feature.dora_indicators), MAX_DORA_INDICATORS)
        self.assertEqual(sum(feature.own_tile_counts), MAX_CONCEALED_TILES)

    def test_variable_length_fields_over_the_physical_bounds_fail_closed(self):
        six_dora = tuple(
            manzu((index % 9) + 1) for index in range(MAX_DORA_INDICATORS + 1)
        )
        with self.assertRaisesRegex(
            PolicyInputFeatureValidationError, "dora_indicators length"
        ):
            build_policy_input_feature(minimal_policy_input(dora_indicators=six_dora))

        base = minimal_policy_input()
        repeated_pon = PublicMeld(
            MeldKind.PON,
            (manzu(1), manzu(1), manzu(1)),
            Seat.SEAT_1,
            manzu(1),
        )
        five_melds = replace(
            base,
            players=(
                player(melds=(repeated_pon,) * (MAX_MELDS_PER_PLAYER + 1)),
                *base.players[1:],
            ),
        )
        with self.assertRaisesRegex(PolicyInputFeatureValidationError, "melds length"):
            build_policy_input_feature(five_melds)

        fifteen_tiles = tuple(
            manzu((index % 9) + 1) for index in range(MAX_CONCEALED_TILES + 1)
        )
        with self.assertRaisesRegex(
            PolicyInputFeatureValidationError, "concealed_tiles length"
        ):
            build_policy_input_feature(minimal_policy_input(own_tiles=fifteen_tiles))

        with self.assertRaisesRegex(
            PolicyInputFeatureValidationError, "global discards length"
        ):
            build_policy_input_feature(
                minimal_policy_input(
                    players=discard_population(MAX_GLOBAL_DISCARDS + 1)
                )
            )

    def test_wrong_types_subclasses_order_gaps_and_numeric_domain_fail_closed(self):
        with self.assertRaises(TypeError):
            build_policy_input_feature(object())

        class PolicyInputSubclass(PolicyInput):
            pass

        base = minimal_policy_input()
        subclass = PolicyInputSubclass(
            base.self_seat,
            base.round,
            base.players,
            base.own_hand,
        )
        with self.assertRaises(TypeError):
            build_policy_input_feature(subclass)

        class PlayerSubclass(PlayerPublicState):
            pass

        player_subclass = PlayerSubclass(25_000, (), (), RiichiState.NONE)
        bad_component = replace(base, players=(player_subclass, *base.players[1:]))
        with self.assertRaisesRegex(
            PolicyInputFeatureValidationError, "exact PlayerPublicState"
        ):
            build_policy_input_feature(bad_component)

        gap = minimal_policy_input(players=discard_population(2))
        first_player = gap.players[0]
        gap = replace(
            gap,
            players=(
                replace(
                    first_player,
                    discards=(replace(first_player.discards[0], order=2),),
                ),
                *gap.players[1:],
            ),
        )
        with self.assertRaisesRegex(PolicyInputFeatureValidationError, "contiguous"):
            build_policy_input_feature(gap)

        too_many_live = replace(
            base,
            round=replace(
                base.round,
                live_wall_tiles_remaining=MAX_LIVE_WALL_TILES + 1,
            ),
        )
        with self.assertRaisesRegex(PolicyInputFeatureValidationError, "live_wall"):
            build_policy_input_feature(too_many_live)

        huge_score = replace(
            base,
            players=(replace(base.players[0], score=10**1_000), *base.players[1:]),
        )
        with self.assertRaisesRegex(PolicyInputFeatureValidationError, "finite"):
            tensor_values(build_policy_input_feature(huge_score))

    def test_runtime_dimension_assertion_is_fail_closed(self):
        feature = build_policy_input_feature(minimal_policy_input())
        with patch.object(tensor_module, "FEATURE_DIM", FEATURE_DIM + 1):
            with self.assertRaisesRegex(FeatureDimensionError, "dimension drifted"):
                tensor_values(feature)


if __name__ == "__main__":
    unittest.main()
