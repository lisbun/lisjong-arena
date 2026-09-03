"""Stage 3 Entry Gate torch-dependent training, evaluation, and artifact tests.

synthetic Stage 3 populationだけを使い、formal pilot generationやTEST inference
へは到達しない。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from _stage3_entry_gate_fixtures import STAGE3_BASE_SEEDS, stage3_population_artifacts

from lisjong_arena.phase8_sequential.model import S2_PARAMETER_COUNT
from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_entry_gate.artifact import (
    CHECKPOINT_SELECTION_RULE,
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    Stage3ArtifactError,
    execution_runtime_value,
    load_model,
    model_manifest_without_weights,
    save_model_artifact,
    validate_model_manifest,
)
from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    build_population_data,
    configure_torch_runtime,
    evaluate_on_population,
    evaluation_value,
    inventory_summary,
    train_population_candidate,
)
from lisjong_arena.stage3_entry_gate.population import (
    population_a_plan,
    population_b_plan,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _population_data(root: Path, population_id: str, base_seed: int, plan):
    persisted_raw, dataset = stage3_population_artifacts(root, base_seed)
    return build_population_data(
        population_id=population_id,
        population_identity=plan.population_identity,
        persisted_raw=persisted_raw,
        dataset=dataset,
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Stage3TrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.first = _population_data(
            root / "first", "A", STAGE3_BASE_SEEDS[0], population_a_plan()
        )
        cls.second = _population_data(
            root / "second", "B", STAGE3_BASE_SEEDS[1], population_b_plan()
        )
        cls.result = train_population_candidate(cls.first)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_training_uses_the_locked_phase8_s2_family_and_budget(self):
        self.assertIs(self.result.candidate, CANDIDATE)
        self.assertEqual(self.result.parameter_count, S2_PARAMETER_COUNT)
        self.assertEqual(self.result.config, FORMAL_TRAINING_CONFIG)
        self.assertEqual(self.result.config.seed, 0)
        self.assertEqual(self.result.config.dataloader_seed, 0)
        self.assertEqual(self.result.config.max_epochs, 40)
        self.assertEqual(self.result.config.patience, 6)
        self.assertTrue(self.result.config.deterministic_algorithms)

    def test_checkpoint_is_the_lowest_pooled_validation_mae_epoch(self):
        history = self.result.history
        self.assertTrue(history)
        best = min(value.validation_mae for value in history)
        selected = next(
            value for value in history if value.epoch == self.result.selected_epoch
        )
        self.assertEqual(selected.validation_mae, best)

    def test_delta_is_measured_against_the_conditional_uniform_reference_arm(self):
        validation = self.result.validation
        self.assertEqual(
            validation.snapshot_metrics,
            self.first.canonical_validation.snapshot_metrics,
        )
        self.assertAlmostEqual(
            validation.delta_mae,
            validation.snapshot_metrics.per_tile_mae - validation.metrics.per_tile_mae,
            places=12,
        )
        self.assertEqual(len(validation.per_game), len(self.first.validation_sequences))

    def test_physical_validity_fields_are_reported_for_the_pilot(self):
        physical = self.result.validation.physical_consistency
        self.assertEqual(physical["constraint_non_convergence_count"], 0)
        for name in (
            "maximum_row_column_residual",
            "concealed_size_inconsistency_max",
            "physical_conservation_violation_sample_rate",
            "blocking_gate_passed",
        ):
            self.assertIn(name, physical)

    def test_configure_torch_runtime_matches_the_locked_training_config(self):
        import torch

        torch.set_num_threads(2)
        configure_torch_runtime()
        self.assertEqual(torch.get_num_threads(), FORMAL_TRAINING_CONFIG.torch_threads)
        self.assertTrue(torch.are_deterministic_algorithms_enabled())

    def test_self_rollout_reproduces_the_training_time_validation_metrics(self):
        evaluation = evaluate_on_population(self.result.model, self.first)
        self.assertEqual(
            evaluation.metrics.per_tile_mae,
            self.result.validation.metrics.per_tile_mae,
        )
        self.assertEqual(
            evaluation.snapshot_metrics.per_tile_mae,
            self.result.validation.snapshot_metrics.per_tile_mae,
        )

    def test_cross_population_evaluation_uses_only_the_target_population(self):
        cell = evaluation_value(
            evaluate_on_population(self.result.model, self.second), self.second
        )
        self.assertEqual(cell["validation_population_id"], "B")
        self.assertEqual(
            cell["validation_dataset_identity"], self.second.dataset_identity
        )
        self.assertNotEqual(
            cell["validation_dataset_identity"], self.first.dataset_identity
        )
        self.assertEqual(
            cell["conditional_uniform_validation_mae"],
            self.second.baseline_metrics.per_tile_mae,
        )
        self.assertNotEqual(
            cell["conditional_uniform_validation_mae"],
            self.first.baseline_metrics.per_tile_mae,
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Stage3ModelArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.data = _population_data(
            root / "population", "A", STAGE3_BASE_SEEDS[0], population_a_plan()
        )
        cls.result = train_population_candidate(cls.data)
        cls.manifest = model_manifest_without_weights(
            population_id=cls.data.population_id,
            population_identity=cls.data.population_identity,
            raw_corpus_identity=cls.data.raw_corpus_identity,
            dataset_identity=cls.data.dataset_identity,
            inventory=inventory_summary(cls.data),
            result=cls.result,
            runtime=execution_runtime_value(),
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_artifact_roundtrips_state_dict_only_and_binds_identities(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "model"
            loaded = save_model_artifact(
                destination, self.result.model, dict(self.manifest)
            )
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                {MANIFEST_FILENAME, WEIGHTS_FILENAME},
            )
            self.assertEqual(
                loaded.manifest["training_population_identity"],
                self.data.population_identity,
            )
            self.assertEqual(
                loaded.manifest["dataset_identity"], self.data.dataset_identity
            )
            self.assertIs(loaded.manifest["test_partition_evaluated"], False)
            self.assertEqual(loaded.manifest["parameter_count"], S2_PARAMETER_COUNT)
            restored, manifest = load_model(destination)
            self.assertEqual(manifest, loaded.manifest)
            evaluation = evaluate_on_population(restored, self.data)
            self.assertEqual(
                evaluation.metrics.per_tile_mae,
                self.result.validation.metrics.per_tile_mae,
            )

    def test_artifact_refuses_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "model"
            save_model_artifact(destination, self.result.model, dict(self.manifest))
            with self.assertRaises(FileExistsError):
                save_model_artifact(destination, self.result.model, dict(self.manifest))

    def test_tampered_weights_fail_closed_on_load(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "model"
            save_model_artifact(destination, self.result.model, dict(self.manifest))
            weights = destination / WEIGHTS_FILENAME
            weights.write_bytes(weights.read_bytes() + b"\x00")
            with self.assertRaises(Stage3ArtifactError):
                load_model(destination)

    def test_manifest_rejects_a_checkpoint_outside_the_selection_rule(self):
        history = [
            {"epoch": 1, "train_mse": 1.0, "validation_mae": 0.5},
            {"epoch": 2, "train_mse": 0.9, "validation_mae": 0.4},
        ]
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(Stage3ArtifactError):
                save_model_artifact(
                    Path(name) / "model",
                    self.result.model,
                    dict(self.manifest)
                    | {"loss_history": history, "selected_epoch": 1},
                )

    def test_manifest_records_execution_source_revisions(self):
        runtime = self.manifest["runtime"]
        self.assertEqual(runtime["device"], "cpu")
        self.assertIs(runtime["cuda_available"], False)
        self.assertEqual(
            set(runtime["execution_source_revisions"]),
            {"lisjong", "lisjong_engine", "lisjong_arena"},
        )
        self.assertIn("execution_source_revisions_fully_resolved", runtime)

    def test_manifest_rejects_a_non_cpu_or_multi_thread_runtime(self):
        for override in (
            {"runtime": dict(self.manifest["runtime"]) | {"cuda_available": True}},
            {"runtime": dict(self.manifest["runtime"]) | {"torch_thread_count": 4}},
        ):
            with tempfile.TemporaryDirectory() as name:
                with self.assertRaises(Stage3ArtifactError):
                    save_model_artifact(
                        Path(name) / "model",
                        self.result.model,
                        dict(self.manifest) | override,
                    )

    def test_manifest_rejects_locked_contract_tampering(self):
        """schema versionを保ったままlocked semanticsを書き換える改変を拒否する。"""
        overrides = (
            {"reference_arm_id": "other-reference-arm"},
            {"feature_dimension": 918},
            {"checkpoint_selection_rule": "lowest train MSE"},
            {"self_rollout_failure_count": 1},
            {
                "training_config": dict(self.manifest["training_config"])
                | {"max_epochs": 41}
            },
            {
                "training_config": dict(self.manifest["training_config"])
                | {"deterministic_algorithms": False}
            },
            {
                "runtime": dict(self.manifest["runtime"])
                | {
                    "execution_source_revisions_fully_resolved": not self.manifest[
                        "runtime"
                    ]["execution_source_revisions_fully_resolved"]
                }
            },
            {
                "runtime": dict(self.manifest["runtime"])
                | {"execution_source_revisions": {"lisjong": "0" * 40}}
            },
            {
                "runtime": dict(self.manifest["runtime"])
                | {
                    "execution_source_revisions": dict(
                        self.manifest["runtime"]["execution_source_revisions"]
                    )
                    | {"lisjong": "not-a-sha"}
                }
            },
        )
        for override in overrides:
            with (
                self.subTest(override=sorted(override)),
                self.assertRaises(Stage3ArtifactError),
            ):
                validate_model_manifest(dict(self.manifest) | override)

    def test_manifest_uses_the_phase8_checkpoint_tie_tolerance(self):
        """1e-12以内のtieは改善扱いしない。単純なmin()とは選択が異なる。"""
        history = [
            {"epoch": 1, "train_mse": 1.0, "validation_mae": 0.5},
            {"epoch": 2, "train_mse": 0.9, "validation_mae": 0.5 - 5e-13},
        ]
        base = dict(self.manifest) | {"loss_history": history}
        # min()なら epoch 2 だが、Phase 8 の規則では epoch 1 が選ばれる。
        self.assertEqual(
            validate_model_manifest(base | {"selected_epoch": 1})["selected_epoch"], 1
        )
        with self.assertRaises(Stage3ArtifactError):
            validate_model_manifest(base | {"selected_epoch": 2})

    def test_manifest_rejects_inconsistent_within_population_validation(self):
        record = dict(self.manifest["within_population_validation"])
        with self.assertRaises(Stage3ArtifactError):
            validate_model_manifest(
                dict(self.manifest)
                | {
                    "within_population_validation": record
                    | {"delta_mae_vs_conditional_uniform": 0.5}
                }
            )
        physical = dict(record["physical_consistency"]) | {
            "blocking_gate_passed": False
        }
        with self.assertRaises(Stage3ArtifactError):
            validate_model_manifest(
                dict(self.manifest)
                | {
                    "within_population_validation": record
                    | {"physical_consistency": physical}
                }
            )

    def test_manifest_checkpoint_rule_string_is_the_locked_one(self):
        self.assertEqual(
            self.manifest["checkpoint_selection_rule"], CHECKPOINT_SELECTION_RULE
        )

    def test_manifest_rejects_a_test_evaluated_artifact(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(Stage3ArtifactError):
                save_model_artifact(
                    Path(name) / "model",
                    self.result.model,
                    dict(self.manifest) | {"test_partition_evaluated": True},
                )


if __name__ == "__main__":
    unittest.main()
