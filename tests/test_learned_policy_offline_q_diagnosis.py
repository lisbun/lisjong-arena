"""Artifact-only failure diagnosis contract tests (Issue #152).

ここで固定するのはtorchを必要としない境界である。

- Issue #152がlockしたsource artifact identity
- exhaustive outcomeの列挙とfabrication拒否
- result artifact validation（countsからのaggregate再導出）
- Measurement Dのplayer-safe reconstructionと、ambiguous入力の拒否
"""

import unittest
from collections import Counter

from _learned_policy_offline_q_diagnosis_fixtures import (
    available_measurement_d,
    discard_index,
    feature_row_with_hand,
    hand_tiles,
    summary,
    valid_result_document,
)
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q.diagnosis import (
    DECISION_DEPTH_BAND_EDGES,
    DIAGNOSIS_LIMITATIONS,
    FIXED_QUANTILES,
    LOCKED_SOURCE_IDENTITIES,
    RETENTION_KEY,
    DiagnosisOutcome,
    DiagnosisRole,
    ExpectedArtifactIdentities,
    decision_depth_band,
    fixed_summary,
    legal_action_count_bucket,
    record_classification,
    validate_diagnosis_result,
)
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQAmbiguousStateError,
    OfflineQDiagnosisError,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    OWN_HAND_TILE_COUNT_START,
    OWN_HAND_TILE_COUNT_STOP,
    UKEIRE_UNAVAILABLE_REASON,
    MeasurementAvailability,
    discard_tile_for_index,
    hand_progression,
    hand_progression_for_row,
    is_discard_index,
    reconstruct_concealed_tiles,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    action_family,
)

_ISSUE_152_IDENTITIES = {
    "dataset_identity": (
        "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4"
    ),
    "bc_checkpoint_identity": (
        "17a31fc8aa0edcdd3834da7075abe37bd9554d47f4efe94afb31050bad20ac3b"
    ),
    "q_checkpoint_identity": (
        "31545d6bde3da4fd7ee6152bf3183e5be82302d8a5cee70ccf35923781382b94"
    ),
    "replacement_test_artifact_identity": (
        "fe7a4455b775cbc23568b0d9c7489593c0859bce28e0529e3e400a816cf7fccd"
    ),
    "supported_indices_digest": (
        "230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e"
    ),
}


class LockedSourceIdentityTest(unittest.TestCase):
    def test_locked_identities_are_the_issue_values(self):
        self.assertEqual(LOCKED_SOURCE_IDENTITIES.to_document(), _ISSUE_152_IDENTITIES)

    def test_identity_must_be_a_sha256_digest(self):
        with self.assertRaises(OfflineQDiagnosisError):
            ExpectedArtifactIdentities(
                dataset_identity="short",
                bc_checkpoint_identity=_ISSUE_152_IDENTITIES["bc_checkpoint_identity"],
                q_checkpoint_identity=_ISSUE_152_IDENTITIES["q_checkpoint_identity"],
                replacement_test_artifact_identity=_ISSUE_152_IDENTITIES[
                    "replacement_test_artifact_identity"
                ],
                supported_indices_digest=_ISSUE_152_IDENTITIES[
                    "supported_indices_digest"
                ],
            )

    def test_roles_separate_train_from_the_replacement_test(self):
        self.assertEqual(
            {role.value for role in DiagnosisRole},
            {
                "dataset-train",
                "dataset-validation",
                "dataset-test",
                "replacement-test",
            },
        )


