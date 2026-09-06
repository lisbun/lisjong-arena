"""P1 Gate A derived-view, training, and measurement tests (Issue #158).

ここで固定するのはtorchを必要とする境界である。

- retained rowsからのderived view（source / nonterminal next / terminal padding）
- row order / split membership / behavior / reward / terminal / legal maskの不変性
- 8241-input modelのparameter countと、input dimension以外が同一であること
- retained artifact identity mismatchのfail closed
- Gate Aのpaired hand-progression metricsと4 armの同一row評価
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset
from _learned_policy_offline_q_p1_gate_a_fixtures import (
    write_hand_dataset,
    write_hand_replacement_test,
)

from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.diagnosis import (
    LOCKED_SOURCE_IDENTITIES,
    ExpectedArtifactIdentities,
)
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQAmbiguousStateError,
    OfflineQDiagnosisError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    MeasurementAvailability,
    keep_shanten_tile_mask,
)
from lisjong_arena.learned_policy_offline_q.p1_features import (
    P1_FEATURE_DIMENSION,
    derive_all_split_tensors,
    derive_p1_feature_tensor,
    derive_replacement_test_tensors,
    derive_split_tensors,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_a import (
    ARMS,
    PAIRS,
    CandidateModelRecord,
    P1GateAOutcome,
    P1GateARole,
    P1RolePopulation,
    bind_gate_a_inputs,
    build_gate_a_result,
    derive_classification,
    evaluate_role,
    validate_gate_a_result,
    verify_derived_alignment,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    create_p1_model,
    model_weights_digest,
    require_p1_split_tensors,
    train_p1_q_model,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    MAXIMUM_EPOCHS,
    Split,
)
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint,
    train_q_model,
)
from lisjong_arena.learned_policy_offline_q.replacement_test import (
    load_replacement_test_tensors,
    support_mask_from_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors
from lisjong_arena.learned_policy_stage2.network import parameter_count

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

ROWS_PER_GAME = 6


def _save_bc(dataset, destination):
    from lisjong_arena.learned_policy_offline_q.bc_training import (
        save_checkpoint as save_bc_checkpoint,
    )

    return save_bc_checkpoint(destination, dataset, train_bc_model(dataset))


def _synthetic_expected(dataset, bc_checkpoint, q_checkpoint, replacement):
    return ExpectedArtifactIdentities(
        dataset_identity=dataset.identity,
        bc_checkpoint_identity=bc_checkpoint.identity,
        q_checkpoint_identity=q_checkpoint.identity,
        replacement_test_artifact_identity=replacement.identity,
        supported_indices_digest=q_checkpoint.manifest["supported_indices_digest"],
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class P1ModelTest(unittest.TestCase):
    """8241-input modelのshapeとparameter countを固定する。"""

    def test_the_parameter_count_is_the_locked_value(self):
        model = create_p1_model()
        self.assertEqual(parameter_count(model), 1_158_434)
        self.assertEqual(parameter_count(model), P1_EXPECTED_PARAMETER_COUNT)

    def test_hidden_width_is_not_shrunk_to_match_the_baseline_count(self):
        from lisjong_arena.learned_policy_stage2.network import create_model

        baseline = create_model()
        self.assertEqual(parameter_count(baseline), 1_153_698)
        self.assertEqual(
            parameter_count(create_p1_model()) - parameter_count(baseline), 4_736
        )

    def test_the_layer_shapes_are_8241_128_802(self):
        import torch

        model = create_p1_model()
        layers = [
            module for module in model.modules() if isinstance(module, torch.nn.Linear)
        ]
        self.assertEqual(
            [tuple(layer.weight.shape) for layer in layers], [(128, 8241), (802, 128)]
        )
        self.assertTrue(
            any(isinstance(module, torch.nn.ReLU) for module in model.modules())
        )

    def test_the_weights_digest_is_deterministic(self):
        model = create_p1_model()
        self.assertEqual(model_weights_digest(model), model_weights_digest(model))
        self.assertEqual(len(model_weights_digest(model)), 64)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class DerivedViewTest(unittest.TestCase):
    """retained rowsからのderived viewが何を保存し何を足すのかを固定する。"""

    @classmethod
    def setUpClass(cls):
        cls._root = Path(tempfile.mkdtemp())
        cls.dataset = write_hand_dataset(
            cls._root / "dataset", rows_per_game=ROWS_PER_GAME
        )
        cls.split_tensors = load_split_tensors(cls.dataset)
        cls.p1_split_tensors, cls.coverage = derive_all_split_tensors(cls.split_tensors)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._root, ignore_errors=True)

    def test_the_derived_source_features_are_8241_wide(self):
        for split, entry in self.p1_split_tensors.items():
            self.assertEqual(
                int(entry.features.shape[1]), P1_FEATURE_DIMENSION, split.value
            )
            self.assertEqual(int(entry.next_features.shape[1]), P1_FEATURE_DIMENSION)

    def test_the_base_columns_are_kept_verbatim(self):
        import torch

        for split, entry in self.p1_split_tensors.items():
            base = self.split_tensors[split]
            self.assertTrue(
                torch.equal(entry.features[:, :FEATURE_DIMENSION], base.features)
            )
            self.assertTrue(
                torch.equal(
                    entry.next_features[:, :FEATURE_DIMENSION], base.next_features
                )
            )

    def test_the_appended_block_is_the_keep_shanten_mask(self):
        entry = self.p1_split_tensors[Split.TRAIN]
        base = self.split_tensors[Split.TRAIN]
        for position in range(min(8, base.row_count)):
            self.assertEqual(
                tuple(entry.features[position, FEATURE_DIMENSION:].tolist()),
                keep_shanten_tile_mask(base.features[position]),
            )

    def test_a_nonterminal_next_state_is_derived(self):
        entry = self.p1_split_tensors[Split.TRAIN]
        base = self.split_tensors[Split.TRAIN]
        nonterminal = [
            position for position, flag in enumerate(base.terminal.tolist()) if not flag
        ]
        self.assertTrue(nonterminal)
        for position in nonterminal[:8]:
            self.assertEqual(
                tuple(entry.next_features[position, FEATURE_DIMENSION:].tolist()),
                keep_shanten_tile_mask(base.next_features[position]),
            )

    def test_a_terminal_next_state_keeps_the_all_zero_placeholder(self):
        entry = self.p1_split_tensors[Split.TRAIN]
        base = self.split_tensors[Split.TRAIN]
        terminal = base.terminal
        self.assertTrue(bool(terminal.any()))
        self.assertEqual(float(entry.next_features[terminal].abs().sum()), 0.0)

    def test_row_order_split_membership_and_labels_are_unchanged(self):
        import torch

        for split, entry in self.p1_split_tensors.items():
            base = self.split_tensors[split]
            self.assertEqual(entry.split, base.split)
            self.assertEqual(entry.row_indices, base.row_indices)
            self.assertEqual(entry.row_count, base.row_count)
            for name in (
                "legal_mask",
                "next_legal_mask",
                "behavior_action_index",
                "reward",
                "terminal",
            ):
                self.assertTrue(
                    torch.equal(getattr(entry, name), getattr(base, name)), name
                )

    def test_the_coverage_report_partitions_every_row(self):
        for split, coverage in self.coverage.items():
            base = self.split_tensors[split]
            document = coverage.to_document()
            self.assertEqual(document["row_count"], base.row_count)
            self.assertEqual(document["source_rows_derived"], base.row_count)
            self.assertEqual(document["imputed_row_count"], 0)
            self.assertEqual(
                document["nonterminal_next_rows_derived"]
                + document["terminal_next_rows_zero_padded"],
                base.row_count,
            )
            self.assertEqual(
                document["terminal_next_rows_zero_padded"], int(base.terminal.sum())
            )

    def test_the_derived_view_is_independent_of_the_legal_mask(self):
        import torch

        base = self.split_tensors[Split.TEST]
        flipped = type(base)(
            split=base.split,
            features=base.features,
            legal_mask=~base.legal_mask,
            behavior_action_index=base.behavior_action_index,
            reward=base.reward,
            terminal=base.terminal,
            next_features=base.next_features,
            next_legal_mask=~base.next_legal_mask,
            row_indices=base.row_indices,
        )
        derived, _ = derive_split_tensors(flipped)
        self.assertTrue(
            torch.equal(derived.features, self.p1_split_tensors[Split.TEST].features)
        )

    def test_an_ambiguous_row_fails_closed_instead_of_being_imputed(self):
        """own handを一意に復元できないrowは推測で埋めない。"""
        import torch

        with self.assertRaises(OfflineQAmbiguousStateError):
            derive_p1_feature_tensor(torch.zeros((1, FEATURE_DIMENSION)))

    def test_a_wrong_width_tensor_is_rejected(self):
        import torch

        with self.assertRaises(OfflineQProtocolError):
            derive_p1_feature_tensor(torch.zeros((1, FEATURE_DIMENSION - 1)))

    def test_require_p1_split_tensors_rejects_v1_tensors(self):
        with self.assertRaises(OfflineQProtocolError):
            require_p1_split_tensors(self.split_tensors)
        self.assertIs(
            require_p1_split_tensors(self.p1_split_tensors), self.p1_split_tensors
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class GateAMeasurementTest(unittest.TestCase):
    """合成candidate pairで、Gate Aの評価境界を1度だけ検証する。"""

    @classmethod
    def setUpClass(cls):
        cls._root = Path(tempfile.mkdtemp())
        cls.dataset = write_hand_dataset(
            cls._root / "dataset", rows_per_game=ROWS_PER_GAME
        )
        cls.replacement = write_hand_replacement_test(
            cls._root / "replacement", rows_per_game=ROWS_PER_GAME
        )
        cls.bc_checkpoint = _save_bc(cls.dataset, cls._root / "bc-checkpoint")
        cls.q_checkpoint = save_checkpoint(
            cls._root / "q-checkpoint", cls.dataset, train_q_model(cls.dataset)
        )
        cls.support_mask = support_mask_from_checkpoint(
            cls.q_checkpoint.supported_indices
        )
        cls.split_tensors = load_split_tensors(cls.dataset)
        cls.p1_split_tensors, cls.split_coverage = derive_all_split_tensors(
            cls.split_tensors
        )
        cls.replacement_tensors = load_replacement_test_tensors(cls.replacement)
        (
            cls.p1_replacement_tensors,
            cls.replacement_coverage,
        ) = derive_replacement_test_tensors(cls.replacement_tensors)
        cls.training_run = train_p1_q_model(cls.p1_split_tensors)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._root, ignore_errors=True)

    def _expected(self):
        return _synthetic_expected(
            self.dataset, self.bc_checkpoint, self.q_checkpoint, self.replacement
        )

    def _binding(self):
        return bind_gate_a_inputs(
            dataset=self.dataset,
            bc_checkpoint=self.bc_checkpoint,
            q_checkpoint=self.q_checkpoint,
            replacement_test=self.replacement,
            expected=self._expected(),
        )

    def _populations(self):
        populations = [
            P1RolePopulation(
                role=role,
                tensors=self.split_tensors[split],
                p1_tensors=self.p1_split_tensors[split],
                rows=tuple(
                    self.dataset.rows[index]
                    for index in self.split_tensors[split].row_indices
                ),
                coverage=self.split_coverage[split],
            )
            for role, split in (
                (P1GateARole.DATASET_TRAIN, Split.TRAIN),
                (P1GateARole.DATASET_VALIDATION, Split.VALIDATION),
                (P1GateARole.DATASET_TEST, Split.TEST),
            )
        ]
        populations.append(
            P1RolePopulation(
                role=P1GateARole.REPLACEMENT_TEST,
                tensors=self.replacement_tensors,
                p1_tensors=self.p1_replacement_tensors,
                rows=self.replacement.rows,
                coverage=self.replacement_coverage,
            )
        )
        return populations

    def _roles(self):
        return [
            evaluate_role(
                population,
                q_v1_model=self.q_checkpoint.model,
                q_v2_model=self.training_run.model,
                bc_model=self.bc_checkpoint.model,
                support_mask=self.support_mask,
            )
            for population in self._populations()
        ]

    def _candidate(self):
        import torch

        from lisjong_arena.learned_policy_offline_q.support import support_set_identity

        return CandidateModelRecord(
            weights_digest=model_weights_digest(self.training_run.model),
            selected_epoch=self.training_run.selected_epoch,
            final_validation_huber_loss=self.training_run.final_validation_huber_loss,
            epoch_history=self.training_run.history,
            supported_indices_digest=support_set_identity(
                sorted(
                    int(index)
                    for index in torch.nonzero(self.training_run.support_mask)
                    .flatten()
                    .tolist()
                )
            ),
        )

    # --- training --------------------------------------------------------

    def test_training_keeps_the_fixed_final_iteration_checkpoint(self):
        self.assertEqual(self.training_run.selected_epoch, MAXIMUM_EPOCHS)
        self.assertEqual(len(self.training_run.history), MAXIMUM_EPOCHS)

    def test_the_candidate_uses_the_same_support_set_as_the_retained_arm(self):
        self.assertEqual(
            self._candidate().supported_indices_digest,
            self.q_checkpoint.manifest["supported_indices_digest"],
        )

    def test_the_trained_model_consumes_the_derived_schema(self):
        import torch

        with torch.inference_mode():
            output = self.training_run.model(
                self.p1_split_tensors[Split.TEST].features[:2]
            )
        self.assertEqual(tuple(output.shape), (2, 802))

    # --- binding ---------------------------------------------------------

    def test_a_synthetic_pair_is_not_a_real_artifact_execution(self):
        self.assertFalse(self._binding().real_artifact_execution)

    def test_an_artifact_identity_mismatch_fails_closed(self):
        with self.assertRaises(OfflineQDiagnosisError):
            bind_gate_a_inputs(
                dataset=self.dataset,
                bc_checkpoint=self.bc_checkpoint,
                q_checkpoint=self.q_checkpoint,
                replacement_test=self.replacement,
                expected=LOCKED_SOURCE_IDENTITIES,
            )

    def test_a_substituted_dataset_identity_fails_closed(self):
        other = write_synthetic_dataset(self._root / "other-dataset")
        with self.assertRaises(OfflineQDiagnosisError):
            bind_gate_a_inputs(
                dataset=other,
                bc_checkpoint=self.bc_checkpoint,
                q_checkpoint=self.q_checkpoint,
                replacement_test=self.replacement,
                expected=self._expected(),
            )

    # --- derived alignment ----------------------------------------------

    def test_a_rewritten_base_column_fails_closed(self):
        population = self._populations()[2]
        tampered = population.p1_tensors.features.clone()
        tampered[0, 0] += 1.0
        broken = P1RolePopulation(
            role=population.role,
            tensors=population.tensors,
            p1_tensors=type(population.p1_tensors)(
                split=population.p1_tensors.split,
                features=tampered,
                legal_mask=population.p1_tensors.legal_mask,
                behavior_action_index=population.p1_tensors.behavior_action_index,
                reward=population.p1_tensors.reward,
                terminal=population.p1_tensors.terminal,
                next_features=population.p1_tensors.next_features,
                next_legal_mask=population.p1_tensors.next_legal_mask,
                row_indices=population.p1_tensors.row_indices,
            ),
            rows=population.rows,
            coverage=population.coverage,
        )
        with self.assertRaises(OfflineQDiagnosisError):
            verify_derived_alignment(broken)

    # --- measurement ------------------------------------------------------

    def test_every_role_reports_available_hand_progression(self):
        for role in self._roles():
            self.assertEqual(
                role["hand_progression"]["status"],
                MeasurementAvailability.AVAILABLE.value,
                role["role"],
            )

    def test_all_four_arms_are_measured_on_the_same_rows(self):
        for role in self._roles():
            eligible = role["row_counts"]["eligible_row_count"]
            arms = role["hand_progression"]["arms"]
            self.assertEqual({arms[arm]["row_count"] for arm in ARMS}, {eligible})
            pairs = role["hand_progression"]["pairs"]
            self.assertEqual({pairs[pair]["row_count"] for pair in PAIRS}, {eligible})

    def test_paired_counts_partition_the_rows(self):
        for role in self._roles():
            eligible = role["row_counts"]["eligible_row_count"]
            for pair in PAIRS:
                entry = role["hand_progression"]["pairs"][pair]
                self.assertEqual(
                    entry["lower_post_discard_shanten_count"]
                    + entry["equal_post_discard_shanten_count"]
                    + entry["higher_post_discard_shanten_count"],
                    eligible,
                    pair,
                )

    def test_per_seed_blocks_partition_the_rows(self):
        for role in self._roles():
            eligible = role["row_counts"]["eligible_row_count"]
            per_seed = role["hand_progression"]["per_seed"]
            self.assertEqual(sum(item["row_count"] for item in per_seed), eligible)
            self.assertEqual(
                [item["seed"] for item in per_seed],
                sorted(item["seed"] for item in per_seed),
            )

    def test_the_behavior_arm_matches_the_recorded_behavior_action(self):
        role = self._roles()[2]
        agreement = role["action_agreement"]
        self.assertEqual(
            agreement["eligible_row_count"], role["row_counts"]["eligible_row_count"]
        )
        for pair in ("q_v2_vs_q_v1", "q_v2_vs_bc", "bc_vs_behavior"):
            self.assertLessEqual(
                agreement[f"{pair}_disagreement_count"],
                agreement["eligible_row_count"],
            )

    def test_a_complete_result_document_validates_and_classifies(self):
        document = validate_gate_a_result(
            build_gate_a_result(
                binding=self._binding(),
                candidate=self._candidate(),
                roles=self._roles(),
            )
        )
        self.assertIsNone(document["classification"])
        self.assertEqual(document["generation_budget"]["new_hanchan"], 0)
        self.assertEqual(document["derived_feature"]["dimension"], 8241)
        self.assertIn(derive_classification(document), tuple(P1GateAOutcome))

    def test_a_synthetic_result_cannot_record_an_outcome(self):
        from lisjong_arena.learned_policy_offline_q.p1_gate_a import (
            record_classification,
        )

        document = validate_gate_a_result(
            build_gate_a_result(
                binding=self._binding(),
                candidate=self._candidate(),
                roles=self._roles(),
            )
        )
        with self.assertRaises(OfflineQDiagnosisError):
            record_classification(document, derive_classification(document))


if __name__ == "__main__":
    unittest.main()
