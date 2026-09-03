"""Stage 3 Entry Gate torch-dependent training, evaluation, and artifact tests.

synthetic Stage 3 populationだけを使い、formal pilot generationやTEST inference
へは到達しない。
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _stage3_entry_gate_fixtures import STAGE3_BASE_SEEDS, stage3_population_artifacts

from lisjong_arena.phase8_sequential.model import S2_PARAMETER_COUNT
from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_entry_gate.artifact import (
    MANIFEST_FILENAME,
    RESULT_SCHEMA_VERSION,
    WEIGHTS_FILENAME,
    Stage3ArtifactError,
    execution_runtime_value,
    load_model,
    load_result,
    model_manifest_without_weights,
    save_model_artifact,
    save_result,
)
from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    build_population_data,
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

    def test_manifest_rejects_a_test_evaluated_artifact(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(Stage3ArtifactError):
                save_model_artifact(
                    Path(name) / "model",
                    self.result.model,
                    dict(self.manifest) | {"test_partition_evaluated": True},
                )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Stage3ResultArtifactTest(unittest.TestCase):
    def _value(self) -> dict[str, object]:
        return {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "pilot_role": "development-only",
            "cross_population_matrix": [],
            "test_partition_evaluated": False,
        }

    def test_result_binds_a_logical_identity_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "result.json"
            save_result(destination, self._value())
            loaded = load_result(destination)
            self.assertEqual(len(loaded["result_identity"]), 64)
            with self.assertRaises(FileExistsError):
                save_result(destination, self._value())

    def test_tampered_result_fails_closed_on_load(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "result.json"
            save_result(destination, self._value())
            value = json.loads(destination.read_text())
            value["pilot_role"] = "formal"
            destination.write_text(
                json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            with self.assertRaises(Stage3ArtifactError):
                load_result(destination)

    def test_result_schema_version_is_required(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(Stage3ArtifactError):
                save_result(
                    Path(name) / "result.json",
                    replace_schema(self._value(), "other"),
                )


def replace_schema(value: dict[str, object], version: str) -> dict[str, object]:
    return dict(value) | {"result_schema_version": version}


if __name__ == "__main__":
    unittest.main()