class OutcomeLadderTest(unittest.TestCase):
    def test_outcomes_are_the_locked_exhaustive_ladder(self):
        self.assertEqual(
            [outcome.value for outcome in DiagnosisOutcome],
            [
                "HAND-PROGRESSION DEGRADATION IDENTIFIED",
                "Q-RANKING INSTABILITY IDENTIFIED",
                "FAILURE MECHANISM INCONCLUSIVE",
                "DIAGNOSTIC EVIDENCE INSUFFICIENT",
                "STOP / INVALID",
            ],
        )

    def test_classification_requires_a_real_artifact_execution(self):
        document = valid_result_document(real_artifact_execution=False)
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                document, DiagnosisOutcome.FAILURE_MECHANISM_INCONCLUSIVE
            )

    def test_classification_is_recorded_once_on_a_real_execution(self):
        document = valid_result_document()
        classified = record_classification(
            document, DiagnosisOutcome.Q_RANKING_INSTABILITY_IDENTIFIED
        )
        self.assertEqual(
            classified["classification"], "Q-RANKING INSTABILITY IDENTIFIED"
        )
        self.assertIsNone(document["classification"])
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                classified, DiagnosisOutcome.FAILURE_MECHANISM_INCONCLUSIVE
            )

    def test_classification_rejects_a_value_outside_the_ladder(self):
        with self.assertRaises(TypeError):
            record_classification(
                valid_result_document(), "HAND-PROGRESSION DEGRADATION IDENTIFIED"
            )

    def test_validation_rejects_an_unknown_recorded_outcome(self):
        document = valid_result_document()
        document["classification"] = "OFFLINE Q LOOKS FINE"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_hand_progression_outcome_requires_an_available_measurement_d(self):
        """Measurement Dが全roleでUNAVAILABLEなら手牌進行のdiagnosisは名乗れない。"""
        document = valid_result_document()
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                document, DiagnosisOutcome.HAND_PROGRESSION_DEGRADATION_IDENTIFIED
            )
        available = valid_result_document(hand_progression_available=True)
        classified = record_classification(
            available, DiagnosisOutcome.HAND_PROGRESSION_DEGRADATION_IDENTIFIED
        )
        self.assertEqual(
            classified["classification"], "HAND-PROGRESSION DEGRADATION IDENTIFIED"
        )

    def test_a_forged_real_execution_flag_is_rejected(self):
        """locked identityと一致しないのにflagだけTrueのdocumentは通さない。"""
        document = valid_result_document(real_artifact_execution=False)
        document["input_artifact_identities"]["real_artifact_execution"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(
                document, DiagnosisOutcome.FAILURE_MECHANISM_INCONCLUSIVE
            )

    def test_a_tampered_input_identity_is_rejected(self):
        document = valid_result_document()
        document["input_artifact_identities"]["q_checkpoint_identity"] = "0" * 64
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_tampered_locked_identity_block_is_rejected(self):
        document = valid_result_document()
        document["locked_source_identities"]["dataset_identity"] = "1" * 64
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)


