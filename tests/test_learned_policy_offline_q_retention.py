"""Artifact retention gate tests (Issue #140).

`resolve_retention_target()`自体のfail-closedな判定条件（temporary directory、
Git work tree等）は`lisbun/lisjong-arena #138`のtest suiteが正本であり、ここでは
BC / Q checkpointの複製・freeze record・strict readbackだけを検証する。
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_offline_q.bc_training import (
    save_checkpoint as save_bc_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint as save_q_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.q_training import train_q_model
from lisjong_arena.learned_policy_offline_q.retention import (
    Stage4aRetentionError,
    freeze_candidates,
    strict_readback,
)

_EPHEMERAL_PATCH = "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots"


class RetentionGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        bc_run = train_bc_model(self.dataset)
        q_run = train_q_model(self.dataset)
        self.bc_checkpoint = save_bc_checkpoint(
            self._tmp / "bc-checkpoint", self.dataset, bc_run
        )
        self.q_checkpoint = save_q_checkpoint(
            self._tmp / "q-checkpoint", self.dataset, q_run
        )
        self.retention_root = self._tmp / "retention-root"
        self.retention_root.mkdir()

    def test_blocked_without_a_declared_non_ephemeral_root(self):
        with self.assertRaises(Stage4aRetentionError):
            freeze_candidates(
                bc_checkpoint_path=self.bc_checkpoint.path,
                q_checkpoint_path=self.q_checkpoint.path,
                backend="test-store",
                root=self.retention_root,
                key="offlineq/run-1",
            )

    def test_freeze_and_strict_readback_round_trips(self):
        with mock.patch(_EPHEMERAL_PATCH, return_value=()):
            freeze, retained = freeze_candidates(
                bc_checkpoint_path=self.bc_checkpoint.path,
                q_checkpoint_path=self.q_checkpoint.path,
                backend="test-store",
                root=self.retention_root,
                key="offlineq/run-1",
            )
        self.assertEqual(freeze.bc_checkpoint_identity, self.bc_checkpoint.identity)
        self.assertEqual(freeze.q_checkpoint_identity, self.q_checkpoint.identity)
        self.assertEqual(retained.bc_checkpoint.identity, self.bc_checkpoint.identity)
        self.assertEqual(retained.q_checkpoint.identity, self.q_checkpoint.identity)

        reloaded = strict_readback(self.retention_root / "offlineq" / "run-1")
        self.assertEqual(reloaded.bc_checkpoint.identity, self.bc_checkpoint.identity)
        self.assertEqual(reloaded.q_checkpoint.identity, self.q_checkpoint.identity)

    def test_a_second_freeze_at_the_same_key_is_write_once(self):
        with mock.patch(_EPHEMERAL_PATCH, return_value=()):
            freeze_candidates(
                bc_checkpoint_path=self.bc_checkpoint.path,
                q_checkpoint_path=self.q_checkpoint.path,
                backend="test-store",
                root=self.retention_root,
                key="offlineq/run-1",
            )
            with self.assertRaises(Stage4aRetentionError):
                freeze_candidates(
                    bc_checkpoint_path=self.bc_checkpoint.path,
                    q_checkpoint_path=self.q_checkpoint.path,
                    backend="test-store",
                    root=self.retention_root,
                    key="offlineq/run-1",
                )


if __name__ == "__main__":
    unittest.main()
