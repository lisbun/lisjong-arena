"""Offline Q dataset artifact writer / reader tests (Issue #140)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import (
    FIXTURE_PROVENANCE,
    transition_row,
    write_synthetic_dataset,
)

from lisjong_arena.learned_policy_offline_q.artifact import (
    OfflineQArtifactError,
    OfflineQDatasetWriter,
    load_dataset,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_HANCHAN_COUNT,
    DATASET_ORDERED_SEEDS,
    Split,
    split_for_seed,
)


class DatasetArtifactTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_round_trip_binds_every_required_identity(self):
        dataset = write_synthetic_dataset(self.destination)
        self.assertEqual(
            dataset.manifest["totals"]["game_count"], DATASET_HANCHAN_COUNT
        )
        self.assertEqual(
            dataset.manifest["totals"]["row_count"],
            6 * DATASET_HANCHAN_COUNT,
        )
        self.assertEqual(
            dataset.manifest["totals"]["terminal_row_count"], DATASET_HANCHAN_COUNT
        )
        reloaded = load_dataset(self.destination)
        self.assertEqual(reloaded.identity, dataset.identity)
        self.assertEqual(len(reloaded.rows), dataset.row_count)

    def test_every_row_satisfies_the_hard_dataset_invariants(self):
        dataset = write_synthetic_dataset(self.destination)
        for index, row in enumerate(dataset.rows):
            self.assertGreaterEqual(row.legal_action_count, 2)
            mask = dataset.legal_mask_row(index)
            self.assertEqual(sum(mask), row.legal_action_count)
            self.assertTrue(mask[row.behavior_action_index])
            if row.terminal:
                self.assertIsNone(row.next_decision_ordinal)
            else:
                self.assertIsNotNone(row.next_decision_ordinal)
                self.assertGreater(row.next_decision_ordinal, row.decision_ordinal)
                next_mask = dataset.next_legal_mask_row(index)
                self.assertGreaterEqual(sum(next_mask), 2)
        self.assertEqual(dataset.count_non_finite_features(), 0)

    def test_split_membership_never_crosses_games(self):
        dataset = write_synthetic_dataset(self.destination)
        for split in Split:
            for index in dataset.split_indices(split):
                self.assertIs(dataset.rows[index].split, split)

    def test_existing_destination_is_never_overwritten(self):
        write_synthetic_dataset(self.destination)
        with self.assertRaises(FileExistsError):
            write_synthetic_dataset(self.destination)

    def test_incomplete_seed_population_fails_closed(self):
        writer = OfflineQDatasetWriter(self.destination, provenance=FIXTURE_PROVENANCE)
        try:
            for seed in DATASET_ORDERED_SEEDS[:-1]:
                writer.add_game(
                    seed=seed,
                    split=split_for_seed(seed),
                    scores=(25000, 25000, 25000, 25000),
                    ranks=(1, 2, 3, 4),
                    rows=(transition_row(seed, i, rows_per_game=6) for i in range(6)),
                )
            with self.assertRaises(OfflineQArtifactError):
                writer.finalize()
        finally:
            writer.discard()

    def test_out_of_order_seeds_fail_closed(self):
        writer = OfflineQDatasetWriter(self.destination, provenance=FIXTURE_PROVENANCE)
        try:
            first, second = DATASET_ORDERED_SEEDS[0], DATASET_ORDERED_SEEDS[1]
            writer.add_game(
                seed=second,
                split=split_for_seed(second),
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(transition_row(second, i, rows_per_game=6) for i in range(6)),
            )
            with self.assertRaises(OfflineQArtifactError):
                writer.add_game(
                    seed=first,
                    split=split_for_seed(first),
                    scores=(25000, 25000, 25000, 25000),
                    ranks=(1, 2, 3, 4),
                    rows=(transition_row(first, i, rows_per_game=6) for i in range(6)),
                )
        finally:
            writer.discard()

    def test_corrupt_feature_bytes_fail_closed(self):
        write_synthetic_dataset(self.destination)
        path = self.destination / "features.f32"
        payload = bytearray(path.read_bytes())
        payload[0] ^= 0xFF
        path.write_bytes(bytes(payload))
        with self.assertRaises(OfflineQArtifactError):
            load_dataset(self.destination)

    def test_corrupt_next_legal_mask_bytes_fail_closed(self):
        write_synthetic_dataset(self.destination)
        path = self.destination / "next_legal_mask.u8"
        payload = bytearray(path.read_bytes())
        payload[0] ^= 0xFF
        path.write_bytes(bytes(payload))
        with self.assertRaises(OfflineQArtifactError):
            load_dataset(self.destination)

    def test_tampered_manifest_identity_fails_closed(self):
        write_synthetic_dataset(self.destination)
        manifest_path = self.destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["totals"]["row_count"] += 1
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(OfflineQArtifactError):
            load_dataset(self.destination)

    def test_extra_file_fails_closed(self):
        write_synthetic_dataset(self.destination)
        (self.destination / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(OfflineQArtifactError):
            load_dataset(self.destination)

    def test_missing_file_fails_closed(self):
        write_synthetic_dataset(self.destination)
        (self.destination / "next_features.f32").unlink()
        with self.assertRaises(OfflineQArtifactError):
            load_dataset(self.destination)


class TeacherRevisionBindingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_writer_rejects_provenance_from_another_lisjong_revision(self):
        bad_provenance = dict(FIXTURE_PROVENANCE, lisjong_revision="f" * 40)
        with self.assertRaises(OfflineQArtifactError):
            OfflineQDatasetWriter(self.destination, provenance=bad_provenance)


if __name__ == "__main__":
    unittest.main()
