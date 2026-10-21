"""#513 実engineの1半荘で、取得相当 -> 保存 -> 読込 -> 検査を通す。

selectorはengineの提示候補の最後を選ぶだけのtest用callableで、lisjong Policyも
Arena bridgeも通らない。したがって槓factが得られても、実candidate経由の槓適用・
記録の確認（#472の未確認事項）にはならない。§10.7のrunner end-to-endは#514。
"""

import copy
import dataclasses
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from _engine_aabb_record_fixtures import PRODUCER_REVISION, SCRIPT_Y, capture, seats_for
from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat

from lisjong_arena.heuristic_candidate_engine_aabb import (
    HanchanCapture,
    SecondaryMetricError,
    VerifiedKyokuRecords,
    read_kyoku_records,
    summarize_secondary_metrics,
    write_kyoku_records,
)

_SEED = 513000


class RealEngineHanchanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        calls = Counter()

        def selector(seat):
            def select(observation, options):
                calls[seat] += 1
                return tuple(options)[-1]

            return select

        completions = []
        match = run_hanchan(
            MatchState(seed=_SEED, rules=RuleSet.default()),
            {seat: selector(seat) for seat in Seat},
            on_round_evidence_complete=completions.append,
        )
        counts = tuple(calls[seat] for seat in Seat)
        cls.capture = HanchanCapture(
            match=match,
            seed=_SEED,
            placement=0,
            seats=seats_for(0),
            selector_call_counts=counts,
            policy_call_counts=counts,
            round_evidence=tuple(completions),
        )
        cls.tmp = Path(tempfile.mkdtemp())
        cls.written = write_kyoku_records(
            cls.tmp / "record",
            [cls.capture],
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_record_round_trips_and_matches_engine_values(self):
        read = read_kyoku_records(self.tmp / "record", event_facts_required=True)
        self.assertEqual(read, self.written)
        (row,) = read.hanchans
        match = self.capture.match
        self.assertEqual(len(row["kyokus"]), len(match.history))
        self.assertEqual(row["end_reason"], match.end_reason.value)
        self.assertEqual(
            [s["final_points"] for s in row["final_score"]],
            [match.final_score.for_seat(seat).final_points for seat in Seat],
        )

    def test_real_public_facts_are_projected_and_cross_checked(self):
        (events,) = self.written.event_facts
        kinds = Counter(
            fact["type"] for kyoku in events["kyokus"] for fact in kyoku["facts"]
        )
        # seed 513000 + 最後の候補を選ぶselectorでは、鳴き・槓・立直が起きる
        self.assertGreater(kinds["meld_called"], 0)
        self.assertGreater(kinds["kan_confirmed"], 0)
        self.assertEqual(kinds["riichi_declared"], kinds["riichi_established"])
        contributions = sum(
            len(kyoku["riichi_contributions"])
            for kyoku in self.written.hanchans[0]["kyokus"]
        )
        self.assertEqual(kinds["riichi_established"], contributions)


class ImportBoundaryTest(unittest.TestCase):
    def test_new_record_path_does_not_import_learning(self):
        code = (
            "import sys\n"
            "import lisjong_arena.heuristic_candidate_engine_aabb\n"
            "print(sorted(m for m in sys.modules if m.startswith('lisjong.learning')))\n"
        )
        output = subprocess.check_output([sys.executable, "-c", code], text=True)
        self.assertEqual(output.strip(), "[]")


class UnclassifiedTransferTest(unittest.TestCase):
    def test_metrics_fail_closed_on_an_unclassifiable_transfer(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        records = write_kyoku_records(
            tmp / "record",
            [capture(71, p, SCRIPT_Y) for p in range(6)],
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )
        rows = copy.deepcopy(list(records.hanchans))
        # ロン局のronをtsumoとして記録した値（K8の合計は変わらない）
        rows[0]["kyokus"][0]["transfers"][0]["reason"] = "tsumo"
        tampered = dataclasses.replace(records, hanchans=tuple(rows))
        self.assertIsInstance(tampered, VerifiedKyokuRecords)
        with self.assertRaisesRegex(SecondaryMetricError, "unclassified"):
            summarize_secondary_metrics(tampered)


if __name__ == "__main__":
    unittest.main()
