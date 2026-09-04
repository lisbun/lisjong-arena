"""Offline Q split-tensor loading tests (Issue #140)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors


class SplitTensorsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_split_tensors_partition_the_dataset_by_whole_hanchan(self):
        dataset = write_synthetic_dataset(self.destination, rows_per_game=6)
        tensors = load_split_tensors(dataset)
        total_rows = sum(tensors[split].row_count for split in Split)
        self.assertEqual(total_rows, dataset.row_count)

    def test_every_behavior_action_is_legal_in_its_own_mask(self):
        dataset = write_synthetic_dataset(self.destination, rows_per_game=6)
        tensors = load_split_tensors(dataset)
        for split in Split:
            entry = tensors[split]
            legal = entry.legal_mask.gather(1, entry.behavior_action_index.unsqueeze(1))
            self.assertTrue(bool(legal.all()))

    def test_nonterminal_rows_have_at_least_two_next_legal_actions(self):
        dataset = write_synthetic_dataset(self.destination, rows_per_game=6)
        tensors = load_split_tensors(dataset)
        for split in Split:
            entry = tensors[split]
            nonterminal = ~entry.terminal
            counts = entry.next_legal_mask[nonterminal].sum(dim=1)
            self.assertTrue(bool((counts >= 2).all()))

    def test_terminal_rows_are_marked_and_present(self):
        dataset = write_synthetic_dataset(self.destination, rows_per_game=6)
        tensors = load_split_tensors(dataset)
        total_terminal = sum(int(tensors[split].terminal.sum()) for split in Split)
        self.assertEqual(
            total_terminal, dataset.manifest["totals"]["terminal_row_count"]
        )


if __name__ == "__main__":
    unittest.main()
