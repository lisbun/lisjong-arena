"""Arm A (BC control) training and checkpoint tests (Issue #140)."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_offline_q.bc_training import (
    load_checkpoint,
    save_checkpoint,
    train_bc_model,
)
from lisjong_arena.learned_policy_offline_q.errors import OfflineQArtifactError

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class BcTrainingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        self.checkpoint_path = self._tmp / "checkpoint"

    def test_training_selects_a_checkpoint_and_round_trips_it(self):
        run = train_bc_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        self.assertEqual(checkpoint.manifest["dataset_identity"], self.dataset.identity)
        reloaded = load_checkpoint(self.checkpoint_path)
        self.assertEqual(reloaded.identity, checkpoint.identity)

    def test_existing_checkpoint_destination_is_never_overwritten(self):
        run = train_bc_model(self.dataset)
        save_checkpoint(self.checkpoint_path, self.dataset, run)
        with self.assertRaises(FileExistsError):
            save_checkpoint(self.checkpoint_path, self.dataset, run)

    def test_frozen_inference_is_deterministic(self):
        import torch

        run = train_bc_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        features = torch.zeros(1, checkpoint.model.network[0].in_features)
        first = checkpoint.model(features)
        second = checkpoint.model(features)
        self.assertTrue(torch.equal(first, second))

    def test_tampered_weights_fail_closed(self):
        run = train_bc_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        weights_path = checkpoint.path / "weights.pt"
        payload = bytearray(weights_path.read_bytes())
        payload[-1] ^= 0xFF
        weights_path.write_bytes(bytes(payload))
        with self.assertRaises(OfflineQArtifactError):
            load_checkpoint(self.checkpoint_path)

    def test_tampered_manifest_config_fails_closed(self):
        import json

        run = train_bc_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        manifest_path = checkpoint.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["training"]["learning_rate"] = 0.5
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(OfflineQArtifactError):
            load_checkpoint(self.checkpoint_path)


if __name__ == "__main__":
    unittest.main()
