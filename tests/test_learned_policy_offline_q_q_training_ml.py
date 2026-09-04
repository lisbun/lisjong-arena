"""Arm B (support-restricted Offline Q) training and checkpoint tests (Issue #140)."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import (
    FIXTURE_PROVENANCE,
    write_synthetic_dataset,
)

from lisjong_arena.learned_policy_offline_q.artifact import OfflineQDatasetWriter
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQArtifactError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS,
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
)
from lisjong_arena.learned_policy_offline_q.q_training import (
    load_checkpoint,
    save_checkpoint,
    train_q_model,
    train_support_mask,
)
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors


def _mask(indices) -> tuple[bool, ...]:
    chosen = set(indices)
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def _features(seed: int, ordinal: int) -> tuple[float, ...]:
    values = [0.0] * FEATURE_DIMENSION
    values[(seed * 31 + ordinal * 17) % FEATURE_DIMENSION] = 1.0
    return tuple(values)


def _row(
    seed: int,
    ordinal: int,
    *,
    legal_indices,
    behavior_index,
    terminal,
    next_legal_indices=None,
) -> MacroTransitionRow:
    kwargs = dict(
        seed=seed,
        split=split_for_seed(seed),
        round_ordinal=0,
        round_wind="east",
        hand_number=1,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=_features(seed, ordinal),
        legal_mask=_mask(legal_indices),
        behavior_action_index=behavior_index,
        behavior_action_family=action_family(behavior_index),
        reward=0.01,
        terminal=terminal,
    )
    if terminal:
        kwargs.update(
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )
    else:
        kwargs.update(
            next_step_ordinal=ordinal + 1,
            next_decision_ordinal=ordinal + 1,
            next_feature_values=_features(seed, ordinal + 1),
            next_legal_mask=_mask(next_legal_indices),
        )
    return MacroTransitionRow(**kwargs)


def _build_dataset_with_a_coverage_gap(destination: Path):
    """TRAINのbehavior supportは{0,1}に固定し、1つのTRAIN gameだけ
    next legal indicesが{2,3}（support外）しか持たないnonterminal rowを持つ。
    """
    writer = OfflineQDatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        train_seeds = [
            seed
            for seed in DATASET_ORDERED_SEEDS
            if split_for_seed(seed) is Split.TRAIN
        ]
        gap_seed = train_seeds[0]
        for seed in DATASET_ORDERED_SEEDS:
            split = split_for_seed(seed)
            if seed == gap_seed:
                rows = (
                    _row(
                        seed,
                        0,
                        legal_indices=(0, 1),
                        behavior_index=0,
                        terminal=False,
                        next_legal_indices=(2, 3),
                    ),
                )
            else:
                rows = (
                    _row(
                        seed,
                        0,
                        legal_indices=(0, 1),
                        behavior_index=0,
                        terminal=False,
                        next_legal_indices=(0, 1),
                    ),
                    _row(
                        seed, 1, legal_indices=(0, 1), behavior_index=1, terminal=True
                    ),
                )
            writer.add_game(
                seed=seed,
                split=split,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=rows,
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class QTrainingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        self.checkpoint_path = self._tmp / "checkpoint"

    def test_train_support_mask_matches_the_train_behavior_actions(self):
        tensors = load_split_tensors(self.dataset)
        mask = train_support_mask(tensors[Split.TRAIN])
        expected = set(tensors[Split.TRAIN].behavior_action_index.tolist())
        actual = {index for index in range(VOCABULARY_SIZE) if bool(mask[index])}
        self.assertEqual(actual, expected)

    def test_training_selects_a_checkpoint_and_round_trips_it(self):
        run = train_q_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        self.assertEqual(checkpoint.manifest["dataset_identity"], self.dataset.identity)
        reloaded = load_checkpoint(self.checkpoint_path)
        self.assertEqual(reloaded.identity, checkpoint.identity)
        self.assertTrue(reloaded.supported_indices)

    def test_existing_checkpoint_destination_is_never_overwritten(self):
        run = train_q_model(self.dataset)
        save_checkpoint(self.checkpoint_path, self.dataset, run)
        with self.assertRaises(FileExistsError):
            save_checkpoint(self.checkpoint_path, self.dataset, run)

    def test_tampered_weights_fail_closed(self):
        run = train_q_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        weights_path = checkpoint.path / "weights.pt"
        payload = bytearray(weights_path.read_bytes())
        payload[-1] ^= 0xFF
        weights_path.write_bytes(bytes(payload))
        with self.assertRaises(OfflineQArtifactError):
            load_checkpoint(self.checkpoint_path)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CoverageGapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_unsupported_next_actions_fail_closed_instead_of_silently_extrapolating(
        self,
    ):
        dataset = _build_dataset_with_a_coverage_gap(self.destination)
        with self.assertRaises(OfflineQProtocolError):
            train_q_model(dataset)


if __name__ == "__main__":
    unittest.main()
