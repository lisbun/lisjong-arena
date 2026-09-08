"""ML wiring and strict artifact checks for Issue #190."""

import hashlib
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_data_sufficiency import artifact as artifact_module
from lisjong_arena.learned_policy_data_sufficiency import protocol as protocol_module
from lisjong_arena.learned_policy_data_sufficiency import source as source_module
from lisjong_arena.learned_policy_data_sufficiency.artifact import load_artifact
from lisjong_arena.learned_policy_data_sufficiency.experiment import run_experiment
from lisjong_arena.learned_policy_data_sufficiency.protocol import SCALE_SEEDS
from lisjong_arena.learned_policy_data_sufficiency.source import (
    load_source_dataset,
    scale_tensors,
)
from lisjong_arena.learned_policy_offline_q import artifact as offlineq_artifact
from lisjong_arena.learned_policy_offline_q.bc_training import (
    locked_training_block,
    train_from_split_tensors,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATALOADER_SEED,
    DETERMINISTIC_ALGORITHMS,
    TRAINING_SEED,
    Split,
)
from lisjong_arena.learned_policy_stage2.network import create_model

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _fixture_prefix_binding(dataset):
    row_count = sum(
        game["row_count"] for game in dataset.manifest["games"] if game["seed"] <= 270
    )
    lines = (
        (dataset.path / offlineq_artifact.ROWS_FILENAME)
        .read_bytes()
        .splitlines(keepends=True)
    )
    rows = b"".join(lines[:row_count])
    features = (dataset.path / offlineq_artifact.FEATURES_FILENAME).read_bytes()[
        : row_count * 8204 * 4
    ]
    masks = (dataset.path / offlineq_artifact.LEGAL_MASK_FILENAME).read_bytes()[
        : row_count * 802
    ]
    return {
        "row_count": row_count,
        "rows_sha256": hashlib.sha256(rows).hexdigest(),
        "features_sha256": hashlib.sha256(features).hexdigest(),
        "legal_mask_sha256": hashlib.sha256(masks).hexdigest(),
    }


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class DataSufficiencyMlTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self.root / "dataset", rows_per_game=1)

    def _identity_patches(self):
        prefix = _fixture_prefix_binding(self.dataset)
        return (
            mock.patch.object(
                source_module, "SOURCE_DATASET_IDENTITY", self.dataset.identity
            ),
            mock.patch.object(
                artifact_module, "SOURCE_DATASET_IDENTITY", self.dataset.identity
            ),
            mock.patch.object(
                protocol_module, "SOURCE_DATASET_IDENTITY", self.dataset.identity
            ),
            mock.patch.object(source_module, "SOURCE_PREFIX_BINDING", prefix),
            mock.patch.object(protocol_module, "SOURCE_PREFIX_BINDING", prefix),
        )

    def _source(self):
        patches = self._identity_patches()
        with patches[0], patches[3]:
            return load_source_dataset(self.dataset.path)

    def test_model_is_exact_flat_8204_128_relu_802(self):
        from torch import nn

        model = create_model()
        self.assertEqual(model.network[0].in_features, 8204)
        self.assertEqual(model.network[0].out_features, 128)
        self.assertIsInstance(model.network[1], nn.ReLU)
        self.assertEqual(model.network[2].in_features, 128)
        self.assertEqual(model.network[2].out_features, 802)

    def test_only_train_row_count_changes_and_validation_is_fixed(self):
        source = self._source()
        validation = None
        train_counts = []
        for scale in SCALE_SEEDS:
            tensors = scale_tensors(source, scale)
            train_counts.append(tensors[Split.TRAIN].row_count)
            current = tensors[Split.VALIDATION]
            if validation is None:
                validation = current
            else:
                self.assertEqual(current.row_indices, validation.row_indices)
                self.assertTrue(current.features.equal(validation.features))
                self.assertTrue(current.legal_mask.equal(validation.legal_mask))
                self.assertTrue(
                    current.behavior_action_index.equal(
                        validation.behavior_action_index
                    )
                )
        self.assertEqual(train_counts, [5, 10, 15, 20])
        config = locked_training_block()
        self.assertEqual(config["training_seed"], TRAINING_SEED)
        self.assertEqual(config["dataloader_seed"], DATALOADER_SEED)
        self.assertIs(config["deterministic_algorithms"], DETERMINISTIC_ALGORITHMS)

    def test_training_is_deterministic_with_the_fixed_seeds(self):
        source = self._source()
        first = train_from_split_tensors(scale_tensors(source, "S5"))
        second = train_from_split_tensors(scale_tensors(source, "S5"))
        self.assertEqual(first.selected_epoch, second.selected_epoch)
        self.assertEqual(
            first.selected_validation_choice_masked_ce,
            second.selected_validation_choice_masked_ce,
        )
        for name, tensor in first.model.state_dict().items():
            self.assertTrue(tensor.equal(second.model.state_dict()[name]))

    def test_full_artifact_is_write_once_and_strictly_read_back(self):
        patches = self._identity_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            source = load_source_dataset(self.dataset.path)
            artifact = run_experiment(
                source=source,
                destination=self.root / "result",
                backend="fixture-durable",
                key="offlineq-190-data-sufficiency-preflight/fixture",
            )
            self.assertEqual(set(artifact.result["scales"]), set(SCALE_SEEDS))
            for scale, checkpoint in artifact.checkpoints.items():
                self.assertEqual(checkpoint.manifest["scale"], scale)
                self.assertEqual(
                    checkpoint.manifest["training"], locked_training_block()
                )
            reloaded = load_artifact(self.root / "result")
            self.assertEqual(
                reloaded.result["result_identity"],
                artifact.result["result_identity"],
            )
            with self.assertRaises(FileExistsError):
                run_experiment(
                    source=source,
                    destination=self.root / "result",
                    backend="fixture-durable",
                    key="offlineq-190-data-sufficiency-preflight/fixture",
                )

    def test_tampered_checkpoint_weights_fail_closed(self):
        patches = self._identity_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            source = load_source_dataset(self.dataset.path)
            run_experiment(
                source=source,
                destination=self.root / "result",
                backend="fixture-durable",
                key="offlineq-190-data-sufficiency-preflight/tamper",
            )
            weights = self.root / "result" / "checkpoints" / "S5" / "weights.pt"
            payload = bytearray(weights.read_bytes())
            payload[-1] ^= 0xFF
            weights.write_bytes(payload)
            with self.assertRaises(ValueError):
                load_artifact(self.root / "result")


if __name__ == "__main__":
    unittest.main()
