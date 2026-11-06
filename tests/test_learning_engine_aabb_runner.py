"""#562 正式runner・result・観測値・offline verify（合成600半荘）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘を返す ``fake_play*`` を
使う。合成値であり、棋力結果・正式allocationではない。完走する600半荘の実行は
class単位で1回だけ行い、改ざんtestはその保存物のcopyに対して行う。
"""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    read_kyoku_records,
)
from lisjong_arena.learning_candidate_engine_aabb import result as result_module
from lisjong_arena.learning_candidate_engine_aabb import runner
from lisjong_arena.learning_candidate_engine_aabb.lock import (
    LearningEngineAabbLockError,
    save_lock,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    LAYOUT,
    LearningEngineAabbError,
)
from lisjong_arena.learning_candidate_engine_aabb.result import (
    LearningEngineAabbResultError,
    build_result,
    verify_result,
)
from lisjong_arena.learning_candidate_engine_aabb.runner import run_formal_event


class _EventCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.lock = fx.lock_document(self.tmp)
        self.lock_path = save_lock(self.lock, self.tmp / "lock.json")

    def run_event(self, play=fx.fake_play, **kwargs):
        return run_formal_event(
            self.lock_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=fx.gate,
            **kwargs,
        )

    def assert_no_result(self):
        for name in ("raw", "result.json", "observations.json"):
            self.assertFalse((self.tmp / name).exists(), name)
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if "staging" in p.name], []
        )
        stops = list((self.tmp / "diagnostics").glob("stop-*.json"))
        self.assertEqual(len(stops), 1)
        stop = json.loads(stops[0].read_text("utf-8"))
        self.assertTrue(stop["not_a_result"])
        self.assertEqual(stop["status"], "STOP / INVALID")
        self.assertEqual(stop["lock_digest"], self.lock["lock_digest"])
        shutil.rmtree(self.tmp / "diagnostics")
        return stop


