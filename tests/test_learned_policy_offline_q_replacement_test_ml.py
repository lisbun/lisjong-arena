"""Checkpoint-bound replacement TEST evaluation tests (Issue #140).

replacement TEST pathの要点は、support setをTEST rowからもTRAIN rowからも
再計算せず、Q checkpointへidentity-boundされた`supported_indices`を正本に
することである。ここではその境界と、hard validity gateが数える値を固定する。
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import (
    write_synthetic_dataset,
    write_synthetic_replacement_test,
)

from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.errors import OfflineQArtifactError
from lisjong_arena.learned_policy_offline_q.exposure_evaluation import (
    evaluate_bc_test,
    evaluate_q_with_support_mask,
)
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint,
    train_q_model,
)
from lisjong_arena.learned_policy_offline_q.replacement_test import (
    count_unsupported_bootstrap,
    load_replacement_test_tensors,
    support_complete_flags,
    support_mask_from_checkpoint,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class ReplacementTestEvaluationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        self.artifact = write_synthetic_replacement_test(
            self._tmp / "replacement", rows_per_game=6
        )
        self.tensors = load_replacement_test_tensors(self.artifact)

    def _q_checkpoint(self):
        run = train_q_model(self.dataset)
        return save_checkpoint(self._tmp / "q-checkpoint", self.dataset, run)

    def test_tensors_match_the_artifact_totals(self):
        self.assertEqual(self.tensors.row_count, self.artifact.row_count)
        self.assertEqual(
            int(self.tensors.terminal.sum()), self.artifact.terminal_row_count
        )

    def test_support_mask_comes_from_the_checkpoint_not_the_test_rows(self):
        checkpoint = self._q_checkpoint()
        mask = support_mask_from_checkpoint(checkpoint.supported_indices)
        self.assertEqual(int(mask.sum()), len(checkpoint.supported_indices))
        for index in checkpoint.supported_indices:
            self.assertTrue(bool(mask[index]))

    def test_evaluation_needs_no_train_tensors(self):
        """replacement TEST評価はoriginal TRAIN rowsを一切要求しない。"""
        checkpoint = self._q_checkpoint()
        mask = support_mask_from_checkpoint(checkpoint.supported_indices)
        diagnostics = evaluate_q_with_support_mask(checkpoint.model, self.tensors, mask)
        self.assertEqual(diagnostics.row_count, self.artifact.row_count)
        self.assertEqual(diagnostics.finite_q_rate, 1.0)
        self.assertGreaterEqual(diagnostics.selected_action_huber_loss, 0.0)

    def test_bc_diagnostics_run_on_the_same_population(self):
        run = train_bc_model(self.dataset)
        diagnostics = evaluate_bc_test(run.model, self.tensors)
        self.assertGreater(diagnostics.choice_rows, 0)
        self.assertGreaterEqual(diagnostics.choice_exact_agreement, 0.0)
        self.assertLessEqual(diagnostics.choice_exact_agreement, 1.0)

    def test_fully_supported_population_reports_zero_unsupported_bootstrap(self):
        checkpoint = self._q_checkpoint()
        mask = support_mask_from_checkpoint(checkpoint.supported_indices)
        self.assertEqual(count_unsupported_bootstrap(self.tensors, mask), 0)
        complete = support_complete_flags(self.tensors, mask)
        self.assertEqual(int(complete.sum()), self.tensors.row_count)

    def test_unsupported_next_legal_action_is_counted_and_fails_closed(self):
        """next legal actionが1つでもTRAIN-unsupportedならfail closedする。

        subset上でbootstrapして黙って通さないことが#140のlocked contractである。
        """
        from lisjong_arena.learned_policy_offline_q.errors import (
            OfflineQProtocolError,
        )
        from lisjong_arena.learned_policy_offline_q.q_training import (
            compute_td_targets,
        )

        checkpoint = self._q_checkpoint()
        mask = support_mask_from_checkpoint(checkpoint.supported_indices)
        # fixtureのlegal setは{0,1,2,3}。index 3だけをsupportから外すと、
        # 全nonterminal rowがpartial-overlapのfail closed条件に入る。
        narrowed = mask.clone()
        narrowed[3] = False

        nonterminal = int((~self.tensors.terminal).sum())
        self.assertGreater(nonterminal, 0)
        self.assertEqual(
            count_unsupported_bootstrap(self.tensors, narrowed), nonterminal
        )
        with self.assertRaises(OfflineQProtocolError):
            compute_td_targets(checkpoint.model, self.tensors, narrowed)
        with self.assertRaises(OfflineQProtocolError):
            evaluate_q_with_support_mask(checkpoint.model, self.tensors, narrowed)

    def test_support_mask_rejects_an_empty_or_invalid_support_set(self):
        with self.assertRaises(OfflineQArtifactError):
            support_mask_from_checkpoint(frozenset())
        with self.assertRaises(OfflineQArtifactError):
            support_mask_from_checkpoint(frozenset({-1}))


if __name__ == "__main__":
    unittest.main()
