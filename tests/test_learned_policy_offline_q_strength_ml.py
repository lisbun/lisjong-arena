"""Q-vs-BC ABBB strength screen wiring tests (Issue #140)."""

import importlib.util
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
from lisjong_arena.learned_policy_offline_q.protocol import OfflineQOutcome
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint as save_q_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.q_training import train_q_model
from lisjong_arena.learned_policy_offline_q.retention import freeze_candidates
from lisjong_arena.learned_policy_offline_q.strength import (
    OfflineQStrengthError,
    build_specs,
    classify_value_q_signal,
)
from lisjong_arena.single_round_evaluation import SeedBlockStatistics

_EPHEMERAL_PATCH = "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots"


def _statistics(lower, upper) -> SeedBlockStatistics:
    return SeedBlockStatistics(
        seed_block_count=25,
        mean_seed_block_delta=(lower or 0.0 + (upper or 0.0)) / 2,
        sample_standard_deviation=1.0,
        standard_error=1.0,
        normal_approx_95_interval_lower=lower,
        normal_approx_95_interval_upper=upper,
        positive_seed_block_count=10,
        zero_seed_block_count=0,
        negative_seed_block_count=15,
    )


TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class ClassifyValueQSignalTest(unittest.TestCase):
    def test_positive_interval_is_signal(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(10.0, 100.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_SIGNAL,
        )

    def test_negative_interval_is_negative(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(-100.0, -10.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_NEGATIVE,
        )

    def test_straddling_interval_is_inconclusive(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(-10.0, 10.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_INCONCLUSIVE,
        )

    def test_undefined_interval_fails_closed(self):
        with self.assertRaises(OfflineQStrengthError):
            classify_value_q_signal(_statistics(None, None))


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class BuildSpecsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        bc_run = train_bc_model(self.dataset)
        q_run = train_q_model(self.dataset)
        bc_checkpoint = save_bc_checkpoint(
            self._tmp / "bc-checkpoint", self.dataset, bc_run
        )
        q_checkpoint = save_q_checkpoint(
            self._tmp / "q-checkpoint", self.dataset, q_run
        )
        retention_root = self._tmp / "retention-root"
        retention_root.mkdir()
        with mock.patch(_EPHEMERAL_PATCH, return_value=()):
            _, self.retained = freeze_candidates(
                bc_checkpoint_path=bc_checkpoint.path,
                q_checkpoint_path=q_checkpoint.path,
                backend="test-store",
                root=retention_root,
                key="offlineq/run-1",
            )

    def test_specs_have_distinct_identities_bound_to_checkpoints(self):
        candidate, baseline = build_specs(self.retained)
        self.assertNotEqual(candidate.identity, baseline.identity)
        self.assertIn(self.retained.q_checkpoint.identity, candidate.identity)
        self.assertIn(self.retained.bc_checkpoint.identity, baseline.identity)

    def test_specs_produce_fresh_policy_instances(self):
        candidate, baseline = build_specs(self.retained)
        first = candidate.factory()
        second = candidate.factory()
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
