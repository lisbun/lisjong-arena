"""P1 keep-shanten derived representation and Gate A ladder tests (Issue #158).

torchを必要としない境界だけをここで固定する。

- 37-tile keep-shanten featureのsemantics（存在 / keep / worsen / duplicate /
  赤5と通常5のidentity分離 / hidden・future情報を読まない構造 / fail closed）
- derived schema identity / dimension / descriptor order / fingerprint
- locked v1 contractがbreaking changeされていないこと
- Q protocolの差分がinput dimensionだけであること
- Gate A result documentのvalidationと、事前lockされたexhaustive ladder
"""

import unittest

from _learned_policy_offline_q_diagnosis_fixtures import (
    feature_row_with_hand,
    hand_tiles,
    tile,
)
from _learned_policy_offline_q_p1_gate_a_fixtures import (
    ELIGIBLE_ROW_COUNT,
    NEUTRAL_PROGRESSION,
    REGRESSION_PROGRESSION,
    SIGNAL_PROGRESSION,
    hand_progression_block,
    unavailable_hand_progression,
    valid_result_document,
)
from lisjong.policy_contract import Tile, TileCategory, TileType

from lisjong_arena.learned_policy_input.feature import (
    FEATURE_SEMANTICS_ID,
    TILE_AXIS,
    TILE_INDEX,
)
from lisjong_arena.learned_policy_input.tensor import (
    FEATURE_DIM,
    FEATURE_INDEX_DESCRIPTORS,
    TENSOR_DTYPE,
    TENSOR_SCHEMA_VERSION,
    TILE_AXIS_LABELS,
    schema_fingerprint,
)
from lisjong_arena.learned_policy_offline_q.artifact import feature_block
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQAmbiguousStateError,
    OfflineQDiagnosisError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    OWN_HAND_TILE_COUNT_START,
    keep_shanten_tile_mask,
    post_discard_tiles,
    reconstruct_concealed_tiles,
)
from lisjong_arena.learned_policy_offline_q.p1_features import (
    KEEP_SHANTEN_DIMENSION,
    KEEP_SHANTEN_INDEX_DESCRIPTORS,
    LOCKED_P1_SCHEMA_FINGERPRINT,
    P1_FEATURE_DIMENSION,
    P1_FEATURE_SEMANTICS_ID,
    P1_INDEX_DESCRIPTORS,
    P1_TENSOR_DTYPE,
    P1_TENSOR_SCHEMA_VERSION,
    derive_p1_row,
    p1_feature_block,
    p1_schema_fingerprint,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_a import (
    ARMS,
    P1GateAOutcome,
    P1GateARole,
    derive_classification,
    record_classification,
    validate_gate_a_result,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_a import (
    hand_progression_block as hand_progression_measurement,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    BASELINE_PARAMETER_COUNT,
    P1_EXPECTED_PARAMETER_COUNT,
    PARAMETER_COUNT_DELTA,
    p1_model_block,
    p1_training_block,
    verify_locked_q_protocol_delta,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    VOCABULARY_SIZE,
)
from lisjong_arena.learned_policy_offline_q.q_training import (
    locked_model_block,
    locked_training_block,
)

LOCKED_V1_FINGERPRINT = (
    "097dd99fa2956c0c7c2e298399e6b71326753a2d810835f1f2fef224abd0ed30"
)
"""Issue #158 preflightでreadbackした現行v1 fingerprint。"""

TENPAI_HAND = "123456789m123p1s5z"
"""4面子 + 単騎候補2枚（`1s` / `5z`）。pre-discard shantenは0。"""

DUPLICATE_HAND = "11234567899m123p"
"""`1m` / `9m`を2枚ずつ持つ手牌。1枚だけ切ると向聴数が変わらない。"""

RED_FIVE_HAND = "123789m123789p1s"
"""赤5mを足して14枚にする土台（通常5mは1枚も含まない）。"""

RED_FIVE_MANZU = Tile(TileType(TileCategory.MANZU, 5), is_red=True)
NORMAL_FIVE_MANZU = Tile(TileType(TileCategory.MANZU, 5))


class KeepShantenFeatureTest(unittest.TestCase):
    """37-tile keep-shanten featureのsemanticsを固定する。"""

    def mask(self, concealed, **kwargs):
        return keep_shanten_tile_mask(feature_row_with_hand(concealed, **kwargs))

    def test_the_mask_has_exactly_thirty_seven_entries(self):
        mask = self.mask(hand_tiles(TENPAI_HAND))
        self.assertEqual(len(mask), 37)
        self.assertEqual(len(mask), KEEP_SHANTEN_DIMENSION)

    def test_the_mask_follows_the_canonical_tile_axis_order(self):
        mask = self.mask(hand_tiles(TENPAI_HAND))
        for index, axis_tile in enumerate(TILE_AXIS):
            self.assertEqual(TILE_INDEX[axis_tile], index)
        self.assertEqual(mask[TILE_INDEX[tile("1s")]], 1.0)
        self.assertEqual(mask[TILE_INDEX[tile("5z")]], 1.0)

    def test_only_floats_zero_and_one_are_produced(self):
        self.assertEqual(set(self.mask(hand_tiles(TENPAI_HAND))), {0.0, 1.0})

    def test_a_tile_absent_from_the_hand_is_zero(self):
        mask = self.mask(hand_tiles(TENPAI_HAND))
        self.assertEqual(mask[TILE_INDEX[tile("7z")]], 0.0)
        self.assertEqual(mask[TILE_INDEX[tile("9s")]], 0.0)

    def test_a_keep_shanten_discard_is_one(self):
        mask = self.mask(hand_tiles(TENPAI_HAND))
        self.assertEqual(mask[TILE_INDEX[tile("1s")]], 1.0)

    def test_a_worsening_discard_is_zero(self):
        mask = self.mask(hand_tiles(TENPAI_HAND))
        self.assertEqual(mask[TILE_INDEX[tile("9m")]], 0.0)
        self.assertEqual(mask[TILE_INDEX[tile("1m")]], 0.0)

    def test_a_duplicated_tile_removes_exactly_one_copy(self):
        concealed = hand_tiles(DUPLICATE_HAND)
        remaining = post_discard_tiles(concealed, tile("1m"))
        self.assertEqual(len(remaining), len(concealed) - 1)
        self.assertEqual(remaining.count(tile("1m")), concealed.count(tile("1m")) - 1)
        # 2枚とも外していれば12枚になり、その手牌枚数はfail closedされる。
        self.assertEqual(self.mask(concealed)[TILE_INDEX[tile("1m")]], 1.0)

    def test_red_and_normal_five_keep_separate_identities(self):
        concealed = (*hand_tiles(RED_FIVE_HAND), RED_FIVE_MANZU)
        mask = self.mask(concealed)
        self.assertEqual(mask[TILE_INDEX[RED_FIVE_MANZU]], 1.0)
        self.assertEqual(mask[TILE_INDEX[NORMAL_FIVE_MANZU]], 0.0)
        self.assertNotEqual(TILE_INDEX[RED_FIVE_MANZU], TILE_INDEX[NORMAL_FIVE_MANZU])

    def test_the_mask_ignores_every_index_outside_the_own_hand_group(self):
        """hidden / future情報を読まない構造。own_hand以外を変えても不変。"""
        concealed = hand_tiles(TENPAI_HAND)
        baseline = self.mask(concealed)
        for filler in (0.25, -1.0, 7.5):
            self.assertEqual(self.mask(concealed, filler=filler), baseline)

    def test_the_mask_takes_no_legal_mask_argument(self):
        """derivationはlegal maskを入力に取らない（signatureで固定する）。"""
        from inspect import signature

        self.assertEqual(
            list(signature(keep_shanten_tile_mask).parameters), ["feature_values"]
        )

    def test_a_non_integer_own_hand_count_fails_closed(self):
        values = feature_row_with_hand(hand_tiles(TENPAI_HAND))
        values[OWN_HAND_TILE_COUNT_START] = 0.3
        with self.assertRaises(OfflineQAmbiguousStateError):
            keep_shanten_tile_mask(values)

    def test_an_impossible_hand_size_fails_closed(self):
        values = feature_row_with_hand(hand_tiles(TENPAI_HAND))
        values[OWN_HAND_TILE_COUNT_START + TILE_INDEX[tile("7z")]] = 1 / 4.0
        with self.assertRaises(OfflineQAmbiguousStateError):
            keep_shanten_tile_mask(values)

    def test_an_all_zero_row_fails_closed(self):
        with self.assertRaises(OfflineQAmbiguousStateError):
            keep_shanten_tile_mask([0.0] * FEATURE_DIMENSION)

    def test_the_reconstruction_is_the_shared_player_safe_derivation(self):
        concealed = hand_tiles(TENPAI_HAND)
        self.assertEqual(
            sorted(
                reconstruct_concealed_tiles(feature_row_with_hand(concealed)),
                key=lambda item: TILE_INDEX[item],
            ),
            sorted(concealed, key=lambda item: TILE_INDEX[item]),
        )


class DerivedRowTest(unittest.TestCase):
    """8204 -> 8241 row変換の構造を固定する。"""

    def test_a_derived_row_is_the_base_row_plus_the_mask(self):
        values = feature_row_with_hand(hand_tiles(TENPAI_HAND))
        derived = derive_p1_row(values)
        self.assertEqual(len(derived), P1_FEATURE_DIMENSION)
        self.assertEqual(list(derived[:FEATURE_DIMENSION]), values)
        self.assertEqual(derived[FEATURE_DIMENSION:], keep_shanten_tile_mask(values))

    def test_an_empty_role_is_unavailable_rather_than_a_fabricated_rate(self):
        block = hand_progression_measurement([], (), {arm: [] for arm in ARMS})
        self.assertEqual(block["status"], "UNAVAILABLE")
        self.assertTrue(block["unavailable_reason"])
        for name in ("arms", "pairs", "per_seed", "outcome_conditions"):
            self.assertIsNone(block[name])

    def test_a_wrong_width_row_is_rejected(self):
        with self.assertRaises(OfflineQProtocolError):
            derive_p1_row([0.0] * (FEATURE_DIMENSION - 1))

    def test_an_ambiguous_row_fails_closed_instead_of_being_imputed(self):
        with self.assertRaises(OfflineQAmbiguousStateError):
            derive_p1_row([0.0] * FEATURE_DIMENSION)


class DerivedSchemaIdentityTest(unittest.TestCase):
    """derived schema identityとfingerprintをlocked valueへ固定する。"""

    def test_the_locked_dimensions(self):
        self.assertEqual(KEEP_SHANTEN_DIMENSION, 37)
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)
        self.assertEqual(P1_FEATURE_DIMENSION, FEATURE_DIMENSION + 37)

    def test_the_locked_identity_strings(self):
        self.assertEqual(
            P1_FEATURE_SEMANTICS_ID,
            "arena-learned-policy-offlineq-p1-keep-shanten-feature-v1",
        )
        self.assertEqual(
            P1_TENSOR_SCHEMA_VERSION,
            "arena-learned-policy-offlineq-p1-keep-shanten-tensor-v1",
        )
        self.assertEqual(P1_TENSOR_DTYPE, "float32")

    def test_the_base_descriptors_are_kept_verbatim(self):
        self.assertEqual(
            P1_INDEX_DESCRIPTORS[:FEATURE_DIMENSION], FEATURE_INDEX_DESCRIPTORS
        )

    def test_the_appended_descriptors_follow_the_canonical_tile_axis(self):
        self.assertEqual(len(KEEP_SHANTEN_INDEX_DESCRIPTORS), 37)
        self.assertEqual(
            KEEP_SHANTEN_INDEX_DESCRIPTORS,
            tuple(
                f"p1_keep_shanten_discard.tile[{label}]:binary"
                for label in TILE_AXIS_LABELS
            ),
        )
        self.assertEqual(
            KEEP_SHANTEN_INDEX_DESCRIPTORS[0],
            "p1_keep_shanten_discard.tile[1m]:binary",
        )
        self.assertEqual(
            KEEP_SHANTEN_INDEX_DESCRIPTORS[-1],
            "p1_keep_shanten_discard.tile[5s-red]:binary",
        )

    def test_every_descriptor_is_unique(self):
        self.assertEqual(len(set(P1_INDEX_DESCRIPTORS)), P1_FEATURE_DIMENSION)

    def test_the_fingerprint_is_the_preflight_locked_value(self):
        self.assertEqual(
            LOCKED_P1_SCHEMA_FINGERPRINT,
            "beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409",
        )
        self.assertEqual(p1_schema_fingerprint(), LOCKED_P1_SCHEMA_FINGERPRINT)

    def test_the_derived_fingerprint_is_not_the_v1_fingerprint(self):
        self.assertNotEqual(LOCKED_P1_SCHEMA_FINGERPRINT, schema_fingerprint())

    def test_the_feature_block_carries_the_base_identity(self):
        block = p1_feature_block()
        self.assertEqual(block["dimension"], 8241)
        self.assertEqual(block["appended_dimension"], 37)
        self.assertEqual(block["schema_fingerprint"], LOCKED_P1_SCHEMA_FINGERPRINT)
        self.assertEqual(block["base_feature"], feature_block())
        self.assertEqual(block["terminal_next_state_padding"], "all-zero")


class LockedV1ContractTest(unittest.TestCase):
    """v1がbreaking changeされていないことを固定する。"""

    def test_the_v1_semantics_and_dimension_are_unchanged(self):
        self.assertEqual(FEATURE_SEMANTICS_ID, "arena-policy-input-feature-v1")
        self.assertEqual(TENSOR_SCHEMA_VERSION, "arena-policy-input-tensor-v1")
        self.assertEqual(TENSOR_DTYPE, "float32")
        self.assertEqual(FEATURE_DIM, 8204)
        self.assertEqual(FEATURE_DIMENSION, 8204)

    def test_the_v1_fingerprint_is_unchanged(self):
        self.assertEqual(schema_fingerprint(), LOCKED_V1_FINGERPRINT)
        self.assertEqual(LOCKED_FEATURE_SCHEMA_FINGERPRINT, LOCKED_V1_FINGERPRINT)
        self.assertEqual(feature_block()["schema_fingerprint"], LOCKED_V1_FINGERPRINT)

    def test_the_v1_descriptor_count_is_unchanged(self):
        self.assertEqual(len(FEATURE_INDEX_DESCRIPTORS), 8204)


class QProtocolDeltaTest(unittest.TestCase):
    """Q protocolの差分がinput dimensionだけであることを固定する。"""

    def test_the_locked_protocol_delta_passes(self):
        verify_locked_q_protocol_delta()

    def test_the_training_semantics_are_literally_the_locked_ones(self):
        self.assertEqual(p1_training_block(), locked_training_block())

    def test_checkpoint_selection_stays_fixed_final_iteration(self):
        self.assertEqual(
            p1_training_block()["checkpoint_selection"], "fixed_final_iteration"
        )

    def test_only_the_input_dimension_differs_in_the_model_block(self):
        baseline = locked_model_block()
        candidate = p1_model_block()
        for name in ("hidden_layers", "hidden_width", "activation", "output_dimension"):
            self.assertEqual(candidate[name], baseline[name])
        self.assertIsNone(candidate["dropout"])
        self.assertIsNone(candidate["normalization_layer"])
        self.assertEqual(baseline["input_dimension"], 8204)
        self.assertEqual(candidate["input_dimension"], 8241)
        self.assertEqual(candidate["hidden_width"], 128)
        self.assertEqual(candidate["activation"], "relu")

    def test_the_parameter_counts_are_recomputed_independently(self):
        def count(inputs: int) -> int:
            return (
                inputs * HIDDEN_WIDTH
                + HIDDEN_WIDTH
                + HIDDEN_WIDTH * VOCABULARY_SIZE
                + VOCABULARY_SIZE
            )

        self.assertEqual(count(8204), 1_153_698)
        self.assertEqual(count(8241), 1_158_434)
        self.assertEqual(BASELINE_PARAMETER_COUNT, 1_153_698)
        self.assertEqual(P1_EXPECTED_PARAMETER_COUNT, 1_158_434)
        self.assertEqual(PARAMETER_COUNT_DELTA, 4_736)
        self.assertEqual(PARAMETER_COUNT_DELTA, 37 * 128)


class GateAResultValidationTest(unittest.TestCase):
    """Gate A result documentのvalidationをfail closedで固定する。"""

    def test_the_fixture_document_is_valid(self):
        validate_gate_a_result(valid_result_document())

    def test_a_missing_field_is_rejected(self):
        document = valid_result_document()
        del document["derived_feature"]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_an_extra_field_is_rejected(self):
        document = valid_result_document()
        document["extra"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_tampered_derived_feature_identity_is_rejected(self):
        document = valid_result_document()
        document["derived_feature"] = {
            **document["derived_feature"],
            "schema_fingerprint": "0" * 64,
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_changed_axis_beyond_the_locked_one_is_rejected(self):
        document = valid_result_document()
        document["changed_axis"] = [*document["changed_axis"], "reward redesign"]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_non_zero_generation_budget_is_rejected(self):
        document = valid_result_document()
        document["generation_budget"] = {
            **document["generation_budget"],
            "new_hanchan": 4,
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_missing_role_is_rejected(self):
        document = valid_result_document()
        document["roles"] = document["roles"][:3]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_role_claiming_generalization_evidence_is_rejected(self):
        document = valid_result_document()
        document["roles"][2]["is_generalization_evidence"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_mislabelled_primary_role_is_rejected(self):
        document = valid_result_document()
        document["roles"][0]["is_primary_role"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_disagreement_rate_that_is_not_derivable_is_rejected(self):
        document = valid_result_document()
        document["roles"][2]["action_agreement"]["q_v2_vs_q_v1_disagreement_rate"] = 0.9
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_worsening_rate_that_is_not_derivable_is_rejected(self):
        document = valid_result_document()
        arms = document["roles"][2]["hand_progression"]["arms"]
        arms["q_v2"]["worsen_shanten_rate"] = 0.0
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_pair_difference_that_is_not_derivable_is_rejected(self):
        document = valid_result_document()
        pairs = document["roles"][2]["hand_progression"]["pairs"]
        pairs["q_v2_vs_bc"]["worsen_shanten_rate_difference"] = -1.0
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_tampered_outcome_conditions_are_rejected(self):
        document = valid_result_document()
        conditions = document["roles"][2]["hand_progression"]["outcome_conditions"]
        conditions["signal_conditions"]["bc_worsen_rate_gap_narrowed"] = False
        conditions["signal"] = False
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_an_imputed_row_is_rejected(self):
        document = valid_result_document()
        document["roles"][2]["derived_coverage"]["imputed_row_count"] = 1
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_incomplete_derived_coverage_is_rejected(self):
        document = valid_result_document()
        document["roles"][2]["derived_coverage"]["source_rows_derived"] = 1
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_self_declared_real_execution_flag_is_rejected(self):
        document = valid_result_document(real_artifact_execution=False)
        document["input_artifact_identities"]["real_artifact_execution"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_candidate_support_set_that_is_not_the_retained_one_is_rejected(self):
        document = valid_result_document()
        document["candidate"]["supported_indices_digest"] = "1" * 64
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_non_final_checkpoint_selection_is_rejected(self):
        document = valid_result_document()
        document["candidate"]["selected_epoch"] = 3
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_tampered_candidate_model_block_is_rejected(self):
        document = valid_result_document()
        document["candidate"]["model"] = {
            **document["candidate"]["model"],
            "hidden_width": 64,
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_a_tampered_candidate_training_block_is_rejected(self):
        document = valid_result_document()
        document["candidate"]["training"] = {
            **document["candidate"]["training"],
            "gamma": 0.99,
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)


class GateAClassificationTest(unittest.TestCase):
    """事前lockされたexhaustive ladderを固定する。"""

    def test_the_outcome_enumeration_is_exhaustive_and_locked(self):
        self.assertEqual(
            [outcome.value for outcome in P1GateAOutcome],
            [
                "P1 HAND-PROGRESSION SIGNAL",
                "P1 HAND-PROGRESSION REGRESSION",
                "P1 HAND-PROGRESSION INCONCLUSIVE",
                "P1 EVIDENCE INSUFFICIENT",
                "STOP / INVALID",
            ],
        )

    def test_the_primary_roles_are_the_two_test_populations(self):
        document = valid_result_document()
        self.assertEqual(
            document["primary_roles"], ["dataset-test", "replacement-test"]
        )

    def test_both_primary_roles_positive_is_a_signal(self):
        document = validate_gate_a_result(
            valid_result_document(primary_progression=SIGNAL_PROGRESSION)
        )
        self.assertIs(
            derive_classification(document), P1GateAOutcome.HAND_PROGRESSION_SIGNAL
        )

    def test_both_primary_roles_negative_is_a_regression(self):
        document = validate_gate_a_result(
            valid_result_document(primary_progression=REGRESSION_PROGRESSION)
        )
        self.assertIs(
            derive_classification(document), P1GateAOutcome.HAND_PROGRESSION_REGRESSION
        )

    def test_a_mixed_direction_is_inconclusive(self):
        document = validate_gate_a_result(
            valid_result_document(
                dataset_test_progression=SIGNAL_PROGRESSION,
                replacement_test_progression=REGRESSION_PROGRESSION,
            )
        )
        self.assertIs(
            derive_classification(document),
            P1GateAOutcome.HAND_PROGRESSION_INCONCLUSIVE,
        )

    def test_neither_direction_is_inconclusive(self):
        document = validate_gate_a_result(
            valid_result_document(primary_progression=NEUTRAL_PROGRESSION)
        )
        self.assertIs(
            derive_classification(document),
            P1GateAOutcome.HAND_PROGRESSION_INCONCLUSIVE,
        )

    def test_a_train_only_signal_does_not_reach_the_primary_ladder(self):
        """TRAIN / VALIDATIONはpositive claimの正本にならない。"""
        document = valid_result_document(primary_progression=NEUTRAL_PROGRESSION)
        document["roles"][0]["hand_progression"] = hand_progression_block(
            **SIGNAL_PROGRESSION
        )
        validated = validate_gate_a_result(document)
        self.assertIs(
            derive_classification(validated),
            P1GateAOutcome.HAND_PROGRESSION_INCONCLUSIVE,
        )

    def test_an_unavailable_primary_role_is_evidence_insufficient(self):
        document = valid_result_document()
        document["roles"][3]["hand_progression"] = unavailable_hand_progression()
        validated = validate_gate_a_result(document)
        self.assertIs(
            derive_classification(validated), P1GateAOutcome.EVIDENCE_INSUFFICIENT
        )

    def test_a_zero_row_primary_role_is_evidence_insufficient(self):
        document = valid_result_document()
        counts = document["roles"][2]["row_counts"]
        counts["eligible_row_count"] = 0
        counts["excluded_row_count"] = counts["total_row_count"]
        document["roles"][2]["action_agreement"] = {
            "eligible_row_count": 0,
            **{
                key: (0 if key.endswith("count") else None)
                for key in document["roles"][2]["action_agreement"]
                if key != "eligible_row_count"
            },
        }
        document["roles"][2]["hand_progression"] = unavailable_hand_progression()
        validated = validate_gate_a_result(document)
        self.assertIs(
            derive_classification(validated), P1GateAOutcome.EVIDENCE_INSUFFICIENT
        )


class GateAClassificationRecordingTest(unittest.TestCase):
    """outcome記録の機械的境界を固定する。"""

    def test_the_derived_outcome_is_recorded(self):
        classified = record_classification(
            valid_result_document(primary_progression=SIGNAL_PROGRESSION),
            P1GateAOutcome.HAND_PROGRESSION_SIGNAL,
        )
        self.assertEqual(classified["classification"], "P1 HAND-PROGRESSION SIGNAL")
        validate_gate_a_result(classified)

    def test_an_outcome_the_ladder_does_not_derive_is_rejected(self):
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                valid_result_document(primary_progression=NEUTRAL_PROGRESSION),
                P1GateAOutcome.HAND_PROGRESSION_SIGNAL,
            )

    def test_stop_invalid_is_a_pre_result_state(self):
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(valid_result_document(), P1GateAOutcome.STOP_INVALID)

    def test_a_synthetic_execution_cannot_record_an_outcome(self):
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                valid_result_document(real_artifact_execution=False),
                P1GateAOutcome.HAND_PROGRESSION_SIGNAL,
            )

    def test_an_outcome_cannot_be_overwritten(self):
        classified = record_classification(
            valid_result_document(), P1GateAOutcome.HAND_PROGRESSION_SIGNAL
        )
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                classified, P1GateAOutcome.HAND_PROGRESSION_INCONCLUSIVE
            )

    def test_a_recorded_classification_must_match_the_ladder(self):
        document = valid_result_document(primary_progression=SIGNAL_PROGRESSION)
        document["classification"] = "P1 HAND-PROGRESSION REGRESSION"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_gate_a_result(document)

    def test_evidence_insufficient_can_be_recorded_for_an_incomplete_role(self):
        document = valid_result_document()
        document["roles"][3]["hand_progression"] = unavailable_hand_progression()
        classified = record_classification(
            document, P1GateAOutcome.EVIDENCE_INSUFFICIENT
        )
        self.assertEqual(classified["classification"], "P1 EVIDENCE INSUFFICIENT")

    def test_every_role_is_measured_on_the_same_eligible_rows(self):
        document = validate_gate_a_result(valid_result_document())
        for role in document["roles"]:
            self.assertEqual(
                role["action_agreement"]["eligible_row_count"], ELIGIBLE_ROW_COUNT
            )
        self.assertEqual(
            {role["role"] for role in document["roles"]},
            {item.value for item in P1GateARole},
        )


if __name__ == "__main__":
    unittest.main()
