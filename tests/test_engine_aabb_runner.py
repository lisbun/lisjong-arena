"""#514 正式runner・result・offline verify（合成2 block）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘を返す``fake_play*``を使う。
合成値であり、棋力結果・正式allocationではない。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _engine_aabb_protocol_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.lock import (
    EngineAabbLockError,
    save_lock,
)
from lisjong_arena.heuristic_candidate_engine_aabb.result import (
    EngineAabbResultError,
    verify_result,
)
from lisjong_arena.heuristic_candidate_engine_aabb.runner import run_formal_event


class _EventCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.lock = fx.lock_document(self.tmp)
        self.lock_path = save_lock(self.lock, self.tmp / "lock.json")

    def run_event(self, play=fx.fake_play):
        return run_formal_event(
            self.lock_path, ledger_path=self.ledger_path, play=play, live_gate=fx.gate
        )

    def assert_no_result(self):
        self.assertFalse((self.tmp / "raw").exists())
        self.assertFalse((self.tmp / "result.json").exists())
        stops = list((self.tmp / "diagnostics").glob("stop-*.json"))
        self.assertEqual(len(stops), 1)
        stop = json.loads(stops[0].read_text("utf-8"))
        self.assertTrue(stop["not_a_result"])
        self.assertEqual(stop["status"], "STOP / INVALID")
        return stop

    def verify(self):
        return verify_result(
            lock_path=self.lock_path,
            raw_path=self.tmp / "raw",
            result_path=self.tmp / "result.json",
        )


class SuccessfulEventTest(_EventCase):
    def test_two_block_event(self):
        result = self.run_event()
        self.assertEqual(result["evaluation_kind"], "FORMAL")
        self.assertEqual(result["lock_digest"], self.lock["lock_digest"])
        self.assertEqual(
            [(b["seed"], b["s_a_internal"], b["s_b_internal"], b["difference_internal"]) for b in result["blocks"]],
            [(fx.SEED_A, -500, 500, -1000), (fx.SEED_B, 0, 0, 0)],
        )  # fmt: skip
        statistic = result["statistic"]
        self.assertEqual(statistic["k"], 2)
        self.assertAlmostEqual(statistic["mean_d"], -1000 / 120 / 2)
        self.assertAlmostEqual(statistic["sd"], (1000 / 120) / 2**0.5)
        self.assertEqual(statistic["classification"], "INCONCLUSIVE")
        self.assertEqual(result["event_fact_status"], {"status": "VALID"})
        self.assertIsNotNone(result["secondary_metrics"]["event_fact_metrics"])
        self.assertEqual(self.verify(), result)

    def test_verify_in_a_separate_process(self):
        result = self.run_event()
        code = (
            "import sys\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb.result import verify_result\n"
            "r = verify_result(lock_path=sys.argv[1], raw_path=sys.argv[2], result_path=sys.argv[3])\n"
            "print(r['result_digest'])\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(self.lock_path), str(self.tmp / "raw"), str(self.tmp / "result.json")],
            text=True,
        )  # fmt: skip
        self.assertEqual(output.strip(), result["result_digest"])

    def test_written_event_is_never_rerun(self):
        self.run_event()
        with self.assertRaisesRegex(EngineAabbLockError, "write-once"):
            self.run_event()

    def test_layer2_only_failure_keeps_the_primary_result(self):
        result = self.run_event(fx.fake_play_missing_evidence)
        self.assertEqual(result["event_fact_status"]["status"], "INVALID")
        self.assertIn("not captured", result["event_fact_status"]["reason"])
        self.assertIsNone(result["secondary_metrics"]["event_fact_metrics"])
        self.assertEqual(result["blocks"][0]["difference_internal"], -1000)
        self.assertEqual(self.verify(), result)


class StopTest(_EventCase):
    def test_failures_never_produce_a_result(self):
        for play, pattern in (
            (fx.fake_play_policy_error, "synthetic Policy failure"),
            (fx.fake_play_mapping_error, "UnmappedActionError"),
            (fx.fake_play_other_environment, "environment differs"),
            (fx.fake_play_count_mismatch, "K12"),
            (fx.fake_play_wrong_seed, "different hanchan"),
        ):
            with self.subTest(play=play.__name__):
                with self.assertRaisesRegex(EngineAabbRecordError, pattern):
                    self.run_event(play)
                stop = self.assert_no_result()
                self.assertIn(pattern.split()[0], stop["error"])
                shutil.rmtree(self.tmp / "diagnostics")

    def test_timeout_stops_the_event(self):
        lock = fx.lock_document(self.tmp, timeout_seconds=3)
        path = save_lock(lock, self.tmp / "short-lock.json")
        with self.assertRaisesRegex(RunStopped, "timeout"):
            run_formal_event(
                path,
                ledger_path=self.ledger_path,
                play=fx.fake_play_slow,
                live_gate=fx.gate,
            )
        self.assert_no_result()

    def test_aborted_event_can_be_rerun_with_the_same_lock(self):
        with self.assertRaises(RunStopped):
            self.run_event(fx.fake_play_policy_error)
        result = self.run_event()
        self.assertEqual(self.verify(), result)

    def test_unreadable_ledger_has_no_fallback(self):
        self.ledger_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(RunStopped):
            self.run_event()
        self.assert_no_result()


class VerifyTamperingTest(_EventCase):
    def setUp(self):
        super().setUp()
        self.result = self.run_event()
        self.result_path = self.tmp / "result.json"

    def rewrite_result(self, mutate):
        document = json.loads(self.result_path.read_text("utf-8"))
        mutate(document)
        self.result_path.write_text(
            canonical_json_text(document), encoding="utf-8", newline="\n"
        )

    def test_recorded_statistics_are_rederived(self):
        for mutate in (
            lambda d: d["statistic"].update(classification="CANDIDATE SUPERIOR"),
            lambda d: d["statistic"].update(mean_d=0.0),
            lambda d: d["blocks"][0].update(difference_internal=-999),
            lambda d: d["secondary_metrics"]["kyoku_metrics"]["paired"]["win_rate"].update(mean_difference=0.5),
            lambda d: d.update(incumbent_role="fixed-c0"),
            lambda d: d.update(aggregation_revision="f" * 40),
        ):  # fmt: skip
            original = self.result_path.read_text("utf-8")
            self.rewrite_result(mutate)
            with self.assertRaises(EngineAabbResultError):
                self.verify()
            self.result_path.write_text(original, encoding="utf-8", newline="\n")
        self.verify()

    def test_unknown_result_schema(self):
        self.rewrite_result(lambda d: d.update(schema="arena-other-result-v1"))
        with self.assertRaisesRegex(EngineAabbResultError, "schema"):
            self.verify()

    def test_raw_tampering_is_rejected(self):
        payload = self.tmp / "raw" / "kyoku-records.jsonl"
        payload.write_bytes(
            payload.read_bytes().replace(
                b'"final_points":510', b'"final_points":511', 1
            )
        )
        with self.assertRaises(EngineAabbRecordError):
            self.verify()

    def test_lock_tampering_is_rejected(self):
        lock = json.loads(self.lock_path.read_text("utf-8"))
        lock["population"]["k"] = 3
        self.lock_path.write_text(
            canonical_json_text(lock), encoding="utf-8", newline="\n"
        )
        with self.assertRaises(EngineAabbRecordError):
            self.verify()

    def test_record_from_another_lock_is_rejected(self):
        (self.tmp / "other").mkdir()
        other = fx.lock_document(self.tmp / "other", incumbent_role="fixed-c0")
        other_path = save_lock(other, self.tmp / "other-lock.json")
        with self.assertRaisesRegex(EngineAabbRecordError, "bindings"):
            verify_result(
                lock_path=other_path,
                raw_path=self.tmp / "raw",
                result_path=self.result_path,
            )


if __name__ == "__main__":
    unittest.main()
