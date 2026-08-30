"""Synthetic PyTorch checks for Phase 8 recurrence, training, and artifacts."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _phase4_raw_corpus_fixtures import fixture_corpus
from lisjong.policy_contract import Wind

from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    evaluate_expected_count_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.constraint import constrain_allocation
from lisjong_arena.phase6_snapshot.training import materialize_snapshot_example
from lisjong_arena.phase8_sequential.protocol import (
    DEPTH_BUCKETS,
    BpttMode,
    BpttPolicy,
    Candidate,
    Phase8Sequence,
    SequenceKey,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Phase8SequentialMlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global torch
        import torch

    def _examples(self):
        temporary = tempfile.TemporaryDirectory()
        raw = save_raw_corpus(fixture_corpus(), Path(temporary.name) / "raw")
        dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
        samples = resolve_training_samples(dataset, raw)
        return temporary, dataset, samples

    def _two_step_sequence(self, *, swap_second_rows: bool = False):
        temporary, dataset, samples = self._examples()
        reference = replace(dataset.examples[0], partition=DatasetPartition.TRAIN)
        first = materialize_snapshot_example(reference, samples[0])
        second = replace(
            first,
            example=replace(reference, checkpoint_index=1, anchor_index=1),
            sample=SimpleNamespace(
                anchor=replace(first.sample.anchor, anchor_index=1),
                labels=first.sample.labels,
            ),
        )
        if swap_second_rows:
            order = (2, 0, 1)
            second = replace(
                second,
                opponent_winds=tuple(second.opponent_winds[index] for index in order),
                row_marginals=tuple(second.row_marginals[index] for index in order)
                + (second.row_marginals[3],),
                target=tuple(second.target[index] for index in order),
            )
        sequence = Phase8Sequence(
            SequenceKey(reference.game, reference.round_index, reference.viewer_seat),
            DatasetPartition.TRAIN,
            (first, second),
        )
        return temporary, sequence

    def test_models_have_exact_fixed_shapes_and_reuse_phase6_constraint(self):
        from lisjong_arena.phase6_snapshot import constraint as phase6_constraint
        from lisjong_arena.phase8_sequential import model as phase8_model
        from lisjong_arena.phase8_sequential.model import (
            PREVIOUS_BELIEF_DIM,
            S2_LATENT_DIM,
            create_model,
            parameter_count,
        )

        self.assertIs(
            phase8_model.constrain_allocation, phase6_constraint.constrain_allocation
        )
        features = torch.zeros((2, 919))
        previous = torch.zeros((2, PREVIOUS_BELIEF_DIM))
        rows = torch.tensor([[13.0, 13.0, 13.0, 97.0]] * 2)
        columns = torch.full((2, 34), 4.0)
        s1 = create_model(Candidate.S1)
        s1_result = s1(features, previous, rows, columns)
        self.assertEqual(s1_result.allocation.shape, (2, 4, 34))
        self.assertEqual(parameter_count(s1), 147_912)
        s2 = create_model(Candidate.S2)
        s2_result, latent = s2(
            features,
            previous,
            torch.zeros((2, S2_LATENT_DIM)),
            rows,
            columns,
        )
        self.assertEqual(s2_result.allocation.shape, (2, 4, 34))
        self.assertEqual(latent.shape, (2, S2_LATENT_DIM))
        self.assertEqual(parameter_count(s2), 459_080)

    def test_baseline_first_then_prior_prediction_with_wind_remap_and_no_labels(self):
        from lisjong_arena.phase8_sequential.rollout import self_rollout
        from lisjong_arena.phase8_sequential.state import baseline_initial_state

        class RecordingModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.previous = []

            def forward(self, _features, previous, rows, columns):
                self.previous.append(previous.detach().clone())
                return constrain_allocation(torch.zeros((1, 4, 34)), rows, columns)

        temporary, sequence = self._two_step_sequence(swap_second_rows=True)
        try:
            model = RecordingModel()
            result = self_rollout(model, Candidate.S1, (sequence,))
            self.assertEqual(
                result.steps[0].previous_belief,
                baseline_initial_state(sequence.steps[0]),
            )
            first_prediction = {
                row.wind: row.values for row in result.steps[0].prediction.rows
            }
            second_previous = {
                row.wind: row.values for row in result.steps[1].previous_belief.rows
            }
            self.assertEqual(second_previous, first_prediction)

            changed = Phase8Sequence(
                sequence.key,
                sequence.partition,
                tuple(
                    replace(
                        step,
                        target=tuple(
                            tuple(4.0 - value for value in row) for row in step.target
                        ),
                    )
                    for step in sequence.steps
                ),
            )
            changed_result = self_rollout(RecordingModel(), Candidate.S1, (changed,))
            self.assertEqual(result.predictions, changed_result.predictions)
            self.assertEqual(
                tuple(value.previous_belief for value in result.steps),
                tuple(value.previous_belief for value in changed_result.steps),
            )
        finally:
            temporary.cleanup()

    def test_s2_latent_resets_to_zero_at_each_sequence_boundary(self):
        from lisjong_arena.phase8_sequential.model import S2_LATENT_DIM
        from lisjong_arena.phase8_sequential.rollout import self_rollout

        class RecordingS2(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.latents = []

            def forward(self, _features, _previous, latent, rows, columns):
                self.latents.append(latent.detach().clone())
                constrained = constrain_allocation(
                    torch.zeros((1, 4, 34)), rows, columns
                )
                return constrained, latent + 1

        temporary, original = self._two_step_sequence()
        try:
            first = Phase8Sequence(original.key, original.partition, original.steps[:1])
            second_step = replace(
                original.steps[0],
                example=replace(
                    original.steps[0].example,
                    round_index=original.steps[0].example.round_index + 1,
                    anchor_index=2,
                ),
                sample=SimpleNamespace(
                    anchor=replace(original.steps[0].sample.anchor, anchor_index=2),
                    labels=original.steps[0].sample.labels,
                ),
            )
            second = Phase8Sequence(
                SequenceKey(
                    second_step.example.game,
                    second_step.example.round_index,
                    second_step.example.viewer_seat,
                ),
                DatasetPartition.TRAIN,
                (second_step,),
            )
            model = RecordingS2()
            result = self_rollout(model, Candidate.S2, (first, second))
            self.assertEqual(len(result.steps), 2)
            self.assertEqual(len(model.latents), 2)
            self.assertTrue(
                all(
                    torch.equal(value, torch.zeros((1, S2_LATENT_DIM)))
                    for value in model.latents
                )
            )
            self.assertEqual(result.predictions[0].rows, result.predictions[1].rows)
        finally:
            temporary.cleanup()

    def test_truncated_bptt_carries_values_and_detaches_only_history(self):
        from lisjong_arena.phase8_sequential.rollout import detach_recurrent_state

        winds = tuple(Wind)[:3]
        source = {
            wind: (torch.ones(34, requires_grad=True) * (index + 1))
            for index, wind in enumerate(winds)
        }
        latent = torch.ones((1, 128), requires_grad=True) * 7
        rows, detached_latent = detach_recurrent_state(source, latent)
        for wind in winds:
            self.assertTrue(torch.equal(rows[wind], source[wind]))
            self.assertFalse(rows[wind].requires_grad)
            self.assertIsNone(rows[wind].grad_fn)
        self.assertTrue(torch.equal(detached_latent, latent))
        self.assertFalse(detached_latent.requires_grad)
        self.assertIsNone(detached_latent.grad_fn)

    def test_one_epoch_training_is_deterministic_and_validation_does_not_update(self):
        from lisjong_arena.phase8_sequential.model import create_model
        from lisjong_arena.phase8_sequential.rollout import self_rollout
        from lisjong_arena.phase8_sequential.training import (
            TrainingConfig,
            train_candidate,
        )

        temporary, dataset, samples = self._examples()
        try:
            train_reference = replace(
                dataset.examples[0], partition=DatasetPartition.TRAIN
            )
            validation_reference = replace(
                dataset.examples[1], partition=DatasetPartition.VALIDATION
            )
            train_step = materialize_snapshot_example(train_reference, samples[0])
            validation_step = materialize_snapshot_example(
                validation_reference, samples[1]
            )
            train_sequence = Phase8Sequence(
                SequenceKey(
                    train_reference.game,
                    train_reference.round_index,
                    train_reference.viewer_seat,
                ),
                DatasetPartition.TRAIN,
                (train_step,),
            )
            validation_sequence = Phase8Sequence(
                SequenceKey(
                    validation_reference.game,
                    validation_reference.round_index,
                    validation_reference.viewer_seat,
                ),
                DatasetPartition.VALIDATION,
                (validation_step,),
            )
            torch.manual_seed(9)
            snapshot = self_rollout(
                create_model(Candidate.S1), Candidate.S1, (validation_sequence,)
            ).predictions
            snapshot_metrics = (
                evaluate_expected_count_predictions(
                    "b" * 64,
                    (validation_reference,),
                    (samples[1],),
                    snapshot,
                )
                .partition_metrics[0]
                .metrics
            )
            config = TrainingConfig(max_epochs=1, patience=1)
            policy = BpttPolicy(BpttMode.FULL_SEQUENCE, None)
            arguments = dict(
                dataset_identity="b" * 64,
                bptt_policy=policy,
                snapshot_validation_predictions=snapshot,
                config=config,
            )
            with patch(
                "lisjong_arena.phase8_sequential.evaluation.SNAPSHOT_VALIDATION_MAE",
                snapshot_metrics.per_tile_mae,
            ):
                first = train_candidate(
                    Candidate.S1,
                    (train_sequence, validation_sequence),
                    **arguments,
                )
                second = train_candidate(
                    Candidate.S1,
                    (train_sequence, validation_sequence),
                    **arguments,
                )
                changed_validation = Phase8Sequence(
                    validation_sequence.key,
                    validation_sequence.partition,
                    (
                        replace(
                            validation_step,
                            target=tuple(
                                tuple(4.0 - value for value in row)
                                for row in validation_step.target
                            ),
                        ),
                    ),
                )
                changed = train_candidate(
                    Candidate.S1,
                    (train_sequence, changed_validation),
                    **arguments,
                )
            for name, value in first.model.state_dict().items():
                self.assertTrue(torch.equal(value, second.model.state_dict()[name]))
                self.assertTrue(torch.equal(value, changed.model.state_dict()[name]))
        finally:
            temporary.cleanup()

    def _manifest(self, candidate: Candidate, model) -> dict[str, object]:
        from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
        from lisjong_arena.phase8_sequential.artifact import ARTIFACT_SCHEMA_VERSION
        from lisjong_arena.phase8_sequential.model import model_config, parameter_count
        from lisjong_arena.phase8_sequential.protocol import (
            INVENTORY_SCHEMA_VERSION,
            SEQUENCE_SEMANTICS_ID,
        )

        metric = {
            "samples": 18_890,
            "per_tile_mae": 0.48,
            "per_hand_l1": 16.0,
            "concealed_size_inconsistency_mean": 0.0,
            "concealed_size_inconsistency_max": 0.0,
            "physical_conservation_violation_sample_rate": 0.0,
            "conservation_total_excess": 0.0,
            "conservation_mean_excess_per_sample": 0.0,
        }
        validation = metric | {"samples": 4_558}
        inventory = {
            "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
            "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
            "raw_corpus_identity": "a" * 64,
            "dataset_identity": "b" * 64,
            "partitions": {
                "train": {
                    "sequence_count": 18_890,
                    "sample_count": 18_890,
                    "minimum_length": 1,
                    "mean_length": 1.0,
                    "median_length": 1.0,
                    "maximum_length": 1,
                    "depth_bucket_counts": {
                        "depth 1": 18_890,
                        "depth 2..4": 0,
                        "depth 5..8": 0,
                        "depth 9+": 0,
                    },
                },
                "validation": {
                    "sequence_count": 4_558,
                    "sample_count": 4_558,
                    "minimum_length": 1,
                    "mean_length": 1.0,
                    "median_length": 1.0,
                    "maximum_length": 1,
                    "depth_bucket_counts": {
                        "depth 1": 4_558,
                        "depth 2..4": 0,
                        "depth 5..8": 0,
                        "depth 9+": 0,
                    },
                },
            },
            "bptt_policy": {
                "mode": "full-sequence",
                "truncation_length": None,
            },
            "test_sequence_count": 0,
        }
        inventory["inventory_identity"] = hashlib.sha256(
            json.dumps(
                inventory,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "candidate": candidate.value,
            "raw_corpus_identity": "a" * 64,
            "dataset_identity": "b" * 64,
            "dataset_source_revisions": {
                "lisjong": "1" * 40,
                "lisjong_engine": "2" * 40,
                "lisjong_arena": "3" * 40,
            },
            "training_source_revisions": {
                "lisjong": "4" * 40,
                "lisjong_engine": "5" * 40,
                "lisjong_arena": "6" * 40,
            },
            "feature_semantics_id": FEATURE_SEMANTICS_ID,
            "feature_dimension": 919,
            "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
            "previous_belief_semantics": {
                "axis": "Wind->expected_count[34]",
                "current_order": "explicit-opponent_winds-remap",
                "scale": 4.0,
                "source": "prior-self-prediction",
            },
            "initial_state_semantics": {
                "depth_1_previous_belief": "current-public-conditional-uniform-baseline",
                "s2_latent": "zeros",
            },
            "self_rollout_semantics": "prediction_t->previous_belief_t+1",
            "population": {
                "train_seeds": list(range(100, 140)),
                "train_anchor_count": 18_890,
                "validation_seeds": list(range(140, 150)),
                "validation_anchor_count": 4_558,
            },
            "inventory": inventory,
            "bptt_policy": inventory["bptt_policy"],
            "model": model_config(candidate),
            "parameter_count": parameter_count(model),
            "constraint": {
                "implementation": "lisjong_arena.phase6_snapshot.constraint.constrain_allocation",
                "shape": [4, 34],
                "residual_tolerance": 1e-6,
            },
            "training": {
                "optimizer": "Adam",
                "seed": 0,
                "dataloader_seed": 0,
                "learning_rate": 1e-3,
                "weight_decay": 0.0,
                "max_epochs": 40,
                "patience": 6,
                "workers": 0,
                "deterministic": True,
                "torch_threads": 1,
                "checkpoint_selection": "strictly-lower-pooled-self-rollout-validation-mae",
                "checkpoint_tie_abs_tol": 1e-12,
            },
            "runtime": {
                "python": "3.14.0",
                "torch": "2.13.0+cpu",
                "device": "cpu",
                "platform": "synthetic",
            },
            "selected_epoch": 1,
            "loss_history": [{"epoch": 1, "train_mse": 1.0, "validation_mae": 0.48}],
            "train_metrics": metric,
            "validation_metrics": validation,
            "snapshot_validation_metrics": validation
            | {"per_tile_mae": 0.4863309527332531},
            "delta_mae": 0.4863309527332531 - 0.48,
            "per_game_diagnostics": [
                {
                    "source_class": "first-party-bootstrap",
                    "game_seed": seed,
                    "sample_count": 456 if seed < 149 else 454,
                    "snapshot_mae": 0.4863309527332531,
                    "candidate_mae": 0.48,
                    "delta_mae": 0.4863309527332531 - 0.48,
                }
                for seed in range(140, 150)
            ],
            "game_macro_mean_delta_mae": 0.4863309527332531 - 0.48,
            "median_per_game_delta_mae": 0.4863309527332531 - 0.48,
            "positive_game_count": 10,
            "depth_diagnostics": [
                {
                    "bucket": bucket,
                    "sample_count": 4_558 if bucket == "depth 1" else 0,
                    "candidate_mae": 0.48 if bucket == "depth 1" else None,
                    "snapshot_mae": 0.49 if bucket == "depth 1" else None,
                    "delta_mae": 0.01 if bucket == "depth 1" else None,
                }
                for bucket in DEPTH_BUCKETS
            ],
            "physical_consistency": {
                "constraint_non_convergence_count": 0,
                "maximum_row_column_residual": 0.0,
                "concealed_size_inconsistency_max": 0.0,
                "physical_conservation_violation_sample_rate": 0.0,
                "conservation_total_excess": 0.0,
                "conservation_mean_excess_per_sample": 0.0,
                "blocking_gate_passed": True,
            },
            "training_wall_clock_seconds": 1.0,
            "peak_process_ram_bytes": None,
            "inference_throughput": {
                "samples_per_second": 1.0,
                "torch_thread_count": 1,
                "platform": "synthetic",
            },
            "advancement_eligible": True,
            "test_partition_evaluated": False,
        }

    def test_artifact_identity_strict_load_test_false_and_overwrite_refusal(self):
        from lisjong_arena.phase8_sequential.artifact import (
            Phase8ArtifactError,
            artifact_logical_identity,
            comparison_value,
            load_comparison_result,
            load_model_artifact,
            save_comparison_result,
            save_model_artifact,
            validate_manifest,
            validate_result,
        )
        from lisjong_arena.phase8_sequential.model import create_model

        s1_model = create_model(Candidate.S1)
        s2_model = create_model(Candidate.S2)
        s1_manifest = self._manifest(Candidate.S1, s1_model)
        s2_manifest = self._manifest(Candidate.S2, s2_model)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            phase6 = root / "phase6"
            phase7 = root / "phase7"
            phase6.write_bytes(b"phase6-unchanged")
            phase7.write_bytes(b"phase7-unchanged")
            first = save_model_artifact(root / "s1", s1_model, s1_manifest)
            second = save_model_artifact(root / "s2", s2_model, s2_manifest)
            self.assertFalse(first.manifest["test_partition_evaluated"])
            self.assertEqual(
                artifact_logical_identity(first.manifest),
                artifact_logical_identity(load_model_artifact(root / "s1").manifest),
            )
            with self.assertRaises(FileExistsError):
                save_model_artifact(root / "s1", s1_model, s1_manifest)
            invalid = dict(s1_manifest)
            invalid["test_partition_evaluated"] = True
            with self.assertRaisesRegex(Phase8ArtifactError, "TEST=false"):
                validate_manifest(
                    invalid | {"weights_bytes": 1, "weights_sha256": "d" * 64}
                )
            wrong_config = dict(s1_manifest)
            wrong_config["candidate"] = "S2"
            with self.assertRaisesRegex(Phase8ArtifactError, "candidate config"):
                save_model_artifact(root / "wrong", s1_model, wrong_config)
            weights = root / "s1" / "weights.pt"
            data = bytearray(weights.read_bytes())
            data[-1] ^= 1
            weights.write_bytes(data)
            with self.assertRaisesRegex(Phase8ArtifactError, "SHA-256"):
                load_model_artifact(root / "s1")
            comparison = comparison_value(
                first.manifest,
                second.manifest,
                creation_software_revision="7" * 40,
            )
            self.assertFalse(comparison["test_partition_evaluated"])
            self.assertEqual(validate_result(comparison), comparison)
            save_comparison_result(root / "comparison", comparison)
            self.assertEqual(load_comparison_result(root / "comparison"), comparison)
            with self.assertRaises(FileExistsError):
                save_comparison_result(root / "comparison", comparison)
            self.assertEqual(phase6.read_bytes(), b"phase6-unchanged")
            self.assertEqual(phase7.read_bytes(), b"phase7-unchanged")


if __name__ == "__main__":
    unittest.main()
