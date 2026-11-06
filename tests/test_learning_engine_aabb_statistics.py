"""#562 主指標（平均最終順位）のblock統計・classification。

rowは統計関数が読むfield（seed・placement・seats・final_score）だけを持つ合成値で、
600半荘ぶんでもengineを使わない。
"""

import math
import unittest

from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import placement_roles
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    CANDIDATE_SUPERIOR,
    CHAMPION_SUPERIOR,
    CLASSIFICATION_RULE_ID,
    FORMAL_K,
    INCONCLUSIVE,
    LearningEngineAabbError,
)
from lisjong_arena.learning_candidate_engine_aabb.statistics import (
    block_rank_totals,
    final_points_difference,
    summarize_rank_blocks,
)

# 半荘の型: (Aの2席のrank, Bの2席のrank, rank 1..4のfinal_points)
EVEN = ((1, 4), (2, 3), (100, 10, -10, -100))
"""R_A = R_B = 5、A = B = 0。"""
A_AHEAD = ((1, 3), (2, 4), (11, 10, -1, -20))
"""R_A = 4 < R_B = 6、A = +10 > B = -10。"""
A_MIDDLE = ((2, 3), (1, 4), (400, -100, -110, -190))
"""R_A = R_B = 5、A = -210 < B = +210（rankは同じでも点ではBが大きく勝つ）。"""
B_AHEAD = ((2, 4), (1, 3), (20, 1, -10, -11))
"""R_A = 6 > R_B = 4。"""


def _row(seed, placement, kind):
    a_ranks, b_ranks, points = kind
    queues = {"A": list(a_ranks), "B": list(b_ranks)}
    seats, scores = [], []
    for role in placement_roles(placement):
        rank = queues[role].pop(0)
        seats.append({"policy_identity": f"policy-{role}", "role": role})
        scores.append({"final_points": points[rank - 1], "rank": rank})
    return {
        "final_score": scores,
        "placement": placement,
        "seats": seats,
        "seed": seed,
    }


def _block(seed, kinds):
    return [_row(seed, placement, kind) for placement, kind in enumerate(kinds)]


def _rows(blocks):
    return [row for seed, kinds in blocks for row in _block(seed, kinds)]


class BlockRankTotalsTest(unittest.TestCase):
    def test_role_counts_sign_and_rank_sums(self) -> None:
        blocks = block_rank_totals(_rows([(7, (A_AHEAD,) * 6), (8, (B_AHEAD,) * 6)]))
        self.assertEqual(
            blocks,
            [
                {
                    "difference_ranks": 12,
                    "rank_sum_a": 24,
                    "rank_sum_b": 36,
                    "seed": 7,
                },
                {
                    "difference_ranks": -12,
                    "rank_sum_a": 36,
                    "rank_sum_b": 24,
                    "seed": 8,
                },
            ],
        )

    def test_rejects_a_missing_duplicated_or_reordered_placement(self) -> None:
        rows = _rows([(7, (EVEN,) * 6)])
        cases = {
            "missing": rows[:5],
            "duplicated": rows[:5] + [rows[4]],
            "reordered": [rows[1], rows[0], *rows[2:]],
            "mixed seeds": rows[:5] + [_row(8, 5, EVEN)],
        }
        for name, value in cases.items():
            with self.subTest(name), self.assertRaises(LearningEngineAabbError):
                block_rank_totals(value)

    def test_rejects_a_repeated_seed_block(self) -> None:
        rows = _rows([(7, (EVEN,) * 6)])
        with self.assertRaises(LearningEngineAabbError):
            block_rank_totals(rows + rows)

    def test_rejects_ranks_that_are_not_one_to_four(self) -> None:
        rows = _rows([(7, (EVEN,) * 6)])
        rows[2]["final_score"][0]["rank"] = 2
        with self.assertRaises(LearningEngineAabbError):
            block_rank_totals(rows)

    def test_rejects_a_role_count_other_than_twelve(self) -> None:
        rows = _rows([(7, (EVEN,) * 6)])
        rows[0]["seats"][0]["role"] = "B" if rows[0]["seats"][0]["role"] == "A" else "A"
        with self.assertRaises(LearningEngineAabbError):
            block_rank_totals(rows)


class SummaryTest(unittest.TestCase):
    def test_hundred_synthetic_blocks(self) -> None:
        """600半荘 -> N = 100、mean・SD(K-1)・区間を手計算と照合する。"""
        layouts = [(A_AHEAD,) * 6, (A_AHEAD, A_AHEAD, EVEN, EVEN, EVEN, B_AHEAD)]
        rows = _rows([(1_000 + index, layouts[index % 2]) for index in range(FORMAL_K)])
        self.assertEqual(len(rows), 600)
        blocks = block_rank_totals(rows)
        differences = [block["difference_ranks"] for block in blocks]
        self.assertEqual(differences, [12, 2] * 50)
        summary = summarize_rank_blocks(differences)
        values = [value / 12 for value in differences]
        mean = sum(values) / 100
        sd = math.sqrt(sum((value - mean) ** 2 for value in values) / 99)
        half = 1.96 * sd / 10
        self.assertEqual(summary["k"], 100)
        self.assertAlmostEqual(summary["mean_d"], 7 / 12)
        self.assertAlmostEqual(summary["sd"], sd)
        self.assertAlmostEqual(summary["lower"], mean - half)
        self.assertAlmostEqual(summary["upper"], mean + half)
        self.assertEqual(summary["classification"], CANDIDATE_SUPERIOR)
        self.assertEqual(summary["classification_rule_id"], CLASSIFICATION_RULE_ID)

    def test_four_classifications(self) -> None:
        cases = {
            CANDIDATE_SUPERIOR: [12, 2] * 50,
            CHAMPION_SUPERIOR: [-12, -2] * 50,
            "INCONCLUSIVE (crosses 0)": [12, -12] * 50,
            "INCONCLUSIVE (0 boundary)": [0] * 100,
        }
        for name, differences in cases.items():
            with self.subTest(name):
                summary = summarize_rank_blocks(differences)
                expected = name.split(" (")[0]
                self.assertEqual(summary["classification"], expected)
        boundary = summarize_rank_blocks([0] * 100)
        self.assertEqual((boundary["lower"], boundary["upper"]), (0.0, 0.0))
        self.assertEqual(boundary["classification"], INCONCLUSIVE)

    def test_rank_decides_even_when_points_disagree(self) -> None:
        """rank差は候補有利、final_points差は候補不利でも、判定はrankだけで決まる。"""
        kinds = (A_AHEAD,) * 5 + (A_MIDDLE,)
        rows = _rows([(2_000 + index, kinds) for index in range(4)])
        blocks = block_rank_totals(rows)
        summary = summarize_rank_blocks([b["difference_ranks"] for b in blocks])
        points = final_points_difference(rows)
        self.assertGreater(summary["mean_d"], 0)
        self.assertLess(points["mean_difference"], 0)
        self.assertEqual(summary["classification"], CANDIDATE_SUPERIOR)
        self.assertTrue(points["descriptive_only"])
        self.assertNotIn("classification", points)

    def test_rejects_invalid_differences(self) -> None:
        for value in ([1.0, 2], [True, 2], [25, 0], [-25, 0], [3]):
            with self.subTest(value), self.assertRaises(LearningEngineAabbError):
                summarize_rank_blocks(value)


if __name__ == "__main__":
    unittest.main()
