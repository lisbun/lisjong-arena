"""#562 局指標がHeuristic engine AABB（#513）と同じ定義で出ること。

#513の手計算済みfixture（``two_block_captures``: 本場・供託・複数ロン・流局・
流し満貫 / パオの分類・分母0を含む）を、Learning protocolのrecord layoutで保存し、
Learningの副指標がAABB layoutでの既存集計と、schema名以外で完全一致することを確かめる。
既存の値は ``test_engine_aabb_secondary_metrics`` が手計算と照合している。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from _engine_aabb_record_fixtures import PRODUCER_REVISION, two_block_captures

from lisjong_arena.heuristic_candidate_engine_aabb import (
    read_kyoku_records,
    summarize_secondary_metrics,
    write_kyoku_records,
)
from lisjong_arena.heuristic_candidate_engine_aabb.errors import KyokuRecordError
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    LAYOUT,
    SECONDARY_METRICS_SCHEMA,
)
from lisjong_arena.learning_candidate_engine_aabb.result import secondary_metrics


class LearningSecondaryMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        captures = two_block_captures()
        write_kyoku_records(
            cls.tmp / "aabb",
            captures,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )
        write_kyoku_records(
            cls.tmp / "learning",
            captures,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
            layout=LAYOUT,
        )
        cls.aabb = read_kyoku_records(cls.tmp / "aabb", event_facts_required=True)
        cls.learning = read_kyoku_records(
            cls.tmp / "learning", event_facts_required=True, layout=LAYOUT
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_same_values_as_the_heuristic_definition(self):
        expected = summarize_secondary_metrics(self.aabb)
        actual = secondary_metrics(self.learning)
        self.assertEqual(actual["schema"], SECONDARY_METRICS_SCHEMA)
        self.assertEqual(
            {k: v for k, v in actual.items() if k != "schema"},
            {k: v for k, v in expected.items() if k != "schema"},
        )

    def test_four_kyoku_metrics_are_present(self):
        actual = secondary_metrics(self.learning)["kyoku_metrics"]
        self.assertEqual(actual["paired"]["win_rate"]["denominator"], "seat_kyokus")
        self.assertEqual(actual["paired"]["deal_in_rate"]["numerator"], "deal_ins")
        self.assertEqual(
            actual["conditional"]["win_hand_points_per_win"]["denominator"], "wins"
        )
        self.assertEqual(
            actual["conditional"]["deal_in_payment_per_deal_in"]["denominator"],
            "deal_in_count",
        )

    def test_records_of_the_other_protocol_are_not_read(self):
        with self.assertRaises(KyokuRecordError):
            read_kyoku_records(
                self.tmp / "aabb", event_facts_required=True, layout=LAYOUT
            )
        with self.assertRaises(KyokuRecordError):
            read_kyoku_records(self.tmp / "learning", event_facts_required=True)


if __name__ == "__main__":
    unittest.main()
