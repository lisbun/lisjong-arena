"""Arena #150 Phase 10のtorch依存training / evaluation / artifact test。

synthetic 80-hanchan populationだけを使い、formalな80-hanchan generationへも
TEST inferenceへも到達しない。S16 / S32 / S64は同じdataset、同じcanonical
VALIDATION、同じPhase 8 inventoryを共有するnested TRAIN subsetであり、
training semanticsがscaleごとに変わらないことをここで固定する。
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _stage3_scale_learning_curve_fixtures import (
    lock_value,
    population_manifest,
    scale_artifacts,
)

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase8_sequential.model import S2_PARAMETER_COUNT
from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    load_model,
    load_model_artifact,
    model_manifest_without_weights,
    save_model_artifact,
)
from lisjong_arena.stage3_scale_learning_curve.experiment import (
    CANDIDATE,
    build_data,
    configure_torch_runtime,
    evaluate_on_population,
    scale_data,
    train_anchor_identities,
    train_scale,
    training_binding,
    validation_anchor_identities,
)
from lisjong_arena.stage3_scale_learning_curve.lock import (
    current_receipt,
    require_current_lock,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    OUTCOMES,
    SCALES,
    VALIDATION_SEEDS,
    ScaleError,
    train_seeds,
)
from lisjong_arena.stage3_scale_learning_curve.result import (
    assemble_result,
    evaluation_record,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class ExecutionReceiptTest(unittest.TestCase):
    """live runtimeを読むsource / runtime lockのfail closed path。

    `current_receipt()`はtorchとinstalled provenanceを実際に読むため、
    torch非依存testから切り離してここへ置く。
    """

    def test_an_execution_revision_mismatch_is_never_allowed_silently(self):
        with self.assertRaises(ScaleError):
            current_receipt(arena_revision="e" * 40, seed_audit="audit")

    def test_a_foreign_lock_cannot_drive_this_runtime(self):
        with self.assertRaises(ScaleError):
            require_current_lock(lock_value())


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class ScaleNestedTrainingTest(unittest.TestCase):
    """nested TRAIN subsetと共有training semantics。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name)
        raw, dataset, _report = scale_artifacts(root)
        cls.full = build_data(raw, dataset)
        cls.results = {scale: train_scale(cls.full, scale) for scale in SCALES}

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_every_scale_trains_the_same_locked_s2_family_and_budget(self):
        for scale in SCALES:
            result = self.results[scale]
            self.assertIs(result.candidate, CANDIDATE)
            self.assertEqual(result.parameter_count, S2_PARAMETER_COUNT)
            self.assertEqual(result.parameter_count, 459_080)
            self.assertEqual(result.config, FORMAL_TRAINING_CONFIG)

    def test_no_scale_gets_its_own_hyperparameters(self):
        configs = {self.results[scale].config for scale in SCALES}
        self.assertEqual(len(configs), 1)

    def test_the_bptt_policy_is_shared_across_the_scales(self):
        policies = {self.results[scale].bptt_policy for scale in SCALES}
        self.assertEqual(len(policies), 1)
        self.assertEqual(
            self.results["S16"].bptt_policy, self.full.inventory.bptt_policy
        )

    def test_train_sequences_are_nested_and_derived_from_seeds_only(self):
        by_scale = {
            scale: {
                sequence.key.game.game_seed
                for sequence in scale_data(self.full, scale).train_sequences
            }
            for scale in SCALES
        }
        for scale in SCALES:
            self.assertEqual(by_scale[scale], set(train_seeds(scale)))
        self.assertLess(by_scale["S16"], by_scale["S32"])
        self.assertLess(by_scale["S32"], by_scale["S64"])

    def test_train_anchors_are_nested_and_disjoint_from_validation(self):
        anchors = {
            scale: set(train_anchor_identities(self.full, scale)) for scale in SCALES
        }
        validation = set(validation_anchor_identities(self.full))
        self.assertLess(anchors["S16"], anchors["S32"])
        self.assertLess(anchors["S32"], anchors["S64"])
        for scale in SCALES:
            self.assertEqual(anchors[scale] & validation, set())

    def test_every_scale_shares_the_same_fixed_validation(self):
        for scale in SCALES:
            data = scale_data(self.full, scale)
            self.assertEqual(data.validation_sequences, self.full.validation_sequences)
            self.assertEqual(
                sorted(
                    {
                        sequence.key.game.game_seed
                        for sequence in data.validation_sequences
                    }
                ),
                list(VALIDATION_SEEDS),
            )
            self.assertTrue(
                all(
                    sequence.partition is DatasetPartition.VALIDATION
                    for sequence in data.validation_sequences
                )
            )

    def test_the_conditional_uniform_reference_is_identical_across_scales(self):
        baselines = {
            self.results[scale].validation.snapshot_metrics for scale in SCALES
        }
        self.assertEqual(len(baselines), 1)
        self.assertEqual(
            self.results["S16"].validation.snapshot_metrics,
            self.full.canonical_validation.snapshot_metrics,
        )

    def test_the_selected_checkpoint_matches_the_reported_validation(self):
        for scale in SCALES:
            result = self.results[scale]
            selected = result.history[result.selected_epoch - 1]
            self.assertEqual(selected.epoch, result.selected_epoch)
            self.assertEqual(
                selected.validation_mae, result.validation.metrics.per_tile_mae
            )

    def test_evaluation_is_the_serving_realistic_self_rollout(self):
        for scale in SCALES:
            result = self.results[scale]
            evaluation = evaluate_on_population(result.model, self.full)
            self.assertEqual(
                evaluation.metrics.per_tile_mae,
                result.validation.metrics.per_tile_mae,
            )

    def test_the_evaluation_record_covers_the_locked_diagnostics(self):
        result = self.results["S16"]
        record = evaluation_record(
            result.validation, self.full, result.inference_throughput
        )
        self.assertEqual(
            [row["game_seed"] for row in record["per_game"]], list(VALIDATION_SEEDS)
        )
        self.assertEqual(
            record["validation_anchor_identities"],
            validation_anchor_identities(self.full),
        )
        self.assertEqual(len(record["depth_diagnostics"]), 4)
        self.assertIn("blocking_gate_passed", record["physical_consistency"])
        self.assertGreater(record["inference"]["samples_per_second"], 0)
        self.assertEqual(record["inference"]["torch_thread_count"], 1)

    def test_scale_data_rejects_an_unknown_scale(self):
        with self.assertRaises(ScaleError):
            scale_data(self.full, "S128")


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class ScaleArtifactTest(unittest.TestCase):
    """model artifactのpersistenceとbinding。"""

    @classmethod
    def setUpClass(cls):
        configure_torch_runtime()
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.lock = lock_value()
        cls.population, raw, dataset = population_manifest(cls.root / "pop", cls.lock)
        cls.full = build_data(raw, dataset)
        cls.results = {scale: train_scale(cls.full, scale) for scale in SCALES}
        cls.loaded = {}
        for scale in SCALES:
            result = cls.results[scale]
            manifest = model_manifest_without_weights(
                scale=scale,
                lock=cls.lock,
                binding=training_binding(cls.full, scale, cls.lock["provenance"]),
                result=result,
                evaluation=evaluation_record(
                    result.validation, cls.full, result.inference_throughput
                ),
                training_cpu_seconds=1.0,
            )
            cls.loaded[scale] = save_model_artifact(
                cls.root / scale,
                result.model,
                manifest,
                cls.population,
                cls.lock,
            )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_artifact_stores_only_a_manifest_and_a_state_dict(self):
        for scale in SCALES:
            self.assertEqual(
                {path.name for path in (self.root / scale).iterdir()},
                {MANIFEST_FILENAME, WEIGHTS_FILENAME},
            )

    def test_the_artifact_round_trips_into_the_locked_model(self):
        model, manifest = load_model(self.root / "S16", self.population, self.lock)
        from lisjong_arena.phase8_sequential.model import parameter_count

        self.assertEqual(parameter_count(model), S2_PARAMETER_COUNT)
        self.assertEqual(manifest["scale"], "S16")
        self.assertEqual(
            manifest["training_lock"]["parameter_count"], S2_PARAMETER_COUNT
        )

    def test_the_artifact_binds_to_its_exact_train_subset(self):
        for scale in SCALES:
            manifest = self.loaded[scale].manifest
            self.assertEqual(
                manifest["subset"]["train_seeds"], list(train_seeds(scale))
            )
            self.assertEqual(
                manifest["train_anchor_identities"],
                train_anchor_identities(self.full, scale),
            )
            self.assertEqual(
                manifest["subset"]["dataset_identity"],
                self.population["dataset_identity"],
            )

    def test_an_existing_destination_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            save_model_artifact(
                self.root / "S16",
                self.results["S16"].model,
                {},
                self.population,
                self.lock,
            )

    def test_tampered_weights_are_rejected_on_load(self):
        destination = self.root / "tampered"
        result = self.results["S32"]
        save_model_artifact(
            destination,
            result.model,
            model_manifest_without_weights(
                scale="S32",
                lock=self.lock,
                binding=training_binding(self.full, "S32", self.lock["provenance"]),
                result=result,
                evaluation=evaluation_record(
                    result.validation, self.full, result.inference_throughput
                ),
                training_cpu_seconds=1.0,
            ),
            self.population,
            self.lock,
        )
        weights = destination / WEIGHTS_FILENAME
        weights.write_bytes(weights.read_bytes() + b"\x00")
        with self.assertRaises(ScaleError):
            load_model_artifact(destination, self.population, self.lock)

    def test_non_canonical_manifest_bytes_are_rejected_on_load(self):
        destination = self.root / "noncanonical"
        result = self.results["S64"]
        loaded = save_model_artifact(
            destination,
            result.model,
            model_manifest_without_weights(
                scale="S64",
                lock=self.lock,
                binding=training_binding(self.full, "S64", self.lock["provenance"]),
                result=result,
                evaluation=evaluation_record(
                    result.validation, self.full, result.inference_throughput
                ),
                training_cpu_seconds=1.0,
            ),
            self.population,
            self.lock,
        )
        (destination / MANIFEST_FILENAME).write_text(
            json.dumps(loaded.manifest, indent=2), encoding="utf-8"
        )
        with self.assertRaises(ScaleError):
            load_model_artifact(destination, self.population, self.lock)

    def test_a_model_cannot_be_relabelled_as_another_scale(self):
        destination = self.root / "relabelled"
        destination.mkdir()
        source = self.root / "S16"
        manifest = json.loads((source / MANIFEST_FILENAME).read_bytes())
        manifest["scale"] = "S32"
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        (destination / WEIGHTS_FILENAME).write_bytes(
            (source / WEIGHTS_FILENAME).read_bytes()
        )
        with self.assertRaises(ScaleError):
            load_model_artifact(destination, self.population, self.lock)

    def test_the_end_to_end_curve_produces_an_exhaustive_outcome(self):
        models = {scale: self.loaded[scale].manifest for scale in SCALES}
        value = assemble_result(self.population, models, self.lock)
        self.assertIn(value["outcome"], OUTCOMES)
        self.assertEqual(len(value["comparisons"]), 3)
        self.assertTrue(all(value["gates"].values()))
        for row in value["comparisons"]:
            self.assertEqual(row["hanchan"], 16)
            self.assertLessEqual(row["interval_lower"], row["interval_upper"])
        from lisjong_arena.stage3_scale_learning_curve.result import validate_result

        validate_result(value, self.lock)


if __name__ == "__main__":
    unittest.main()
