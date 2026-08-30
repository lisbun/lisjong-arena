"""Synthetic PyTorch checks for the Phase 7 frozen-artifact inference boundary."""

import hashlib
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _phase4_raw_corpus_fixtures import fixture_corpus

from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    Phase6ArtifactError,
    artifact_logical_identity,
    save_model_artifact,
)
from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase6_snapshot.model import create_model, parameter_count
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM
from lisjong_arena.phase6_snapshot.training import predict_snapshot_examples
from lisjong_arena.phase7_snapshot_test.evaluation import (
    FrozenArtifactSpec,
    build_phase7_test_example,
    verify_frozen_artifact,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _manifest(model) -> dict[str, object]:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "raw_corpus_identity": "a" * 64,
        "dataset_identity": "b" * 64,
        "dataset_source_revisions": {"arena": "c" * 40},
        "training_source_revisions": {"arena": "d" * 40},
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_dimension": FEATURE_DIM,
        "feature_coverage": {"train": {"samples": 1}},
        "tensorization": {"normalization": "fixed-semantic"},
        "model": {"family": "locked"},
        "parameter_count": parameter_count(model),
        "constraint": {"algorithm": "log-domain-ipfp"},
        "training": {"seed": 0},
        "runtime": {"torch": torch.__version__},
        "selected_epoch": 1,
        "loss_history": [{"epoch": 1, "train_mse": 1.0, "validation_mse": 1.0}],
        "train_metrics": {"mse": 1.0},
        "validation_metrics": {"mse": 1.0},
        "training_wall_clock_seconds": 1.0,
        "peak_process_ram_bytes": None,
        "inference_throughput": {"samples_per_second": 1.0},
        "constraint_maximum_residual": 0.0,
        "constraint_non_convergence_count": 0,
        "test_partition_evaluated": False,
    }


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Phase7SnapshotMlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global torch
        import torch

    def test_exact_frozen_artifact_spec_passes_and_any_mismatch_fails_closed(self):
        model = create_model()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "phase6"
            loaded = save_model_artifact(path, model, _manifest(model))
            manifest_sha = hashlib.sha256(
                (path / MANIFEST_FILENAME).read_bytes()
            ).hexdigest()
            spec = FrozenArtifactSpec(
                weights_sha256=loaded.manifest["weights_sha256"],
                artifact_logical_identity=artifact_logical_identity(loaded.manifest),
                manifest_sha256=manifest_sha,
                raw_corpus_identity=loaded.manifest["raw_corpus_identity"],
                dataset_identity=loaded.manifest["dataset_identity"],
                parameter_count=loaded.manifest["parameter_count"],
            )
            verified, actual_manifest_sha = verify_frozen_artifact(path, spec=spec)
            self.assertEqual(actual_manifest_sha, manifest_sha)
            for name, value in model.state_dict().items():
                self.assertTrue(torch.equal(value, verified.model.state_dict()[name]))
            with self.assertRaisesRegex(RuntimeError, "artifact logical identity"):
                verify_frozen_artifact(
                    path,
                    spec=replace(spec, artifact_logical_identity="0" * 64),
                )
            with self.assertRaisesRegex(RuntimeError, "manifest SHA-256"):
                verify_frozen_artifact(
                    path, spec=replace(spec, manifest_sha256="0" * 64)
                )
            invalid_manifest = _manifest(model)
            invalid_manifest["test_partition_evaluated"] = True
            with self.assertRaisesRegex(Phase6ArtifactError, "seal TEST"):
                save_model_artifact(
                    Path(temporary) / "invalid-phase6", model, invalid_manifest
                )

    def test_test_only_inference_is_cpu_eval_no_grad_on_synthetic_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = save_raw_corpus(fixture_corpus(), Path(temporary) / "raw")
            dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
            samples = resolve_training_samples(dataset, raw)
            materialized = tuple(
                build_phase7_test_example(example, sample)
                for example, sample in zip(dataset.examples, samples, strict=True)
            )
            model = create_model()
            model.train()
            predictions, maximum_residual = predict_snapshot_examples(
                model, materialized, batch_size=3
            )
            self.assertEqual(len(predictions), len(materialized))
            self.assertFalse(model.training)
            self.assertLessEqual(maximum_residual, 1e-6)
            self.assertTrue(
                all(parameter.device.type == "cpu" for parameter in model.parameters())
            )
            self.assertTrue(
                all(
                    isinstance(row.values[0], float)
                    for prediction in predictions
                    for row in prediction.rows
                )
            )


if __name__ == "__main__":
    unittest.main()