class StopTest(_EventCase):
    """失敗はevent全体のSTOPで、成功した半荘だけの結果を出さない。"""

    def test_worker_failures_stop_the_event(self):
        for play, pattern in (
            (fx.fake_play_policy_error, "synthetic Policy failure"),
            (fx.fake_play_other_environment, "environment differs"),
            (fx.fake_play_wrong_placement, "different hanchan"),
        ):
            with self.subTest(play=play.__name__):
                with self.assertRaisesRegex(RunStopped, pattern):
                    self.run_event(play)
                stop = self.assert_no_result()
                self.assertGreaterEqual(stop["completed_hanchans"], 0)

    def test_timeout_stops_the_event(self):
        limits = {**fx.LIMITS, "timeout_seconds": 3}
        lock = fx.lock_document(self.tmp, limits=limits)
        path = save_lock(lock, self.tmp / "short-lock.json")
        with self.assertRaisesRegex(RunStopped, "timeout"):
            run_formal_event(
                path,
                ledger_path=self.ledger_path,
                play=fx.fake_play_slow,
                live_gate=fx.gate,
            )
        self.assertFalse((self.tmp / "result.json").exists())
        self.assertFalse((self.tmp / "raw").exists())

    def test_peak_rss_limit_stops_the_event(self):
        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            self.run_event(fx.fake_play_large_rss)
        stop = self.assert_no_result()
        self.assertEqual(stop["completed_hanchans"], 4)

    def test_wall_limit_stops_the_event(self):
        ticks = iter(range(0, 10**9, 2_000))
        with self.assertRaisesRegex(RunStopped, "wall time"):
            self.run_event(clock=lambda: next(ticks))
        stop = self.assert_no_result()
        self.assertEqual(stop["completed_hanchans"], 1)

    def test_live_gate_failure_stops_before_any_hanchan(self):
        def gate(lock, *, ledger):
            raise LearningEngineAabbLockError("live binding differs")

        with self.assertRaisesRegex(LearningEngineAabbLockError, "live binding"):
            run_formal_event(
                self.lock_path, ledger_path=self.ledger_path, live_gate=gate
            )
        stop = self.assert_no_result()
        self.assertEqual(stop["completed_hanchans"], 0)

    def test_existing_destination_is_never_overwritten(self):
        (self.tmp / "observations.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(LearningEngineAabbLockError, "write-once"):
            self.run_event()


class SuccessfulEventTest(unittest.TestCase):
    """完走した600半荘のevent（class単位で1回だけ実行）。"""

    @classmethod
    def setUpClass(cls):
        cls.source = Path(tempfile.mkdtemp())
        case = _EventCase()
        case.tmp = cls.source
        case.ledger_path = cls.source / "ledger.json"
        seed_registry.write_ledger(case.ledger_path, fx.ledger())
        case.lock = fx.lock_document(cls.source)
        case.lock_path = save_lock(case.lock, cls.source / "lock.json")
        cls.lock = case.lock
        with mock.patch.object(
            runner, "build_result", side_effect=OSError("injected publish failure")
        ):
            try:
                case.run_event()
            except RunStopped as error:
                cls.publish_failure = str(error)
        cls.after_publish_failure = sorted(p.name for p in cls.source.iterdir())
        shutil.rmtree(cls.source / "diagnostics")
        cls.result = case.run_event()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.source, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("raw",):
            shutil.copytree(self.source / name, self.tmp / name)
        for name in ("lock.json", "result.json", "observations.json"):
            shutil.copy2(self.source / name, self.tmp / name)

    def verify(self, **paths):
        arguments = {
            "lock_path": self.tmp / "lock.json",
            "raw_path": self.tmp / "raw",
            "result_path": self.tmp / "result.json",
            "observations_path": self.tmp / "observations.json",
        }
        arguments.update(paths)
        return verify_result(**arguments)

    def rewrite(self, name, mutate):
        path = self.tmp / name
        document = json.loads(path.read_text("utf-8"))
        mutate(document)
        path.write_text(canonical_json_text(document), encoding="utf-8", newline="\n")

    def test_publish_failure_left_nothing_and_the_same_lock_reran(self):
        self.assertIn("injected publish failure", self.publish_failure)
        self.assertEqual(
            self.after_publish_failure, ["diagnostics", "ledger.json", "lock.json"]
        )

    def test_result_counts_sign_and_classification(self):
        result = self.result
        self.assertEqual(result["evaluation_kind"], "FORMAL")
        self.assertEqual(
            result["protocol_id"], "arena-learning-candidate-engine-aabb-v1"
        )
        self.assertEqual(result["lock_digest"], self.lock["lock_digest"])
        self.assertEqual(len(result["blocks"]), 100)
        self.assertEqual(result["statistic"]["k"], 100)
        for block in result["blocks"]:
            self.assertEqual(block["rank_sum_a"] + block["rank_sum_b"], 60)
            self.assertEqual(
                block["difference_ranks"], block["rank_sum_b"] - block["rank_sum_a"]
            )
        x_blocks = {
            b["difference_ranks"] for b in result["blocks"] if b["seed"] in fx.X_SEEDS
        }
        y_blocks = {
            b["difference_ranks"]
            for b in result["blocks"]
            if b["seed"] not in fx.X_SEEDS
        }
        self.assertEqual(y_blocks, {0})
        self.assertEqual(len(x_blocks), 1)
        self.assertNotEqual(x_blocks, {0})
        records = read_kyoku_records(
            self.tmp / "raw", event_facts_required=False, layout=LAYOUT
        )
        self.assertEqual(len(records.hanchans), 600)
        self.assertEqual(
            sum(
                seat["role"] == "A" for row in records.hanchans for seat in row["seats"]
            ),
            1200,
        )
        self.assertEqual(result["event_fact_status"], {"status": "VALID"})
        metrics = result["secondary_metrics"]
        self.assertTrue(metrics["descriptive_only"])
        self.assertEqual(
            metrics["schema"],
            "arena-learning-candidate-engine-aabb-secondary-metrics-v1",
        )
        for name in ("win_rate", "deal_in_rate", "mean_rank"):
            self.assertIn(name, metrics["kyoku_metrics"]["paired"])
        for name in ("win_hand_points_per_win", "deal_in_payment_per_deal_in"):
            self.assertIn(name, metrics["kyoku_metrics"]["conditional"])
        self.assertTrue(result["final_points_difference"]["descriptive_only"])
        self.assertEqual(self.verify(), result)

    def test_observations_are_recorded_and_hashed(self):
        observations = json.loads((self.tmp / "observations.json").read_text("utf-8"))
        self.assertTrue(observations["not_deterministic"])
        self.assertEqual(len(observations["hanchans"]), 600)
        self.assertEqual(observations["summary"]["max_peak_rss_kib"], 2048)
        decisions = observations["summary"]["decision_seconds"]
        self.assertEqual(
            decisions["A"]["decisions"] + decisions["B"]["decisions"], 600 * 166
        )
        self.assertEqual(
            self.result["observations_sha256"],
            result_module.observations_sha256(self.tmp / "observations.json"),
        )

    def test_verify_in_a_separate_process(self):
        code = (
            "import sys\n"
            "from lisjong_arena.learning_candidate_engine_aabb.result import verify_result\n"
            "r = verify_result(lock_path=sys.argv[1], raw_path=sys.argv[2], "
            "result_path=sys.argv[3], observations_path=sys.argv[4])\n"
            "print(r['result_digest'])\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, *(str(self.tmp / n) for n in ("lock.json", "raw", "result.json", "observations.json"))],
            text=True,
        )  # fmt: skip
        self.assertEqual(output.strip(), self.result["result_digest"])

    def test_recorded_statistics_are_rederived(self):
        original = (self.tmp / "result.json").read_text("utf-8")
        for mutate in (
            lambda d: d["statistic"].update(classification="CANDIDATE SUPERIOR"),
            lambda d: d["statistic"].update(mean_d=0.5),
            lambda d: d["blocks"][0].update(difference_ranks=3),
            lambda d: d["final_points_difference"].update(mean_difference=1.0),
            lambda d: d["secondary_metrics"]["kyoku_metrics"]["paired"]["win_rate"].update(mean_difference=0.5),
            lambda d: d.update(observations_sha256="0" * 64),
        ):  # fmt: skip
            self.rewrite("result.json", mutate)
            with self.assertRaises(LearningEngineAabbResultError):
                self.verify()
            (self.tmp / "result.json").write_text(
                original, encoding="utf-8", newline="\n"
            )
        self.verify()

    def test_unknown_result_schema(self):
        self.rewrite("result.json", lambda d: d.update(schema="arena-other-result-v1"))
        with self.assertRaisesRegex(LearningEngineAabbResultError, "schema"):
            self.verify()

    def test_observation_tampering_is_rejected(self):
        original = (self.tmp / "observations.json").read_text("utf-8")
        for mutate in (
            lambda d: d["hanchans"][3].update(wall_seconds=0.75),
            lambda d: d["hanchans"].pop(),
            lambda d: d["hanchans"][0].update(placement=1),
            lambda d: d["hanchans"][5].update(peak_rss_kib=None),
            lambda d: d["summary"].update(max_peak_rss_kib=1),
            lambda d: d.update(lock_digest="0" * 64),
        ):
            self.rewrite("observations.json", mutate)
            with self.assertRaises(LearningEngineAabbResultError):
                self.verify()
            (self.tmp / "observations.json").write_text(
                original, encoding="utf-8", newline="\n"
            )
        self.verify()

    def test_raw_tampering_is_rejected(self):
        payload = self.tmp / "raw" / "kyoku-records.jsonl"
        text = payload.read_bytes()
        payload.write_bytes(text.replace(b'"rank":1', b'"rank":2', 1))
        with self.assertRaises(LearningEngineAabbResultError):
            self.verify()

    def test_lock_tampering_is_rejected(self):
        self.rewrite("lock.json", lambda d: d["population"].update(k=99))
        with self.assertRaises(LearningEngineAabbError):
            self.verify()

    def test_records_must_match_the_schedule(self):
        """欠落・重複・順序違いの半荘を、schedule照合で拒否する（書込前と同じ検査）。"""
        records = read_kyoku_records(
            self.tmp / "raw", event_facts_required=False, layout=LAYOUT
        )
        rows = list(records.hanchans)
        cases = {
            "missing": rows[:-1],
            "duplicated": rows[:-1] + [rows[-2]],
            "reordered": [rows[1], rows[0], *rows[2:]],
        }
        for name, hanchans in cases.items():
            with self.subTest(name):
                tampered = copy.copy(records)
                object.__setattr__(tampered, "hanchans", tuple(hanchans))
                with self.assertRaises(LearningEngineAabbError):
                    build_result(
                        self.lock,
                        tampered,
                        observations_digest=self.result["observations_sha256"],
                        aggregation_revision=fx.ARENA_REVISION,
                    )

    def test_records_of_another_lock_are_rejected(self):
        other_tmp = self.tmp / "other"
        other_tmp.mkdir()
        other = fx.lock_document(other_tmp, limits={**fx.LIMITS, "wall_seconds": 7_200})
        save_lock(other, self.tmp / "other-lock.json")
        with self.assertRaises(LearningEngineAabbError):
            self.verify(lock_path=self.tmp / "other-lock.json")


if __name__ == "__main__":
    unittest.main()
