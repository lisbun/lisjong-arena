"""Issue #406 補助paired metricのunit test（手計算値とfail closed）。"""

import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

from _pure_offense_benchmark_fixtures import OTHER, OTHER_ARM, REFERENCE, save_arm

from lisjong_arena.pure_offense_benchmark import supplementary
from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm
from lisjong_arena.pure_offense_benchmark.summary import PureOffenseSummaryError


class SupplementaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reference_dir = save_arm(
            self.root, "reference", REFERENCE, lambda seed, focal: ("draw", None)
        )
        self.other_dir = save_arm(
            self.root, "other", OTHER, lambda seed, focal: OTHER_ARM[(seed, focal)]
        )

    def _document(self):
        return supplementary.build_supplementary(
            [load_benchmark_arm(self.reference_dir), load_benchmark_arm(self.other_dir)]
        )

    def test_hand_computed_block_paired_differences(self) -> None:
        document = self._document()
        self.assertIsNone(document["terminal_classification"])
        self.assertEqual(document["seed_block_count"], 2)
        (comparison,) = document["comparisons"]
        self.assertEqual(comparison["difference"], "other - reference")
        metrics = comparison["metrics"]
        # seed 1: tsumo, tsumo, tenpai draw, deal-in / seed 2: tsumo, noten draw,
        # tenpai draw, deal-in。referenceは全局noten流局。
        self.assertAlmostEqual(
            metrics["formal_tenpai_reached"]["mean_difference"], 0.625
        )
        self.assertAlmostEqual(metrics["deal_in"]["mean_difference"], 0.25)
        self.assertAlmostEqual(metrics["deal_in"]["paired_sd"], 0.0)
        self.assertAlmostEqual(metrics["deal_in_loss"]["mean_difference"], 500.0)
        self.assertAlmostEqual(metrics["win_points"]["mean_difference"], 1125.0)
        self.assertAlmostEqual(
            metrics["win_points"]["paired_sd"], math.sqrt(2 * 375.0**2)
        )
        self.assertEqual(metrics["riichi_declared"]["mean_difference"], 0.0)
        self.assertEqual(metrics["abortive_draw"]["mean_difference"], 0.0)
        for value in metrics.values():
            self.assertEqual(value["n_seed_blocks"], 2)
        other = document["arms"][1]
        self.assertEqual(other["kyoku_count"], 8)
        self.assertAlmostEqual(other["per_kyoku_mean"]["win_points"], 1125.0)

    def test_cli_writes_once_and_prints(self) -> None:
        out = self.root / "supplementary.json"
        arguments = [str(self.reference_dir), str(self.other_dir), "--out", str(out)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(supplementary.main(arguments), 0)
        self.assertIn("paired: other - reference", stdout.getvalue())
        self.assertEqual(
            json.loads(out.read_text(encoding="utf-8"))["seed_block_count"], 2
        )
        with self.assertRaises(FileExistsError):
            supplementary.main(arguments)

    def test_unpairable_arms_fail_closed(self) -> None:
        arm = load_benchmark_arm(self.reference_dir)
        with self.assertRaises(PureOffenseSummaryError):
            supplementary.build_supplementary([arm, arm])


if __name__ == "__main__":
    unittest.main()
