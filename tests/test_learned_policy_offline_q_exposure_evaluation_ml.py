"""One-shot TEST diagnostics tests (Issue #140)."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.exposure_evaluation import (
    evaluate_bc_test,
    evaluate_q_test,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.learned_policy_offline_q.q_training import train_q_model
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class ExposureEvaluationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        self.tensors = load_split_tensors(self.dataset)

    def test_bc_test_diagnostics_are_finite_and_bounded(self):
        run = train_bc_model(self.dataset)
        diagnostics = evaluate_bc_test(run.model, self.tensors[Split.TEST])
        self.assertGreater(diagnostics.choice_rows, 0)
        self.assertGreaterEqual(diagnostics.choice_masked_cross_entropy, 0.0)
        self.assertGreaterEqual(diagnostics.choice_exact_agreement, 0.0)
        self.assertLessEqual(diagnostics.choice_exact_agreement, 1.0)

    def test_q_test_diagnostics_report_finite_q_rate_and_residual(self):
        run = train_q_model(self.dataset)
        diagnostics = evaluate_q_test(
            run.model, self.tensors[Split.TRAIN], self.tensors[Split.TEST]
        )
        self.assertEqual(diagnostics.row_count, self.tensors[Split.TEST].row_count)
        self.assertEqual(diagnostics.finite_q_rate, 1.0)
        self.assertGreaterEqual(diagnostics.selected_action_huber_loss, 0.0)


if __name__ == "__main__":
    unittest.main()
