"""#513 保存物だけからの副指標・点数分解（§10.5・§10.6）を手計算値と照合する。

fixture（``two_block_captures``）:

```text
seed 513001  placement 0 = script X、placement 1..5 = script Y
seed 513002  placement 0..5 = script Y
```

seat別の手計算値（script docstringの局から数える）:

```text
script X（10局）  E      S      W      N
  和了            1      2      0      1
  放銃局 / 回数   0      0      2 / 3  0
  和了打点        1600   4800   0      1600
  和了収入        1600   6100   0      2900
  放銃失点/支払   0      0      4800 / 5100
  ツモられ        900    0      1700   900
  流局収支        +1500  +500   -2500  +500
  立直供託        2000   0      0      1000
  終局調整        0      1000   0      0
  素点            +200   +7600  -9300  +1500
  順位 / final    3/-140 1/510  4/-440 2/70
script Y（8局）   Eが8和了（打点19200、収入27600）、Nが8放銃（失点19200、支払27600）
  素点 +27600 / 0 / 0 / -27600、順位 1/2/3/4、final 820/50/-150/-720
```

placement p のA席は (0,1) (0,2) (0,3) (1,2) (1,3) (2,3)。
"""

import math
import shutil
import tempfile
import unittest
from pathlib import Path

from _engine_aabb_record_fixtures import (
    PRODUCER_REVISION,
    SCRIPT_X,
    SEED_MIXED,
    SEED_PLAIN,
    capture,
    two_block_captures,
)

from lisjong_arena.heuristic_candidate_engine_aabb import (
    SecondaryMetricError,
    read_kyoku_records,
    summarize_secondary_metrics,
    write_kyoku_records,
)


class SecondaryMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        path = cls.tmp / "record"
        write_kyoku_records(
            path,
            two_block_captures(),
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )
        # 保存物だけから集計する（process状態を使わない）
        cls.records = read_kyoku_records(path, event_facts_required=True)
        cls.summary = summarize_secondary_metrics(cls.records)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def paired(self, name, layer="kyoku_metrics"):
        return self.summary[layer]["paired"][name]

    def conditional(self, name, layer="kyoku_metrics"):
        return self.summary[layer]["conditional"][name]

    def seed_counts(self, metric, seed_index):
        entry = metric["per_seed"][seed_index]
        return (
            (entry["A"]["numerator"], entry["A"]["denominator"]),
            (entry["B"]["numerator"], entry["B"]["denominator"]),
        )

    def test_blocks(self):
        self.assertEqual(self.summary["blocks"], 2)
        self.assertEqual(self.summary["seeds"], [SEED_MIXED, SEED_PLAIN])
        self.assertTrue(self.summary["descriptive_only"])

    def test_win_rate_seed_weighting_and_interval(self):
        metric = self.paired("win_rate")
        # seed 513001: A = X(E1 + S2) + Y(seat0がAの2配置 x 8) = 19 / 100
        #              B = X(N1)      + Y(seat0がBの3配置 x 8) = 25 / 100
        self.assertEqual(self.seed_counts(metric, 0), ((19, 100), (25, 100)))
        # seed 513002: 全配置Y。A / B とも 3配置 x 8 = 24 / 96
        self.assertEqual(self.seed_counts(metric, 1), ((24, 96), (24, 96)))
        self.assertAlmostEqual(metric["per_seed"][0]["difference"], -0.06)
        self.assertEqual(metric["per_seed"][1]["difference"], 0.0)
        self.assertAlmostEqual(metric["mean_difference"], -0.03)
        sd = math.sqrt(2 * 0.03**2)
        self.assertAlmostEqual(metric["sd"], sd)
        self.assertAlmostEqual(metric["lower"], -0.03 - 1.96 * sd / math.sqrt(2))
        self.assertAlmostEqual(metric["upper"], -0.03 + 1.96 * sd / math.sqrt(2))

    def test_deal_in_rate_counts_a_double_ron_once(self):
        metric = self.paired("deal_in_rate")
        # A: Y の N（seat3）がAの3配置 x 8 = 24。B: X の W 2局 + Y 2配置 x 8 = 18
        self.assertEqual(self.seed_counts(metric, 0), ((24, 100), (18, 100)))

    def test_conditional_values_pool_all_seeds(self):
        hand = self.conditional("win_hand_points_per_win")
        # A: 513001 (1600 + 4800 + 2 x 19200) / 19、513002 (3 x 19200) / 24
        self.assertEqual(
            (hand["A"]["numerator"], hand["A"]["denominator"]), (102_400, 43)
        )
        self.assertEqual(
            (hand["B"]["numerator"], hand["B"]["denominator"]), (116_800, 49)
        )
        self.assertAlmostEqual(hand["A"]["value"], 102_400 / 43)
        income = self.conditional("win_income_per_win")
        self.assertEqual(income["A"]["numerator"], 7_700 + 55_200 + 3 * 27_600)
        self.assertEqual(income["B"]["numerator"], 2_900 + 82_800 + 3 * 27_600)
        deal = self.conditional("deal_in_hand_points_per_deal_in")
        # 放銃回数はダブロンで2と数える（X の W は 1 + 2 = 3）
        self.assertEqual(
            (deal["B"]["numerator"], deal["B"]["denominator"]),
            (4_800 + 2 * 19_200 + 3 * 19_200, 3 + 16 + 24),
        )
        payment = self.conditional("deal_in_payment_per_deal_in")
        self.assertEqual(payment["B"]["numerator"], 5_100 + 2 * 27_600 + 3 * 27_600)
        tenpai = self.conditional("tenpai_rate_at_exhaustive_draw")
        self.assertEqual(
            (tenpai["A"]["numerator"], tenpai["A"]["denominator"]), (2, 12)
        )
        self.assertEqual(
            (tenpai["B"]["numerator"], tenpai["B"]["denominator"]), (1, 12)
        )

    def test_other_kyoku_rates(self):
        self.assertEqual(
            self.seed_counts(self.paired("tsumo_loss_per_kyoku"), 0),
            ((900, 100), (2_600, 100)),
        )
        self.assertEqual(
            self.seed_counts(self.paired("draw_balance_per_kyoku"), 0),
            ((2_000, 100), (-2_000, 100)),
        )
        self.assertEqual(
            self.seed_counts(self.paired("riichi_deposit_per_kyoku"), 0),
            ((2_000, 100), (1_000, 100)),
        )
        self.assertEqual(
            self.seed_counts(self.paired("riichi_rate"), 0), ((2, 100), (1, 100))
        )
        self.assertEqual(
            self.seed_counts(self.paired("pao_payment_per_kyoku"), 0),
            ((0, 100), (0, 100)),
        )
        self.assertEqual(
            self.seed_counts(self.paired("final_adjustment_per_hanchan"), 0),
            ((1_000, 12), (0, 12)),
        )
        # 平均順位: A = X(3 + 1) + Y(4 + 5 + 5 + 6 + 7) = 31、B = 60 - 31
        self.assertEqual(
            self.seed_counts(self.paired("mean_rank"), 0), ((31, 12), (29, 12))
        )

    def test_point_decomposition_matches_raw_and_final_components(self):
        first = self.summary["point_decomposition"][0]
        self.assertEqual(first["seed"], SEED_MIXED)
        self.assertEqual(
            first["A"],
            {
                "raw_points": -19_800,
                "win_income": 62_900,
                "deal_in_payment": 82_800,
                "tsumo_loss": 900,
                "pao_payment": 0,
                "draw_balance": 2_000,
                "riichi_deposit": 2_000,
                "final_award": 1_000,
                "base_points": first["A"]["base_points"],
                "uma_points": first["A"]["uma_points"],
                "oka_points": first["A"]["oka_points"],
                "bankruptcy_points": first["A"]["bankruptcy_points"],
                "final_points": -500,
            },
        )
        self.assertEqual(first["difference"]["raw_points"], -39_600)
        self.assertEqual(first["difference"]["final_points"], -1_000)
        a = first["A"]
        self.assertEqual(
            a["base_points"]
            + a["uma_points"]
            + a["oka_points"]
            + a["bankruptcy_points"],
            a["final_points"],
        )
        # A席のbankruptcy: Yの E(+100) がAの2配置、N(-100) がAの3配置 -> -100
        self.assertEqual(a["bankruptcy_points"], -100)

    def test_rank_distribution(self):
        ranks = self.summary["rank_distribution"]
        # 513001: X A席 rank 3, 1。Y の各配置A席。513002: Y 全配置
        self.assertEqual(sum(ranks["A"].values()), 24)
        self.assertEqual(ranks["A"]["rank_1"], 1 + 2 + 3)
        self.assertEqual(ranks["B"]["rank_1"], 0 + 3 + 3)

    def test_event_fact_metrics(self):
        self.assertEqual(self.summary["event_fact_status"], {"status": "VALID"})
        open_call = self.paired("open_call_rate", "event_fact_metrics")
        # X: S（A）のポン、N（B）の大明槓
        self.assertEqual(self.seed_counts(open_call, 0), ((1, 100), (1, 100)))
        kan = self.paired("kan_per_kyoku", "event_fact_metrics")
        # A: E の暗槓、B: N の大明槓
        self.assertEqual(self.seed_counts(kan, 0), ((1, 100), (1, 100)))
        turn = self.conditional("riichi_turn_mean", "event_fact_metrics")
        self.assertEqual((turn["A"]["numerator"], turn["A"]["denominator"]), (13, 2))
        self.assertEqual((turn["B"]["numerator"], turn["B"]["denominator"]), (7, 1))
        after = self.conditional("win_rate_after_riichi", "event_fact_metrics")
        self.assertEqual(after["A"]["value"], 0.0)
        with_call = self.conditional("win_rate_with_open_call", "event_fact_metrics")
        self.assertEqual(
            (with_call["A"]["numerator"], with_call["A"]["denominator"]), (1, 1)
        )

    def test_incomplete_block_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        records = write_kyoku_records(
            tmp / "partial",
            [capture(61, p, SCRIPT_X) for p in range(5)],
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )
        with self.assertRaisesRegex(SecondaryMetricError, "multiple of 6"):
            summarize_secondary_metrics(records)


if __name__ == "__main__":
    unittest.main()
