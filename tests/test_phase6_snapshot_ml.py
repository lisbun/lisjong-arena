"""PyTorch-specific Phase 6 model, constraint, training, and artifact tests."""

import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _phase4_raw_corpus_fixtures import direct_phase2_sample, fixture_corpus

from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.baseline import predict_dataset_baseline
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountPrediction,
    ExpectedCountPredictionRow,
    evaluate_baseline_predictions,
    evaluate_expected_count_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.training import LOCKED_DATASET_IDENTITY

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Phase 6 ml extra")
class Phase6SnapshotMlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global torch
        import torch

    def test_constraint_marginals_zero_axes_gradients_and_rejections(self):
        from lisjong_arena.phase6_snapshot.constraint import (
            ConstraintConvergenceError,
            constrain_allocation,
        )

        logits = torch.randn(2, 4, 34, requires_grad=True)
        columns = torch.full((2, 34), 4.0)
        columns[:, 0] = 0
        rows = torch.tensor([[13.0, 13.0, 13.0, 93.0]] * 2)
        constrained = constrain_allocation(logits, rows, columns)
        allocation = constrained.allocation
        self.assertTrue(bool((allocation >= 0).all()))
        self.assertTrue(bool((allocation[:, :, 0] == 0).all()))
        self.assertTrue(
            torch.allclose(
                allocation.sum(-1), rows.to(torch.float64), atol=1e-6, rtol=0
            )
        )
        self.assertTrue(
            torch.allclose(
                allocation.sum(-2), columns.to(torch.float64), atol=1e-6, rtol=0
            )
        )
        allocation[:, :3].square().mean().backward()
        self.assertTrue(bool(torch.isfinite(logits.grad).all()))

        zero_rows = torch.tensor([[0.0, 13.0, 13.0, 106.0]])
        zero_result = constrain_allocation(
            torch.zeros(1, 4, 34), zero_rows, columns[:1]
        )
        self.assertTrue(bool((zero_result.allocation[:, 0] == 0).all()))
        with self.assertRaisesRegex(ValueError, "finite"):
            constrain_allocation(
                torch.full((1, 4, 34), float("nan")), rows[:1], columns[:1]
            )
        with self.assertRaisesRegex(ValueError, "total mass"):
            constrain_allocation(torch.zeros(1, 4, 34), rows[:1], columns[:1] + 1)
        with self.assertRaises(ConstraintConvergenceError):
            constrain_allocation(
                torch.randn(1, 4, 34),
                rows[:1],
                columns[:1],
                max_iterations=1,
                residual_tolerance=0,
            )

    def test_locked_model_shape_and_parameter_count(self):
        from lisjong_arena.phase6_snapshot.model import create_model, parameter_count
        from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM

        torch.manual_seed(0)
        model = create_model()
        result = model(
            torch.zeros(2, FEATURE_DIM),
            torch.tensor([[13.0, 13.0, 13.0, 97.0]] * 2),
            torch.full((2, 34), 4.0),
        )
        self.assertEqual(result.allocation.shape, (2, 4, 34))
        self.assertEqual(parameter_count(model), 134_856)

    def test_test_partition_is_rejected_before_model_materialization(self):
        from lisjong_arena.phase6_snapshot.training import build_phase6_example

        with tempfile.TemporaryDirectory() as temporary:
            raw = save_raw_corpus(fixture_corpus(), Path(temporary) / "raw")
            dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
            sample = resolve_training_samples(dataset, raw)[0]
            self.assertIs(dataset.examples[0].partition, DatasetPartition.TEST)
            with self.assertRaisesRegex(ValueError, "rejects TEST"):
                build_phase6_example(dataset.examples[0], sample)

    def test_training_is_reproducible_train_updates_and_validation_has_no_gradient(
        self,
    ):
        from lisjong_arena.phase6_snapshot.model import create_model
        from lisjong_arena.phase6_snapshot.training import (
            Phase6Example,
            TrainingConfig,
            TrainValidationData,
            build_phase6_example,
            train_phase6_model,
        )

        sample = direct_phase2_sample()
        with tempfile.TemporaryDirectory() as temporary:
            raw = save_raw_corpus(fixture_corpus(), Path(temporary) / "raw")
            dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
            reference = dataset.examples[0]
        train = build_phase6_example(
            replace(reference, partition=DatasetPartition.TRAIN), sample
        )
        validation = build_phase6_example(
            replace(reference, partition=DatasetPartition.VALIDATION), sample
        )
        config = TrainingConfig(batch_size=1, max_epochs=1, patience=1)
        data = TrainValidationData((train,), (validation,))
        first = train_phase6_model(
            data, dataset_identity=LOCKED_DATASET_IDENTITY, config=config
        )
        second = train_phase6_model(
            data, dataset_identity=LOCKED_DATASET_IDENTITY, config=config
        )
        for name, value in first.model.state_dict().items():
            self.assertTrue(torch.equal(value, second.model.state_dict()[name]))

        torch.manual_seed(0)
        initial = create_model().state_dict()
        self.assertTrue(
            any(
                not torch.equal(value, first.model.state_dict()[name])
                for name, value in initial.items()
            )
        )
        changed_validation = replace(
            validation,
            target=tuple(
                tuple(4.0 - cell for cell in row) for row in validation.target
            ),
        )
        changed = train_phase6_model(
            TrainValidationData((train,), (changed_validation,)),
            dataset_identity=LOCKED_DATASET_IDENTITY,
            config=config,
        )
        for name, value in first.model.state_dict().items():
            self.assertTrue(torch.equal(value, changed.model.state_dict()[name]))
        self.assertIsInstance(train, Phase6Example)

    def test_artifact_roundtrip_digest_overwrite_and_logical_identity(self):
        from lisjong_arena.phase6_snapshot.artifact import (
            ARTIFACT_SCHEMA_VERSION,
            Phase6ArtifactError,
            artifact_logical_identity,
            load_model_artifact,
            save_model_artifact,
            validate_manifest,
        )
        from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
        from lisjong_arena.phase6_snapshot.model import create_model, parameter_count
        from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM

        model = create_model()
        manifest = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "raw_corpus_identity": "a" * 64,
            "dataset_identity": "b" * 64,
            "dataset_source_revisions": {"arena": "c" * 40},
            "training_source_revisions": {"arena": "d" * 40},
            "feature_semantics_id": FEATURE_SEMANTICS_ID,
            "feature_dimension": FEATURE_DIM,
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
        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first"
            second_path = Path(temporary) / "second"
            first = save_model_artifact(first_path, model, manifest)
            second = save_model_artifact(second_path, model, manifest)
            self.assertEqual(
                artifact_logical_identity(first.manifest),
                artifact_logical_identity(second.manifest),
            )
            for name, value in model.state_dict().items():
                self.assertTrue(torch.equal(value, first.model.state_dict()[name]))
            with self.assertRaises(FileExistsError):
                save_model_artifact(first_path, model, manifest)
            with self.assertRaisesRegex(Phase6ArtifactError, "fields"):
                validate_manifest({})
            weights = first_path / "weights.pt"
            data = bytearray(weights.read_bytes())
            data[-1] ^= 1
            weights.write_bytes(data)
            with self.assertRaisesRegex(Phase6ArtifactError, "SHA-256"):
                load_model_artifact(first_path)

    def test_phase5_and_common_expected_count_metric_values_are_equal(self):
        from lisjong.belief import SCALE, wind_index

        with tempfile.TemporaryDirectory() as temporary:
            raw = save_raw_corpus(fixture_corpus(), Path(temporary) / "raw")
            dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
            samples = resolve_training_samples(dataset, raw)
            predictions = predict_dataset_baseline(dataset.examples, samples)
            baseline = evaluate_baseline_predictions(dataset, samples, predictions)
            expected_predictions = tuple(
                ExpectedCountPrediction(
                    reference,
                    tuple(
                        ExpectedCountPredictionRow(
                            truth.identity.wind,
                            tuple(
                                value / SCALE
                                for value in prediction.belief.hands[
                                    wind_index(truth.identity.wind)
                                ].expected_count_raw
                            ),
                            prediction.concealed_slot_counts_by_wind[
                                wind_index(truth.identity.wind)
                            ],
                        )
                        for truth in sample.labels.expected_counts
                    ),
                )
                for reference, sample, prediction in zip(
                    dataset.examples, samples, predictions, strict=True
                )
            )
            common = evaluate_expected_count_predictions(
                dataset.dataset_identity,
                dataset.examples,
                samples,
                expected_predictions,
            )
            self.assertEqual(
                baseline.partition_metrics[0].metrics.expected_count,
                common.partition_metrics[0].metrics,
            )


if __name__ == "__main__":
    unittest.main()
