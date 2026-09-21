from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisjong_arena.durable_seed_checkpoint import (
    DurableSeedCheckpointError,
    publish_seed_checkpoint,
    read_seed_checkpoint,
    verify_seed_checkpoint_set,
)


class DurableSeedCheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.checkpoint_dir = Path(self._tempdir.name) / "checkpoints"

    def _publish(self, seed: int, *, run_id: str = "run-1", protocol="proto-1"):
        return publish_seed_checkpoint(
            self.checkpoint_dir,
            run_id=run_id,
            seed=seed,
            protocol_identity=protocol,
            payload={"seed": seed, "value": seed * 2},
            started_at="2026-09-21T00:00:00Z",
            completed_at="2026-09-21T00:00:01Z",
        )

    def test_publish_creates_artifact_and_receipt_bound_by_hash(self) -> None:
        receipt = self._publish(7)
        artifact_path = self.checkpoint_dir / receipt["artifact_relative_path"]
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(receipt["seed"], 7)
        self.assertEqual(receipt["run_id"], "run-1")
        self.assertEqual(receipt["protocol_identity"], "proto-1")
        self.assertEqual(len(receipt["artifact_sha256"]), 64)
        self.assertEqual(receipt["artifact_size_bytes"], artifact_path.stat().st_size)

    def test_publish_fails_closed_on_duplicate_seed(self) -> None:
        self._publish(7)
        with self.assertRaises(DurableSeedCheckpointError):
            self._publish(7)

    def test_publish_fails_closed_on_pre_existing_destination(self) -> None:
        self.checkpoint_dir.mkdir(parents=True)
        (self.checkpoint_dir / "seed-7.artifact.json").write_text("{}")
        with self.assertRaises(DurableSeedCheckpointError):
            self._publish(7)

    def test_read_back_verifies_hash_and_identity(self) -> None:
        self._publish(3)
        receipt, payload = read_seed_checkpoint(
            self.checkpoint_dir, 3, run_id="run-1", protocol_identity="proto-1"
        )
        self.assertEqual(payload, {"seed": 3, "value": 6})
        self.assertEqual(receipt["seed"], 3)

    def test_read_back_fails_closed_on_run_id_mismatch(self) -> None:
        self._publish(3)
        with self.assertRaises(DurableSeedCheckpointError):
            read_seed_checkpoint(
                self.checkpoint_dir, 3, run_id="other-run", protocol_identity="proto-1"
            )

    def test_read_back_fails_closed_on_protocol_identity_mismatch(self) -> None:
        self._publish(3)
        with self.assertRaises(DurableSeedCheckpointError):
            read_seed_checkpoint(
                self.checkpoint_dir, 3, run_id="run-1", protocol_identity="other"
            )

    def test_read_back_fails_closed_on_missing_receipt(self) -> None:
        with self.assertRaises(DurableSeedCheckpointError):
            read_seed_checkpoint(
                self.checkpoint_dir, 3, run_id="run-1", protocol_identity="proto-1"
            )

    def test_read_back_fails_closed_on_tampered_artifact_hash(self) -> None:
        receipt = self._publish(3)
        artifact_path = self.checkpoint_dir / receipt["artifact_relative_path"]
        artifact_path.write_text('{"seed": 3, "value": 999}\n')
        with self.assertRaises(DurableSeedCheckpointError):
            read_seed_checkpoint(
                self.checkpoint_dir, 3, run_id="run-1", protocol_identity="proto-1"
            )

    def test_verify_set_passes_when_every_expected_seed_is_durable(self) -> None:
        for seed in (1, 2, 3):
            self._publish(seed)
        receipts = verify_seed_checkpoint_set(
            self.checkpoint_dir,
            run_id="run-1",
            protocol_identity="proto-1",
            expected_seeds=[1, 2, 3],
        )
        self.assertEqual(set(receipts), {1, 2, 3})

    def test_verify_set_fails_closed_on_missing_seed(self) -> None:
        for seed in (1, 2):
            self._publish(seed)
        with self.assertRaises(DurableSeedCheckpointError):
            verify_seed_checkpoint_set(
                self.checkpoint_dir,
                run_id="run-1",
                protocol_identity="proto-1",
                expected_seeds=[1, 2, 3],
            )

    def test_verify_set_fails_closed_on_extra_seed(self) -> None:
        for seed in (1, 2, 3):
            self._publish(seed)
        with self.assertRaises(DurableSeedCheckpointError):
            verify_seed_checkpoint_set(
                self.checkpoint_dir,
                run_id="run-1",
                protocol_identity="proto-1",
                expected_seeds=[1, 2],
            )

    def test_verify_set_fails_closed_on_duplicate_expected_seed(self) -> None:
        self._publish(1)
        with self.assertRaises(DurableSeedCheckpointError):
            verify_seed_checkpoint_set(
                self.checkpoint_dir,
                run_id="run-1",
                protocol_identity="proto-1",
                expected_seeds=[1, 1],
            )

    def test_completion_receipt_never_carries_scientific_fields(self) -> None:
        receipt = self._publish(9)
        self.assertNotIn("score", receipt)
        self.assertNotIn("scores", receipt)
        self.assertNotIn("rank", receipt)
        self.assertNotIn("ranks", receipt)
        self.assertNotIn("outcome", receipt)


if __name__ == "__main__":
    unittest.main()
