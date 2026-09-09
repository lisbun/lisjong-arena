"""Synthetic PyTorch tests for Arena #172's frozen readout path."""

import copy
import hashlib
import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from _phase4_raw_corpus_fixtures import fixture_corpus
from test_phase11_public_riichi_wait_readout import _lock, _records, _runtime

import lisjong_arena.phase11_public_riichi_wait_readout.lock as phase11_lock
from lisjong_arena._execution_safety import ExecutionSafetyError
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.constraint import constrain_allocation
from lisjong_arena.phase6_snapshot.training import materialize_snapshot_example
from lisjong_arena.phase8_sequential.model import create_s2_model
from lisjong_arena.phase8_sequential.protocol import Phase8Sequence, SequenceKey
from lisjong_arena.phase11_public_riichi_wait_readout.artifact import (
    WEIGHTS_FILENAME,
    load_model,
    load_result,
    model_manifest_without_weights,
    save_model,
    save_result,
)
from lisjong_arena.phase11_public_riichi_wait_readout.coverage import build_coverage
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    extract_frozen_latents,
)
from lisjong_arena.phase11_public_riichi_wait_readout.evaluation import (
    evaluate_readout,
    mean_binary_log_loss,
)
from lisjong_arena.phase11_public_riichi_wait_readout.lock import current_receipt
from lisjong_arena.phase11_public_riichi_wait_readout.model import (
    assert_frozen_state_unchanged,
    create_readout_head,
    create_readout_optimizer,
    freeze_e160,
)
from lisjong_arena.phase11_public_riichi_wait_readout.protocol import (
    Phase11Error,
    ReadoutTrainingConfig,
    identity,
    retained_value,
)
from lisjong_arena.phase11_public_riichi_wait_readout.result import (
    assemble_result,
    validate_result,
)
from lisjong_arena.phase11_public_riichi_wait_readout.training import (
    EpochMetrics,
    train_readout,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class ExecutionLockHeadTest(unittest.TestCase):
    def _receipt(self, *, clean_head: str, provenance_revision: str, target: str):
        provenance = copy.deepcopy(_lock()["provenance"])
        provenance["source_revisions"]["lisjong_arena"] = provenance_revision
        evidence = SimpleNamespace(phase167_lock={"runtime": _runtime()})
        with (
            patch.object(
                phase11_lock, "require_clean_arena_head", return_value=clean_head
            ),
            patch.object(phase11_lock, "load_retained", return_value=evidence),
            patch.object(
                phase11_lock,
                "retained_readback_value",
                return_value=retained_value(),
            ),
            patch.object(phase11_lock, "phase4_provenance", return_value=None),
            patch.object(phase11_lock, "_provenance_value", return_value=provenance),
            patch.object(phase11_lock, "_runtime", return_value=_runtime()),
        ):
            return current_receipt(
                arena_revision=target,
                corpus_root="corpus",
                phase157_root="phase157",
                phase167_root="phase167",
                artifact_audit="Issue #172 retained audit fixture",
            )

    def test_dirty_arena_worktree_rejects_lock_before_artifact_readback(self):
        with (
            patch.object(
                phase11_lock,
                "require_clean_arena_head",
                side_effect=ExecutionSafetyError("Arena worktree must be clean"),
            ),
            patch.object(phase11_lock, "load_retained") as load_retained,
        ):
            with self.assertRaisesRegex(ExecutionSafetyError, "must be clean"):
                current_receipt(
                    arena_revision="3" * 40,
                    corpus_root="corpus",
                    phase157_root="phase157",
                    phase167_root="phase167",
                    artifact_audit="Issue #172 retained audit fixture",
                )
        load_retained.assert_not_called()

    def test_clean_head_must_match_execution_provenance(self):
        with self.assertRaisesRegex(Phase11Error, "execution provenance"):
            self._receipt(
                clean_head="4" * 40,
                provenance_revision="3" * 40,
                target="4" * 40,
            )

    def test_clean_head_must_match_locked_execution_target(self):
        with self.assertRaisesRegex(Phase11Error, "execution target"):
            self._receipt(
                clean_head="3" * 40,
                provenance_revision="3" * 40,
                target="4" * 40,
            )

    def test_exact_clean_head_is_recorded(self):
        receipt = self._receipt(
            clean_head="3" * 40,
            provenance_revision="3" * 40,
            target="3" * 40,
        )
        self.assertEqual(
            receipt["provenance"]["source_revisions"]["lisjong_arena"], "3" * 40
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class FrozenReadoutTrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global torch
        import torch

    def _tensor_records(self, validation_hanchan=8):
        return tuple(
            replace(record, latent=torch.arange(128, dtype=torch.float32) / 128)
            for record in _records(validation_hanchan)
        )

    def test_fixed_head_shape_and_optimizer_contains_head_only(self):
        trunk = create_s2_model()
        freeze_e160(trunk)
        head = create_readout_head()
        output = head(torch.zeros((2, 128)))
        self.assertEqual(output.shape, (2, 102))
        layers = tuple(head)
        self.assertEqual((layers[0].in_features, layers[0].out_features), (128, 64))
        self.assertIsInstance(layers[1], torch.nn.ReLU)
        self.assertEqual((layers[2].in_features, layers[2].out_features), (64, 102))
        optimizer = create_readout_optimizer(
            head, trunk, learning_rate=1e-3, weight_decay=0.0
        )
        optimized = {
            id(parameter)
            for group in optimizer.param_groups
            for parameter in group["params"]
        }
        self.assertEqual(optimized, {id(parameter) for parameter in head.parameters()})
        self.assertFalse(
            optimized & {id(parameter) for parameter in trunk.parameters()}
        )

    def test_short_synthetic_training_keeps_trunk_and_expected_head_byte_identical(
        self,
    ):
        trunk = create_s2_model()
        snapshot = freeze_e160(trunk)
        records = self._tensor_records()
        result = train_readout(
            trunk,
            snapshot,
            tuple(row for row in records if row.partition is DatasetPartition.TRAIN),
            tuple(
                row for row in records if row.partition is DatasetPartition.VALIDATION
            ),
            config=ReadoutTrainingConfig(max_epochs=2, patience=6),
        )
        self.assertEqual(result.frozen_digest_before, result.frozen_digest_after)
        self.assertEqual(
            assert_frozen_state_unchanged(snapshot, trunk).digest, snapshot.digest
        )
        self.assertEqual(
            result.optimizer_parameter_names,
            ("0.weight", "0.bias", "2.weight", "2.bias"),
        )

    def test_unavailable_rows_do_not_enter_the_unweighted_bce(self):
        records = self._tensor_records(validation_hanchan=1)
        selected = tuple(
            row for row in records if row.partition is DatasetPartition.TRAIN
        )
        head = create_readout_head()
        for parameter in head.parameters():
            torch.nn.init.zeros_(parameter)
        self.assertAlmostEqual(
            mean_binary_log_loss(head, selected), torch.log(torch.tensor(2.0)).item()
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class NextLatentAlignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global torch
        import torch

    def test_target_is_bound_to_same_step_next_latent(self):
        with TemporaryDirectory() as temporary:
            raw = save_raw_corpus(fixture_corpus(), Path(temporary) / "raw")
            dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
            samples = resolve_training_samples(dataset, raw)
            reference = replace(dataset.examples[0], partition=DatasetPartition.TRAIN)
            step = materialize_snapshot_example(reference, samples[0])
            sequence = Phase8Sequence(
                SequenceKey(
                    reference.game, reference.round_index, reference.viewer_seat
                ),
                DatasetPartition.TRAIN,
                (step,),
            )

            class RecordingS2(torch.nn.Module):
                def __init__(self):
                    super().__init__()
                    self.prior_latents = []

                def forward(self, _features, _previous, latent, rows, columns):
                    self.prior_latents.append(latent.detach().clone())
                    allocation = constrain_allocation(
                        torch.zeros((1, 4, 34)), rows, columns
                    )
                    return allocation, latent + 1

            model = RecordingS2()
            freeze_e160(model)
            records = extract_frozen_latents(model, (sequence,))
            self.assertTrue(torch.equal(model.prior_latents[0], torch.zeros((1, 128))))
            self.assertTrue(torch.equal(records[0].latent, torch.ones(128)))


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class ArtifactAndResultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global torch
        import torch

    def _manifest(self, lock, coverage_identity):
        history = tuple(
            EpochMetrics(epoch, 0.6 + epoch / 100, 0.5 + (epoch - 1) / 100)
            for epoch in range(1, 8)
        )
        result = SimpleNamespace(
            selected_epoch=1,
            history=history,
            train_binary_log_loss=0.55,
            validation_binary_log_loss=0.5,
            optimizer_parameter_names=("0.weight", "0.bias", "2.weight", "2.bias"),
            frozen_digest_before="a" * 64,
            frozen_digest_after="a" * 64,
            training_wall_seconds=1.0,
        )
        return model_manifest_without_weights(
            lock=lock,
            coverage_identity=coverage_identity,
            result=result,
            training_cpu_seconds=0.5,
        )

    def test_weights_strict_load_and_tampering_is_rejected(self):
        lock = _lock()
        coverage = build_coverage(self._tensor_records(), identity(lock))
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "readout-model"
            manifest = save_model(
                destination,
                create_readout_head(),
                self._manifest(lock, identity(coverage)),
                lock,
                identity(coverage),
            )
            loaded, readback = load_model(destination, lock, identity(coverage))
            self.assertEqual(readback, manifest)
            self.assertEqual(
                sum(parameter.numel() for parameter in loaded.parameters()), 14_886
            )
            (destination / WEIGHTS_FILENAME).write_bytes(b"tampered")
            with self.assertRaises(Phase11Error):
                load_model(destination, lock, identity(coverage))

    def _tensor_records(self):
        return tuple(replace(record, latent=torch.zeros(128)) for record in _records())

    def test_result_is_rederived_and_tampering_is_rejected(self):
        lock = _lock()
        records = self._tensor_records()
        coverage_value = build_coverage(records, identity(lock))
        coverage = {**coverage_value, "coverage_identity": identity(coverage_value)}
        head = create_readout_head()
        manifest = self._manifest(lock, coverage["coverage_identity"])
        weights = b"fixture"
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = hashlib.sha256(weights).hexdigest()
        validation = tuple(
            row for row in records if row.partition is DatasetPartition.VALIDATION
        )
        evidence = evaluate_readout(
            head, validation, coverage["train_prevalence_baseline"]
        )
        result = assemble_result(
            lock,
            coverage,
            baseline=coverage["train_prevalence_baseline"],
            model_manifest=manifest,
            evaluation_evidence=evidence,
        )
        tampered = copy.deepcopy(result)
        tampered["evaluation_evidence"]["per_tile"][0]["readout_logloss_sum"] += 1.0
        with self.assertRaises(Phase11Error):
            validate_result(tampered, lock)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            save_result(path, result, lock)
            loaded = load_result(path, lock)
            self.assertIn(
                loaded["outcome"],
                {"CLEAR READOUT SIGNAL", "CLEAR READOUT REGRESSION", "INCONCLUSIVE"},
            )
            data = path.read_bytes()
            path.write_bytes(
                data.replace(b'"formal_test":false', b'"formal_test":true')
            )
            with self.assertRaises(Phase11Error):
                load_result(path, lock)


if __name__ == "__main__":
    unittest.main()
