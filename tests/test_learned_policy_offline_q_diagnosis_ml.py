"""Artifact-only failure diagnosis measurement tests (Issue #152).

ここで固定するのはtorchを必要とする境界である。

- artifact / dataset identity binding のfail closed
- serving activationと同じeligibility（forced / 非discard / support-incomplete除外）
- Measurement A-Cの母数、stratification、TD target / Bellman residual
- Measurement Dが`UNAVAILABLE`でもA-Cが成立すること
- TRAIN / replacement TEST roleの分離
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import (
    write_synthetic_dataset,
    write_synthetic_replacement_test,
)
from _learned_policy_offline_q_diagnosis_fixtures import (
    discard_index,
    feature_row_with_hand,
    hand_tiles,
)

from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.diagnosis import (
    LOCKED_SOURCE_IDENTITIES,
    TD_TARGET_MODEL,
    DiagnosisRole,
    ExpectedArtifactIdentities,
    RolePopulation,
    bind_diagnosis_inputs,
    build_diagnosis_result,
    build_q_ranking,
    dataset_role_populations,
    diagnose_role,
    measurement_d,
    require_finite,
    select_eligible_rows,
    validate_diagnosis_result,
)
from lisjong_arena.learned_policy_offline_q.errors import OfflineQDiagnosisError
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    MeasurementAvailability,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint,
    train_q_model,
)
from lisjong_arena.learned_policy_offline_q.replacement_test import (
    load_replacement_test_tensors,
    support_mask_from_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _synthetic_expected(dataset, bc_checkpoint, q_checkpoint, replacement):
    return ExpectedArtifactIdentities(
        dataset_identity=dataset.identity,
        bc_checkpoint_identity=bc_checkpoint.identity,
        q_checkpoint_identity=q_checkpoint.identity,
        replacement_test_artifact_identity=replacement.identity,
        supported_indices_digest=q_checkpoint.manifest["supported_indices_digest"],
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class DiagnosisMeasurementTest(unittest.TestCase):
    """1度だけ合成candidate pairを作り、全measurement boundaryを検証する。"""

    @classmethod
    def setUpClass(cls):
        cls._root = Path(tempfile.mkdtemp())
        cls.dataset = write_synthetic_dataset(cls._root / "dataset")
        cls.replacement = write_synthetic_replacement_test(cls._root / "replacement")
        cls.bc_checkpoint = _save_bc(cls.dataset, cls._root / "bc-checkpoint")
        cls.q_checkpoint = save_checkpoint(
            cls._root / "q-checkpoint", cls.dataset, train_q_model(cls.dataset)
        )
        cls.support_mask = support_mask_from_checkpoint(
            cls.q_checkpoint.supported_indices
        )
        cls.split_tensors = load_split_tensors(cls.dataset)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._root, ignore_errors=True)

    def _binding(self):
        return bind_diagnosis_inputs(
            dataset=self.dataset,
            bc_checkpoint=self.bc_checkpoint,
            q_checkpoint=self.q_checkpoint,
            replacement_test=self.replacement,
            expected=_synthetic_expected(
                self.dataset, self.bc_checkpoint, self.q_checkpoint, self.replacement
            ),
        )

    def _roles(self):
        populations = list(
            dataset_role_populations(self.dataset, self.split_tensors)
        ) + [
            RolePopulation(
                role=DiagnosisRole.REPLACEMENT_TEST,
                tensors=load_replacement_test_tensors(self.replacement),
                rows=self.replacement.rows,
            )
        ]
        return [
            diagnose_role(
                population,
                bc_model=self.bc_checkpoint.model,
                q_model=self.q_checkpoint.model,
                support_mask=self.support_mask,
            )
            for population in populations
        ]

    # --- binding ---------------------------------------------------------

    def test_binding_records_the_observed_identities(self):
        binding = self._binding()
        self.assertEqual(binding.dataset_identity, self.dataset.identity)
        self.assertEqual(binding.q_checkpoint_identity, self.q_checkpoint.identity)
        self.assertEqual(
            binding.supported_indices_digest,
            self.q_checkpoint.manifest["supported_indices_digest"],
        )

    def test_a_synthetic_pair_is_not_a_real_artifact_execution(self):
        self.assertFalse(self._binding().real_artifact_execution)

    def test_an_artifact_identity_mismatch_fails_closed(self):
        wrong = ExpectedArtifactIdentities(
            dataset_identity=LOCKED_SOURCE_IDENTITIES.dataset_identity,
            bc_checkpoint_identity=self.bc_checkpoint.identity,
            q_checkpoint_identity=self.q_checkpoint.identity,
            replacement_test_artifact_identity=self.replacement.identity,
            supported_indices_digest=self.q_checkpoint.manifest[
                "supported_indices_digest"
            ],
        )
        with self.assertRaises(OfflineQDiagnosisError):
            bind_diagnosis_inputs(
                dataset=self.dataset,
                bc_checkpoint=self.bc_checkpoint,
                q_checkpoint=self.q_checkpoint,
                replacement_test=self.replacement,
                expected=wrong,
            )

    def test_a_supported_indices_digest_mismatch_fails_closed(self):
        expected = _synthetic_expected(
            self.dataset, self.bc_checkpoint, self.q_checkpoint, self.replacement
        )
        tampered = ExpectedArtifactIdentities(
            dataset_identity=expected.dataset_identity,
            bc_checkpoint_identity=expected.bc_checkpoint_identity,
            q_checkpoint_identity=expected.q_checkpoint_identity,
            replacement_test_artifact_identity=(
                expected.replacement_test_artifact_identity
            ),
            supported_indices_digest=LOCKED_SOURCE_IDENTITIES.supported_indices_digest,
        )
        with self.assertRaises(OfflineQDiagnosisError):
            bind_diagnosis_inputs(
                dataset=self.dataset,
                bc_checkpoint=self.bc_checkpoint,
                q_checkpoint=self.q_checkpoint,
                replacement_test=self.replacement,
                expected=tampered,
            )

    def test_a_bc_q_dataset_identity_mismatch_fails_closed(self):
        other_root = Path(tempfile.mkdtemp())
        try:
            other_dataset = write_synthetic_dataset(
                other_root / "dataset", rows_per_game=8
            )
            other_bc = _save_bc(other_dataset, other_root / "bc-checkpoint")
            self.assertNotEqual(other_dataset.identity, self.dataset.identity)
            with self.assertRaises(OfflineQDiagnosisError):
                bind_diagnosis_inputs(
                    dataset=self.dataset,
                    bc_checkpoint=other_bc,
                    q_checkpoint=self.q_checkpoint,
                    replacement_test=self.replacement,
                    expected=_synthetic_expected(
                        self.dataset, other_bc, self.q_checkpoint, self.replacement
                    ),
                )
        finally:
            shutil.rmtree(other_root, ignore_errors=True)

    # --- eligibility -----------------------------------------------------

    def test_eligibility_matches_the_serving_activation_rule(self):
        tensors = self.split_tensors[Split.TRAIN]
        eligible, counts = select_eligible_rows(tensors, self.support_mask)
        self.assertEqual(counts.total_row_count, tensors.row_count)
        self.assertEqual(counts.eligible_row_count, int(eligible.sum()))
        self.assertGreater(counts.eligible_row_count, 0)
        self.assertLessEqual(counts.eligible_row_count, counts.choice_row_count)
        self.assertTrue(bool((tensors.legal_mask[eligible].sum(dim=1) >= 2).all()))

    def test_forced_and_unsupported_rows_are_excluded(self):
        import torch

        tensors = self.split_tensors[Split.TRAIN]
        forced = tensors.legal_mask.clone()
        forced[0] = False
        forced[0, tensors.behavior_action_index[0]] = True
        narrowed = type(tensors)(
            split=tensors.split,
            features=tensors.features,
            legal_mask=forced,
            behavior_action_index=tensors.behavior_action_index,
            reward=tensors.reward,
            terminal=tensors.terminal,
            next_features=tensors.next_features,
            next_legal_mask=tensors.next_legal_mask,
            row_indices=tensors.row_indices,
        )
        eligible, counts = select_eligible_rows(narrowed, self.support_mask)
        self.assertFalse(bool(eligible[0]))
        self.assertEqual(counts.choice_row_count, tensors.row_count - 1)

        unsupported = torch.zeros_like(self.support_mask)
        unsupported[int(tensors.behavior_action_index[0])] = True
        _, restricted = select_eligible_rows(tensors, unsupported)
        self.assertEqual(restricted.eligible_row_count, 0)

    def test_non_discard_rows_are_excluded(self):
        tensors = self.split_tensors[Split.TRAIN]
        with_riichi = tensors.legal_mask.clone()
        with_riichi[0, 74] = True  # the riichi vocabulary block
        widened = type(tensors)(
            split=tensors.split,
            features=tensors.features,
            legal_mask=with_riichi,
            behavior_action_index=tensors.behavior_action_index,
            reward=tensors.reward,
            terminal=tensors.terminal,
            next_features=tensors.next_features,
            next_legal_mask=tensors.next_legal_mask,
            row_indices=tensors.row_indices,
        )
        support = self.support_mask.clone()
        support[74] = True
        eligible, counts = select_eligible_rows(widened, support)
        self.assertFalse(bool(eligible[0]))
        self.assertEqual(counts.ordinary_discard_row_count, counts.choice_row_count - 1)

    # --- measurements ----------------------------------------------------

    def test_roles_are_separated_and_each_carries_a_to_z_measurements(self):
        roles = self._roles()
        self.assertEqual(
            [role["role"] for role in roles],
            [
                "dataset-train",
                "dataset-validation",
                "dataset-test",
                "replacement-test",
            ],
        )
        for role in roles:
            self.assertFalse(role["is_generalization_evidence"])
            self.assertIn("measurement_a", role)
            self.assertIn("measurement_b", role)
            self.assertIn("measurement_c", role)
            self.assertIn("measurement_d", role)
        self.assertEqual(roles[0]["source_artifact"], "dataset")
        self.assertEqual(roles[-1]["source_artifact"], "replacement-test")

    def test_measurement_a_counts_and_rates_agree(self):
        role = self._roles()[0]
        block = role["measurement_a"]
        eligible = role["row_counts"]["eligible_row_count"]
        self.assertGreater(eligible, 0)
        self.assertEqual(block["eligible_row_count"], eligible)
        for name in ("q_vs_bc", "q_vs_behavior", "bc_vs_behavior"):
            count = block[f"{name}_disagreement_count"]
            self.assertLessEqual(count, eligible)
            self.assertAlmostEqual(block[f"{name}_disagreement_rate"], count / eligible)
        for strata in block["stratifications"].values():
            self.assertEqual(sum(entry["row_count"] for entry in strata), eligible)

    def test_measurement_a_stratifies_by_terminality(self):
        strata = self._roles()[0]["measurement_a"]["stratifications"]
        self.assertEqual(
            {entry["stratum"] for entry in strata["transition_terminality"]},
            {"terminal", "nonterminal"},
        )

    def test_measurement_b_derives_top1_top2_margin_and_gaps(self):
        block = self._roles()[0]["measurement_b"]
        for scope in ("all_eligible_rows", "q_bc_agree_rows", "q_bc_disagree_rows"):
            self.assertEqual(
                set(block[scope]),
                {
                    "q_top1_value",
                    "q_top2_value",
                    "q_margin",
                    "q_value_of_bc_action",
                    "q_value_of_behavior_action",
                    "q_selected_vs_bc_selected_gap",
                    "q_selected_vs_behavior_gap",
                },
            )
        summary = block["all_eligible_rows"]
        self.assertEqual(summary["q_margin"]["count"], summary["q_top1_value"]["count"])
        self.assertGreaterEqual(summary["q_margin"]["quantiles"]["0"], 0.0)
        self.assertEqual(
            summary["q_top1_value"]["count"],
            block["q_bc_agree_rows"]["q_top1_value"]["count"]
            + block["q_bc_disagree_rows"]["q_top1_value"]["count"],
        )

    def test_the_q_selected_action_never_scores_below_a_compared_action(self):
        import torch

        tensors = self.split_tensors[Split.TRAIN]
        eligible, _ = select_eligible_rows(tensors, self.support_mask)
        selector = torch.nonzero(eligible).flatten()
        with torch.inference_mode():
            q_values = self.q_checkpoint.model(
                tensors.features.index_select(0, selector)
            ).clone()
        legal_mask = tensors.legal_mask.index_select(0, selector)
        behavior = tensors.behavior_action_index.index_select(0, selector).tolist()
        ranking = build_q_ranking(q_values, legal_mask, behavior, behavior)
        for gap in ranking.q_vs_behavior_gap:
            self.assertGreaterEqual(gap, 0.0)
        for top1, top2 in zip(ranking.top1_value, ranking.top2_value, strict=True):
            self.assertGreaterEqual(top1, top2)

    def test_measurement_c_derives_td_targets_and_residuals(self):
        role = self._roles()[0]
        block = role["measurement_c"]
        eligible = role["row_counts"]["eligible_row_count"]
        self.assertEqual(block["td_target_model"], TD_TARGET_MODEL)
        self.assertEqual(
            block["terminal_row_count"] + block["nonterminal_row_count"], eligible
        )
        self.assertEqual(
            block["bootstrap_eligible_row_count"]
            + block["unsupported_bootstrap_row_count"],
            eligible,
        )
        self.assertEqual(
            block["absolute_bellman_residual"]["all_bootstrap_eligible_rows"]["count"],
            block["bootstrap_eligible_row_count"],
        )
        self.assertGreaterEqual(
            block["absolute_bellman_residual"]["all_bootstrap_eligible_rows"][
                "quantiles"
            ]["0"],
            0.0,
        )
        for name in ("td_target", "predicted_selected_q", "absolute_bellman_residual"):
            self.assertEqual(
                set(block[name]),
                {
                    "all_bootstrap_eligible_rows",
                    "q_bc_agree_rows",
                    "q_bc_disagree_rows",
                    "terminal_rows",
                    "nonterminal_rows",
                },
            )
            self.assertEqual(
                block[name]["terminal_rows"]["count"]
                + block[name]["nonterminal_rows"]["count"],
                block["bootstrap_eligible_row_count"],
            )
            self.assertEqual(
                block[name]["q_bc_agree_rows"]["count"]
                + block[name]["q_bc_disagree_rows"]["count"],
                block["bootstrap_eligible_row_count"],
            )

    def test_measurement_c_immediate_reward_is_stratified(self):
        block = self._roles()[0]["measurement_c"]["immediate_reward"]
        self.assertEqual(
            block["terminal_rows"]["count"] + block["nonterminal_rows"]["count"],
            block["all_eligible_rows"]["count"],
        )
        self.assertEqual(
            block["q_bc_agree_rows"]["count"] + block["q_bc_disagree_rows"]["count"],
            block["all_eligible_rows"]["count"],
        )

    # --- Measurement D ---------------------------------------------------

    def test_measurement_d_is_unavailable_for_rows_without_a_concealed_hand(self):
        roles = self._roles()
        for role in roles:
            block = role["measurement_d"]
            self.assertEqual(block["status"], MeasurementAvailability.UNAVAILABLE.value)
            self.assertTrue(block["unavailable_reason"])
            self.assertIsNone(block["post_discard_shanten"])
            self.assertEqual(
                block["ukeire"]["status"], MeasurementAvailability.UNAVAILABLE.value
            )
            # A-C stay valid even though D could not be derived.
            self.assertGreater(role["measurement_a"]["eligible_row_count"], 0)
            self.assertGreater(role["measurement_c"]["bootstrap_eligible_row_count"], 0)

    def test_measurement_d_compares_q_bc_and_behavior_when_derivable(self):
        import torch

        tiles = hand_tiles("123456789m123p19s")
        features = torch.tensor([feature_row_with_hand(tiles)] * 2, dtype=torch.float32)
        keep = discard_index("9s")
        worsen = discard_index("1m")
        block = measurement_d(features, [worsen, worsen], [keep, keep], [keep, keep])
        self.assertEqual(block["status"], MeasurementAvailability.AVAILABLE.value)
        summaries = block["post_discard_shanten"]
        self.assertEqual(summaries["q"]["worsen_shanten_count"], 2)
        self.assertEqual(summaries["bc"]["keep_shanten_count"], 2)
        self.assertEqual(summaries["q_vs_bc"]["higher_post_discard_shanten_count"], 2)
        self.assertEqual(summaries["q_vs_bc"]["worsen_shanten_rate_difference"], 1.0)
        self.assertEqual(
            summaries["q_vs_behavior"]["higher_post_discard_shanten_count"], 2
        )
        self.assertEqual(
            block["ukeire"]["status"], MeasurementAvailability.UNAVAILABLE.value
        )

    # --- non-finite / result artifact ------------------------------------

    def test_non_finite_values_fail_closed(self):
        import torch

        self.assertEqual(require_finite(torch.zeros(3), "zeros"), 0)
        with self.assertRaises(OfflineQDiagnosisError):
            require_finite(torch.tensor([0.0, float("inf")]), "outputs")
        with self.assertRaises(OfflineQDiagnosisError):
            require_finite(torch.tensor([float("nan")]), "outputs")

    def test_the_result_artifact_validates_and_records_no_outcome(self):
        document = build_diagnosis_result(binding=self._binding(), roles=self._roles())
        self.assertIs(validate_diagnosis_result(document)["classification"], None)
        self.assertEqual(len(document["roles"]), 4)
        self.assertFalse(
            document["input_artifact_identities"]["real_artifact_execution"]
        )
        self.assertEqual(
            document["locked_source_identities"],
            LOCKED_SOURCE_IDENTITIES.to_document(),
        )

    def test_the_result_artifact_is_canonical_json_serializable(self):
        from lisjong_arena._artifact_io import canonical_json_text

        document = build_diagnosis_result(binding=self._binding(), roles=self._roles())
        self.assertTrue(canonical_json_text(document).endswith("\n"))

    def test_the_diagnose_cli_writes_a_validated_unclassified_result(self):
        """CLI pathをend-to-endで通す。

        `screen`のresult documentが実runで初めてexercisedされてAttributeError
        になった前例（PR #151 2つ目のcommit）を繰り返さないため、handler側の
        組み立てもtestで固定する。identity bindingとretained bundleの解決だけを
        合成candidate pairへ差し替える。
        """
        import json
        from unittest.mock import patch

        from lisjong_arena.learned_policy_offline_q import diagnosis as diagnosis_module
        from lisjong_arena.learned_policy_offline_q.__main__ import main
        from lisjong_arena.learned_policy_offline_q.retention import RetainedCandidates

        retained = RetainedCandidates(
            freeze=None,
            bc_checkpoint=self.bc_checkpoint,
            q_checkpoint=self.q_checkpoint,
        )
        expected = _synthetic_expected(
            self.dataset, self.bc_checkpoint, self.q_checkpoint, self.replacement
        )
        result_path = Path(tempfile.mkdtemp()) / "diagnosis.json"
        try:
            with (
                patch.object(diagnosis_module, "LOCKED_SOURCE_IDENTITIES", expected),
                patch(
                    "lisjong_arena.learned_policy_offline_q.retention.strict_readback",
                    return_value=retained,
                ),
            ):
                exit_code = main(
                    [
                        "diagnose",
                        "--bundle",
                        str(self._root / "retention"),
                        "--dataset",
                        str(self.dataset.path),
                        "--replacement-test",
                        str(self.replacement.path),
                        "--result",
                        str(result_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            validate_diagnosis_result(document)
            self.assertIsNone(document["classification"])
            self.assertEqual(
                [role["role"] for role in document["roles"]],
                [
                    "dataset-train",
                    "dataset-validation",
                    "dataset-test",
                    "replacement-test",
                ],
            )
        finally:
            shutil.rmtree(result_path.parent, ignore_errors=True)

    def test_the_diagnose_cli_fails_closed_on_an_unbound_artifact(self):
        from unittest.mock import patch

        from lisjong_arena.learned_policy_offline_q.__main__ import main
        from lisjong_arena.learned_policy_offline_q.retention import RetainedCandidates

        retained = RetainedCandidates(
            freeze=None,
            bc_checkpoint=self.bc_checkpoint,
            q_checkpoint=self.q_checkpoint,
        )
        result_path = Path(tempfile.mkdtemp()) / "diagnosis.json"
        try:
            with patch(
                "lisjong_arena.learned_policy_offline_q.retention.strict_readback",
                return_value=retained,
            ):
                with self.assertRaises(OfflineQDiagnosisError):
                    main(
                        [
                            "diagnose",
                            "--bundle",
                            str(self._root / "retention"),
                            "--dataset",
                            str(self.dataset.path),
                            "--replacement-test",
                            str(self.replacement.path),
                            "--result",
                            str(result_path),
                        ]
                    )
            self.assertFalse(result_path.exists())
        finally:
            shutil.rmtree(result_path.parent, ignore_errors=True)


def _save_bc(dataset, destination):
    from lisjong_arena.learned_policy_offline_q.bc_training import (
        save_checkpoint as save_bc_checkpoint,
    )

    return save_bc_checkpoint(destination, dataset, train_bc_model(dataset))


if __name__ == "__main__":
    unittest.main()