class ResultArtifactValidationTest(unittest.TestCase):
    def test_a_well_formed_result_validates(self):
        self.assertIs(
            validate_diagnosis_result(valid_result_document())["classification"], None
        )

    def test_extra_fields_are_rejected(self):
        document = valid_result_document()
        document["conclusion"] = "value objective is wrong"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_disagreement_rate_must_be_derivable_from_its_counts(self):
        document = valid_result_document()
        document["roles"][0]["measurement_a"]["q_vs_bc_disagreement_rate"] = 0.99
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_stratification_must_partition_the_eligible_rows(self):
        document = valid_result_document()
        strata = document["roles"][0]["measurement_a"]["stratifications"]
        strata["legal_action_count"][0]["row_count"] = 3
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_excluded_rows_must_be_derivable_from_the_row_counts(self):
        document = valid_result_document()
        document["roles"][0]["row_counts"]["excluded_row_count"] = 0
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_terminality_counts_must_partition_the_eligible_rows(self):
        document = valid_result_document()
        document["roles"][0]["measurement_c"]["terminal_row_count"] = 2
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_bootstrap_counts_must_partition_the_eligible_rows(self):
        document = valid_result_document()
        document["roles"][0]["measurement_c"]["unsupported_bootstrap_row_count"] = 1
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_unavailable_measurement_d_must_record_a_reason(self):
        document = valid_result_document()
        document["roles"][0]["measurement_d"]["unavailable_reason"] = ""
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_unavailable_measurement_d_must_not_carry_summaries(self):
        document = valid_result_document()
        document["roles"][0]["measurement_d"]["post_discard_shanten"] = {"q": {}}
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_ukeire_must_stay_unavailable(self):
        document = valid_result_document()
        document["roles"][0]["measurement_d"]["ukeire"]["status"] = (
            MeasurementAvailability.AVAILABLE.value
        )
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_candidate_only_metrics_must_not_be_a_baseline_difference(self):
        document = valid_result_document()
        document["retained_strength_context"] = {
            **document["retained_strength_context"],
            "candidate_only_mahjong_metrics": {
                **document["retained_strength_context"][
                    "candidate_only_mahjong_metrics"
                ],
                "is_baseline_difference": True,
            },
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_the_retained_strength_context_must_not_be_recomputed(self):
        document = valid_result_document()
        document["retained_strength_context"] = {
            **document["retained_strength_context"],
            "recomputed": True,
        }
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_limitations_state_the_non_causal_boundary(self):
        joined = " ".join(DIAGNOSIS_LIMITATIONS)
        self.assertIn("ROLLOUT DISTRIBUTION SHIFT", joined)
        self.assertIn("descriptive, not causal", joined)
        self.assertIn("not a difference against", joined)

    def test_a_duplicated_role_is_rejected(self):
        document = valid_result_document()
        document["roles"].append(dict(document["roles"][0]))
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_missing_role_is_rejected(self):
        """4 role全部が揃っていないresultはvalidにしない。"""
        for index in range(4):
            document = valid_result_document()
            removed = document["roles"].pop(index)["role"]
            with self.assertRaises(OfflineQDiagnosisError, msg=removed):
                validate_diagnosis_result(document)

    def test_a_role_must_declare_its_locked_source_and_split(self):
        document = valid_result_document()
        document["roles"][0]["source_artifact"] = "replacement-test"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

        document = valid_result_document()
        document["roles"][0]["split"] = "TEST"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

        document = valid_result_document()
        document["roles"][0]["is_generalization_evidence"] = True
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_an_empty_measurement_b_is_rejected(self):
        document = valid_result_document()
        document["roles"][0]["measurement_b"] = {}
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_measurement_b_metric_must_cover_every_eligible_row(self):
        document = valid_result_document()
        block = document["roles"][0]["measurement_b"]
        block["all_eligible_rows"]["q_margin"] = summary(1)
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_measurement_b_agree_and_disagree_rows_must_partition(self):
        document = valid_result_document()
        block = document["roles"][0]["measurement_b"]
        block["q_bc_disagree_rows"]["q_top1_value"] = summary(2)
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_missing_measurement_b_field_is_rejected(self):
        document = valid_result_document()
        del document["roles"][0]["measurement_b"]["all_eligible_rows"]["q_margin"]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_missing_measurement_c_distribution_is_rejected(self):
        for name in (
            "immediate_reward",
            "td_target",
            "predicted_selected_q",
            "absolute_bellman_residual",
        ):
            document = valid_result_document()
            del document["roles"][0]["measurement_c"][name]
            with self.assertRaises(OfflineQDiagnosisError, msg=name):
                validate_diagnosis_result(document)

    def test_a_measurement_c_distribution_must_cover_its_rows(self):
        document = valid_result_document()
        document["roles"][0]["measurement_c"]["td_target"][
            "all_bootstrap_eligible_rows"
        ] = summary(1)
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_measurement_c_must_declare_the_locked_objective_and_target(self):
        document = valid_result_document()
        document["roles"][0]["measurement_c"]["gamma"] = 0.99
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

        document = valid_result_document()
        document["roles"][0]["measurement_c"]["td_target_model"] = "trained_target"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_an_empty_summary_must_not_carry_fabricated_values(self):
        document = valid_result_document()
        block = document["roles"][0]["measurement_b"]["q_bc_disagree_rows"]
        block["q_margin"] = {"count": 0, "mean": 0.5, "quantiles": None}
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_summary_must_carry_the_locked_quantile_set(self):
        document = valid_result_document()
        block = document["roles"][0]["measurement_b"]["all_eligible_rows"]
        block["q_margin"]["quantiles"].pop("0.95")
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_available_measurement_d_comparison_counts_must_partition(self):
        document = valid_result_document(hand_progression_available=True)
        pair = document["roles"][0]["measurement_d"]["post_discard_shanten"]["q_vs_bc"]
        pair["equal_post_discard_shanten_count"] += 1
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_available_measurement_d_must_not_record_an_unavailable_reason(self):
        document = valid_result_document(hand_progression_available=True)
        document["roles"][0]["measurement_d"]["unavailable_reason"] = "partial"
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_available_measurement_d_worsening_difference_must_be_derivable(self):
        document = valid_result_document(hand_progression_available=True)
        pair = document["roles"][0]["measurement_d"]["post_discard_shanten"]["q_vs_bc"]
        pair["worsen_shanten_rate_difference"] = 0.0
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_tampered_retention_target_is_rejected(self):
        document = valid_result_document()
        document["retention"] = {"backend": "anywhere", "key": RETENTION_KEY}
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_tampered_limitations_are_rejected(self):
        document = valid_result_document()
        document["limitations"] = ["it is fine"]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_a_missing_measurement_a_stratification_is_rejected(self):
        document = valid_result_document()
        del document["roles"][0]["measurement_a"]["stratifications"]["decision_depth"]
        with self.assertRaises(OfflineQDiagnosisError):
            validate_diagnosis_result(document)

    def test_an_available_measurement_d_fixture_validates(self):
        document = valid_result_document(hand_progression_available=True)
        self.assertEqual(
            document["roles"][0]["measurement_d"],
            available_measurement_d(),
        )
        self.assertIs(validate_diagnosis_result(document)["classification"], None)


class FixedSummaryTest(unittest.TestCase):
    def test_summary_uses_the_locked_quantile_set(self):
        summary = fixed_summary([0.0, 1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["mean"], 2.0)
        self.assertEqual(
            sorted(summary["quantiles"]),
            sorted(format(value, "g") for value in FIXED_QUANTILES),
        )
        self.assertEqual(summary["quantiles"]["0"], 0.0)
        self.assertEqual(summary["quantiles"]["0.5"], 2.0)
        self.assertEqual(summary["quantiles"]["1"], 4.0)

    def test_an_empty_population_has_no_fabricated_values(self):
        self.assertEqual(
            fixed_summary([]), {"count": 0, "mean": None, "quantiles": None}
        )

    def test_non_finite_values_fail_closed(self):
        with self.assertRaises(OfflineQDiagnosisError):
            fixed_summary([0.0, float("inf")])
        with self.assertRaises(OfflineQDiagnosisError):
            fixed_summary([float("nan")])

    def test_stratification_labels_are_fixed_before_results(self):
        self.assertEqual(legal_action_count_bucket(2), "2")
        self.assertEqual(legal_action_count_bucket(8), "8")
        self.assertEqual(legal_action_count_bucket(14), "9+")
        self.assertEqual(decision_depth_band(0), "0-3")
        self.assertEqual(decision_depth_band(DECISION_DEPTH_BAND_EDGES[0]), "4-7")
        self.assertEqual(decision_depth_band(DECISION_DEPTH_BAND_EDGES[-1]), "16+")


class HandProgressionDerivationTest(unittest.TestCase):
    def test_the_discard_block_maps_to_tiles_independently_of_the_actor(self):
        from lisjong.action_vocabulary import decode_action

        for index in range(VOCABULARY_SIZE):
            if action_family(index) != "discard":
                self.assertFalse(is_discard_index(index))
                continue
            self.assertTrue(is_discard_index(index))
            tile = discard_tile_for_index(index)
            for seat in Seat:
                self.assertEqual(decode_action(index, seat).tile, tile)

    def test_a_non_discard_index_is_refused(self):
        with self.assertRaises(OfflineQDiagnosisError):
            discard_tile_for_index(discard_index("1m") + VOCABULARY_SIZE)

    def test_the_concealed_hand_round_trips_through_the_feature_row(self):
        tiles = hand_tiles("123456789m123p19s")
        values = feature_row_with_hand(tiles)
        self.assertEqual(Counter(reconstruct_concealed_tiles(values)), Counter(tiles))

    def test_only_the_own_hand_group_is_read(self):
        tiles = hand_tiles("123456789m123p19s")
        baseline = feature_row_with_hand(tiles)
        noisy = feature_row_with_hand(tiles, filler=1.0)
        self.assertNotEqual(baseline[:8], noisy[:8])
        self.assertEqual(
            reconstruct_concealed_tiles(baseline), reconstruct_concealed_tiles(noisy)
        )

    def test_the_own_hand_slice_is_the_only_source_of_the_hand(self):
        tiles = hand_tiles("123456789m123p19s")
        values = feature_row_with_hand(tiles)
        cleared = list(values)
        for index in range(OWN_HAND_TILE_COUNT_START, OWN_HAND_TILE_COUNT_STOP):
            cleared[index] = 0.0
        with self.assertRaises(OfflineQAmbiguousStateError):
            reconstruct_concealed_tiles(cleared)

    def test_a_non_integer_tile_count_is_ambiguous(self):
        values = feature_row_with_hand(hand_tiles("123456789m123p19s"))
        values[OWN_HAND_TILE_COUNT_START] = 0.1
        with self.assertRaises(OfflineQAmbiguousStateError):
            reconstruct_concealed_tiles(values)

    def test_an_out_of_range_tile_count_is_ambiguous(self):
        values = feature_row_with_hand(hand_tiles("123456789m123p19s"))
        values[OWN_HAND_TILE_COUNT_START] = 5 / 4
        with self.assertRaises(OfflineQAmbiguousStateError):
            reconstruct_concealed_tiles(values)

    def test_an_invalid_concealed_hand_size_is_ambiguous(self):
        values = feature_row_with_hand(hand_tiles("123456789m123p"))
        with self.assertRaises(OfflineQAmbiguousStateError):
            reconstruct_concealed_tiles(values)

    def test_a_wrong_feature_dimension_is_refused(self):
        with self.assertRaises(OfflineQDiagnosisError):
            reconstruct_concealed_tiles([0.0] * (FEATURE_DIMENSION - 1))

    def test_post_discard_shanten_uses_the_first_party_primitive(self):
        from lisjong.hand_evaluation import calculate_shanten

        tiles = hand_tiles("123456789m123p19s")
        values = feature_row_with_hand(tiles)
        progression = hand_progression(values, discard_index("9s"))
        self.assertEqual(progression.pre_discard_shanten, calculate_shanten(tiles))
        self.assertEqual(
            progression.post_discard_shanten,
            calculate_shanten(hand_tiles("123456789m123p1s")),
        )
        self.assertTrue(progression.keeps_shanten)
        self.assertFalse(progression.worsens_shanten)

    def test_a_worsening_discard_is_reported_as_worsening(self):
        tiles = hand_tiles("123456789m123p19s")
        values = feature_row_with_hand(tiles)
        progression = hand_progression(values, discard_index("1m"))
        self.assertTrue(progression.worsens_shanten)
        self.assertFalse(progression.keeps_shanten)

    def test_a_discard_absent_from_the_hand_is_ambiguous(self):
        values = feature_row_with_hand(hand_tiles("123456789m123p19s"))
        with self.assertRaises(OfflineQAmbiguousStateError):
            hand_progression(values, discard_index("5s"))

    def test_one_row_reconstruction_serves_every_candidate(self):
        tiles = hand_tiles("123456789m123p19s")
        values = feature_row_with_hand(tiles)
        progressions = hand_progression_for_row(
            values, (discard_index("9s"), discard_index("1m"))
        )
        self.assertEqual(len(progressions), 2)
        self.assertEqual(
            {item.pre_discard_shanten for item in progressions},
            {progressions[0].pre_discard_shanten},
        )
        self.assertLess(
            progressions[0].post_discard_shanten, progressions[1].post_discard_shanten
        )

    def test_ukeire_states_why_it_is_unavailable(self):
        self.assertIn("UkeirePolicy", UKEIRE_UNAVAILABLE_REASON)


if __name__ == "__main__":
    unittest.main()
