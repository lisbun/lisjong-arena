"""Arena #148 torch-dependent training, evaluation, and artifact tests。

synthetic mix pilot armだけを使い、formal 72-hanchan generationやTEST inference
へは到達しない。3 armは同じlocked seedsを共有し、内容だけが違うsynthetic
populationとして表現する。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from _stage3_mix_pilot_fixtures import MIX_BASE_SEEDS, mix_artifacts

from lisjong_arena.phase8_sequential.model import S2_PARAMETER_COUNT
from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_entry_gate.artifact import CHECKPOINT_SELECTION_RULE
from lisjong_arena.stage3_mix_pilot.artifact import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    MixArtifactError,
    execution_runtime_value,
    load_model,
    model_manifest_without_weights,
    save_model_artifact,
    validate_model_manifest,
    validate_result_value,
)
from lisjong_arena.stage3_mix_pilot.comparison import compare_against_control
from lisjong_arena.stage3_mix_pilot.experiment import (
    CANDIDATE,
    build_arm_data,
    configure_torch_runtime,
    evaluate_on_population,
    evaluation_value,
    inventory_summary,
    train_population_candidate,
)
from lisjong_arena.stage3_mix_pilot.population import mix_arm_plan
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    CLEAR_REGRESSION,
    CONTROL_ARM_ID,
    NO_CLEAR_REGRESSION,
    PILOT_ROLE,
    RESULT_SCHEMA_VERSION,
    SELECTION_RULE,
    VALIDATION_SEEDS,
)
from lisjong_arena.stage3_mix_pilot.result import (
    arm_manifest_view,
    classify,
    selected_recipe,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

_ARM_SOURCES = {
    "A": {"kan": False, "base_seed": MIX_BASE_SEEDS[0]},
    "B": {"kan": True, "base_seed": MIX_BASE_SEEDS[0]},
    "C": {"kan": True, "base_seed": MIX_BASE_SEEDS[1]},
}


def _arm_data(root: Path, arm_id: str):
    persisted_raw, dataset = mix_artifacts(root, **_ARM_SOURCES[arm_id])
    return build_arm_data(
        arm_id=arm_id,
        population_identity=mix_arm_plan(arm_id).population_identity,
        persisted_raw=persisted_raw,
        dataset=dataset,
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class MixTrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.control = _arm_data(root / "A", "A")
        cls.candidate = _arm_data(root / "B", "B")
        cls.result = train_population_candidate(cls.control)

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

    def test_every_arm_trains_under_the_identical_fixed_config(self):
        other = train_population_candidate(self.candidate)
        self.assertEqual(other.config, self.result.config)
        self.assertEqual(other.parameter_count, self.result.parameter_count)

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
            self.control.canonical_validation.snapshot_metrics,
        )
        self.assertAlmostEqual(
            validation.delta_mae,
            validation.snapshot_metrics.per_tile_mae - validation.metrics.per_tile_mae,
            places=12,
        )

    def test_the_validation_unit_is_the_whole_locked_hanchan_population(self):
        cell = evaluation_value(
            evaluate_on_population(self.result.model, self.control), self.control
        )
        self.assertEqual(
            tuple(row["game_seed"] for row in cell["per_game"]), VALIDATION_SEEDS
        )

    def test_physical_validity_fields_are_reported(self):
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

    def test_cross_population_evaluation_uses_only_the_target_population(self):
        cell = evaluation_value(
            evaluate_on_population(self.result.model, self.candidate), self.candidate
        )
        self.assertEqual(cell["validation_population_id"], "B")
        self.assertEqual(
            cell["validation_dataset_identity"], self.candidate.dataset_identity
        )
        self.assertNotEqual(
            cell["validation_dataset_identity"], self.control.dataset_identity
        )
        self.assertEqual(
            cell["conditional_uniform_validation_mae"],
            self.candidate.baseline_metrics.per_tile_mae,
        )

    def test_model_output_is_finite_on_every_evaluation_population(self):
        from math import isfinite

        for data in (self.control, self.candidate):
            cell = evaluation_value(
                evaluate_on_population(self.result.model, data), data
            )
            self.assertTrue(isfinite(cell["sequential_validation_mae"]))
            self.assertTrue(
                all(isfinite(row["candidate_mae"]) for row in cell["per_game"])
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class MixMatrixTest(unittest.TestCase):
    """3 model x 3 evaluation populationのend-to-end matrixとpaired comparison。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.data = {arm_id: _arm_data(root / arm_id, arm_id) for arm_id in ARM_IDS}
        cls.models = {
            arm_id: train_population_candidate(cls.data[arm_id]).model
            for arm_id in ARM_IDS
        }
        cls.cells = []
        for training_id in ARM_IDS:
            for validation_id in ARM_IDS:
                data = cls.data[validation_id]
                cell = evaluation_value(
                    evaluate_on_population(cls.models[training_id], data), data
                )
                cell["training_population_id"] = training_id
                cell["training_population_identity"] = cls.data[
                    training_id
                ].population_identity
                cls.cells.append(cell)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_matrix_covers_every_train_validation_pair_exactly_once(self):
        pairs = {
            (cell["training_population_id"], cell["validation_population_id"])
            for cell in self.cells
        }
        self.assertEqual(len(self.cells), 9)
        self.assertEqual(len(pairs), 9)

    def test_arms_have_distinct_dataset_identities(self):
        identities = {value.dataset_identity for value in self.data.values()}
        self.assertEqual(len(identities), len(ARM_IDS))

    def test_paired_comparison_runs_on_every_evaluation_population(self):
        by_pair = {
            (cell["training_population_id"], cell["validation_population_id"]): cell
            for cell in self.cells
        }
        rows = [
            compare_against_control(
                candidate_arm_id=candidate_id,
                validation_arm_id=validation_id,
                control_cell=by_pair[(CONTROL_ARM_ID, validation_id)],
                candidate_cell=by_pair[(candidate_id, validation_id)],
            )
            for candidate_id in ARM_IDS
            if candidate_id != CONTROL_ARM_ID
            for validation_id in ARM_IDS
        ]
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertEqual(row["hanchan"], len(VALIDATION_SEEDS))
            self.assertIn(
                row["classification"], (CLEAR_REGRESSION, NO_CLEAR_REGRESSION)
            )
            self.assertLessEqual(row["interval_lower"], row["interval_upper"])
            self.assertEqual(
                row["classification"],
                CLEAR_REGRESSION if row["interval_upper"] < 0 else NO_CLEAR_REGRESSION,
            )

    def test_a_complete_result_value_validates_against_the_artifact_contract(self):
        """実測matrixから組んだresultが、再導出contractまで含めて通ることを固定する。

        arm entryはoutcomeを再導出できるだけのevidenceを持ち、outcome / gates /
        selected_recipe / paired comparison はすべてそのevidenceから導出する。
        """
        from _stage3_mix_pilot_fixtures import arm_entry_value

        by_pair = {
            (cell["training_population_id"], cell["validation_population_id"]): cell
            for cell in self.cells
        }
        comparisons = [
            compare_against_control(
                candidate_arm_id=candidate_id,
                validation_arm_id=validation_id,
                control_cell=by_pair[(CONTROL_ARM_ID, validation_id)],
                candidate_cell=by_pair[(candidate_id, validation_id)],
            )
            for candidate_id in ARM_IDS
            if candidate_id != CONTROL_ARM_ID
            for validation_id in ARM_IDS
        ]
        arms = {}
        for arm_id in ARM_IDS:
            entry = arm_entry_value(arm_id)
            entry["population_identity"] = self.data[arm_id].population_identity
            entry["raw_corpus_identity"] = self.data[arm_id].raw_corpus_identity
            entry["dataset_identity"] = self.data[arm_id].dataset_identity
            arms[arm_id] = entry
        views = {arm_id: arm_manifest_view(arm_id, arms[arm_id]) for arm_id in ARM_IDS}
        outcome, reasons, gates = classify(views, self.cells, comparisons)
        value = {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "pilot_role": PILOT_ROLE,
            "candidate": CANDIDATE.value,
            "reference_arm_id": ("stage3-conditional-uniform-reference-arm-v1"),
            "retry_rule": "test",
            "selection_rule": SELECTION_RULE,
            "evaluation_runtime": execution_runtime_value(),
            "arms": arms,
            "cross_population_matrix": self.cells,
            "paired_comparisons": comparisons,
            "gates": gates,
            "outcome": outcome,
            "outcome_reasons": list(reasons),
            "selected_recipe": selected_recipe(outcome, views),
            "test_partition_evaluated": False,
            "accumulated_with_historical_evidence": False,
        }
        validate_result_value(value)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class MixModelArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        cls.data = _arm_data(root / "B", "B")
        cls.result = train_population_candidate(cls.data)
        cls.manifest = model_manifest_without_weights(
            arm_id=cls.data.population_id,
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
            self.assertEqual(loaded.manifest["training_arm_id"], "B")
            self.assertEqual(
                loaded.manifest["training_population_identity"],
                self.data.population_identity,
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
            with self.assertRaises(MixArtifactError):
                load_model(destination)

    def test_manifest_checkpoint_rule_string_is_the_locked_one(self):
        self.assertEqual(
            self.manifest["checkpoint_selection_rule"], CHECKPOINT_SELECTION_RULE
        )

    def test_manifest_uses_the_phase8_checkpoint_tie_tolerance(self):
        """1e-12以内のtieは改善扱いしない。単純なmin()とは選択が異なる。"""
        history = [
            {"epoch": 1, "train_mse": 1.0, "validation_mae": 0.5},
            {"epoch": 2, "train_mse": 0.9, "validation_mae": 0.5 - 5e-13},
        ]
        base = dict(self.manifest) | {"loss_history": history}
        self.assertEqual(
            validate_model_manifest(base | {"selected_epoch": 1})["selected_epoch"], 1
        )
        with self.assertRaises(MixArtifactError):
            validate_model_manifest(base | {"selected_epoch": 2})

    def test_manifest_rejects_locked_contract_tampering(self):
        """schema versionを保ったままlocked semanticsを書き換える改変を拒否する。"""
        overrides = (
            {"reference_arm_id": "other-reference-arm"},
            {"feature_dimension": 918},
            {"checkpoint_selection_rule": "lowest train MSE"},
            {"self_rollout_failure_count": 1},
            {"training_arm_id": "D"},
            {"test_partition_evaluated": True},
            {
                "training_config": dict(self.manifest["training_config"])
                | {"max_epochs": 41}
            },
            {
                "training_config": dict(self.manifest["training_config"])
                | {"deterministic_algorithms": False}
            },
            {"runtime": dict(self.manifest["runtime"]) | {"cuda_available": True}},
            {"runtime": dict(self.manifest["runtime"]) | {"torch_thread_count": 4}},
            {
                "runtime": dict(self.manifest["runtime"])
                | {
                    "execution_source_revisions_fully_resolved": not self.manifest[
                        "runtime"
                    ]["execution_source_revisions_fully_resolved"]
                }
            },
        )
        for override in overrides:
            with (
                self.subTest(override=sorted(override)),
                self.assertRaises(MixArtifactError),
            ):
                validate_model_manifest(dict(self.manifest) | override)

    def test_manifest_rejects_inconsistent_within_arm_validation(self):
        record = dict(self.manifest["within_arm_validation"])
        with self.assertRaises(MixArtifactError):
            validate_model_manifest(
                dict(self.manifest)
                | {
                    "within_arm_validation": record
                    | {"delta_mae_vs_conditional_uniform": 0.5}
                }
            )
        physical = dict(record["physical_consistency"]) | {
            "blocking_gate_passed": False
        }
        with self.assertRaises(MixArtifactError):
            validate_model_manifest(
                dict(self.manifest)
                | {"within_arm_validation": record | {"physical_consistency": physical}}
            )

    def test_manifest_records_execution_source_revisions(self):
        runtime = self.manifest["runtime"]
        self.assertEqual(runtime["device"], "cpu")
        self.assertIs(runtime["cuda_available"], False)
        self.assertEqual(
            set(runtime["execution_source_revisions"]),
            {"lisjong", "lisjong_engine", "lisjong_arena"},
        )


if __name__ == "__main__":
    unittest.main()
