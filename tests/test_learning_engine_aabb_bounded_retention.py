"""#578: Learning正式runのbounded保持（合成fixtureのみ）。

実engine・実Policy・正式seed・AWSは使わない。合成600半荘を、変更前の全件保持の
保存処理（``write_kyoku_records`` + ``build_observations`` + ``build_result``）と
新しい逐次保存に通し、科学的な保存物のbytesが一致することを確かめる。wall秒は
実行ごとに変わる観測値なので、観測値の一致はwall秒を除いて比べる。
"""

import gc
import json
import random
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import record_store
from lisjong_arena.heuristic_candidate_engine_aabb.errors import KyokuRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    RunStopped,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    EVENT_FACT_FILENAME,
    KYOKU_RECORD_FILENAME,
    MANIFEST_FILENAME,
    HanchanCapture,
    KyokuRecordStream,
    write_kyoku_records,
)
from lisjong_arena.learning_candidate_engine_aabb import runner
from lisjong_arena.learning_candidate_engine_aabb.lock import save_lock
from lisjong_arena.learning_candidate_engine_aabb.parallel import (
    stream_parallel_tasks,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import LAYOUT
from lisjong_arena.learning_candidate_engine_aabb.result import (
    build_observations,
    build_result,
    observations_sha256,
    record_bindings,
    verify_result,
)

RAW_FILES = (KYOKU_RECORD_FILENAME, EVENT_FACT_FILENAME, MANIFEST_FILENAME)


def _slow_head_play(task):
    """先頭の半荘だけ遅い（後続の完了が先に届く）。"""
    if task.placement == 0:
        time.sleep(1.5)
    return fx.fake_play(task)


def _crashing_play(task):
    import os

    if (task.seed, task.placement) == (fx.SEEDS[2], 1):
        os._exit(17)
    return fx.fake_play(task)


def _live_captures() -> int:
    gc.collect()
    return sum(type(item) is HanchanCapture for item in gc.get_objects())


def _executed(evidence_missing=frozenset()):
    """schedule順の合成600半荘（in-process）。``evidence_missing`` の位置は第2層を欠く。"""
    return [
        ExecutedHanchan(
            fx.capture(seed, placement, evidence=index not in evidence_missing),
            0.5,
            2048,
            None,
            dict(fx.ANALYSIS),
        )
        for index, (seed, placement) in enumerate(
            (seed, placement) for seed in fx.SEEDS for placement in range(6)
        )
    ]


def _stream_in_order(staging, executed, order, **kwargs):
    """``order`` の順に完了したとして、runnerと同じsinkで逐次保存する。"""
    stream = KyokuRecordStream(
        staging,
        producer_revision=fx.ARENA_REVISION,
        event_facts_required=False,
        bindings=kwargs.pop("bindings", record_stream_bindings()),
        layout=LAYOUT,
        capture_type=HanchanCapture,
    )
    sink = runner._CanonicalSink(stream)
    try:
        for index in order:
            sink(index, executed[index])
        sink.require_complete(len(executed))
        return stream.finish(), sink
    finally:
        stream.close()


def record_stream_bindings():
    return record_bindings(fx.lock_document(Path(tempfile.gettempdir())))


class StreamEquivalenceTest(unittest.TestCase):
    """変更前の全件保持の保存と、同じbytes・同じ検査結果になる（in-process）。"""

    @classmethod
    def setUpClass(cls):
        cls.executed = _executed()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindings = record_stream_bindings()

    def reference(self, executed, name="reference"):
        return write_kyoku_records(
            self.tmp / name,
            [item.capture for item in executed],
            producer_revision=fx.ARENA_REVISION,
            event_facts_required=False,
            bindings=self.bindings,
            layout=LAYOUT,
            capture_type=HanchanCapture,
        )

    def assert_same_raw(self, left, right):
        self.assertEqual(
            sorted(p.name for p in left.iterdir()),
            sorted(p.name for p in right.iterdir()),
        )
        for path in left.iterdir():
            self.assertEqual(path.read_bytes(), (right / path.name).read_bytes())

    def test_completion_order_does_not_change_the_bytes(self):
        reference = self.reference(self.executed)
        orders = {
            "schedule": list(range(600)),
            "reversed-window": [i ^ 7 for i in range(600)],
            "shuffled": random.Random(578).sample(range(600), 600),
        }
        for name, order in orders.items():
            with self.subTest(name):
                streamed, _ = _stream_in_order(
                    self.tmp / name, self.executed, order, bindings=self.bindings
                )
                self.assert_same_raw(self.tmp / "reference", self.tmp / name)
                self.assertEqual(streamed, reference)

    def test_first_event_fact_failure_in_canonical_order_is_recorded(self):
        executed = _executed(evidence_missing={40, 7, 300})
        reference = self.reference(executed)
        self.assertFalse(reference.event_fact_status.valid)
        order = random.Random(1).sample(range(600), 600)
        streamed, _ = _stream_in_order(
            self.tmp / "streamed", executed, order, bindings=self.bindings
        )
        self.assert_same_raw(self.tmp / "reference", self.tmp / "streamed")
        self.assertEqual(streamed.event_fact_status, reference.event_fact_status)
        self.assertFalse((self.tmp / "streamed" / EVENT_FACT_FILENAME).exists())

    def test_event_fact_write_failure_marks_only_the_second_layer_invalid(self):
        original = record_store._PayloadFile.write

        def failing(self, line):
            if self.path.name == EVENT_FACT_FILENAME and self.rows == 250:
                raise OSError("injected event fact write failure")
            return original(self, line)

        with mock.patch.object(record_store._PayloadFile, "write", failing):
            streamed, _ = _stream_in_order(
                self.tmp / "streamed", self.executed, range(600), bindings=self.bindings
            )
        self.assertEqual(
            streamed.event_fact_status.reason,
            "event fact write failed: OSError: injected event fact write failure",
        )
        self.assertEqual(len(streamed.hanchans), 600)
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "streamed").iterdir()),
            [KYOKU_RECORD_FILENAME, MANIFEST_FILENAME],
        )

    def test_first_layer_failures_stop(self):
        broken = replace(self.executed[3].capture, policy_call_counts=(1, 2, 3))
        cases = {
            "projection": [
                *self.executed[:3],
                replace(self.executed[3], capture=broken),
            ],
            "duplicate": [*self.executed[:3], self.executed[2]],
        }
        for name, executed in cases.items():
            with self.subTest(name), self.assertRaises(KyokuRecordError):
                _stream_in_order(
                    self.tmp / name,
                    executed,
                    range(len(executed)),
                    bindings=self.bindings,
                )

    def test_a_schedule_index_cannot_arrive_twice(self):
        with self.assertRaisesRegex(RunStopped, "delivered twice"):
            _stream_in_order(
                self.tmp / "twice", self.executed[:3], [1, 1], bindings=self.bindings
            )

    def test_the_reorder_buffer_holds_projected_rows_only(self):
        _, sink = _stream_in_order(
            self.tmp / "streamed",
            self.executed,
            [1, 2, 3, 0, *range(4, 600)],
            bindings=self.bindings,
        )
        self.assertEqual(sink.max_buffered, 4)
        self.assertEqual(len(sink.measured), 600)


class StreamParallelTest(unittest.TestCase):
    def test_submission_stays_within_max_ahead_of_a_slow_head(self):
        """先頭が遅い間は index < 0 + max_ahead までしかsubmitしない。"""
        delivered = []
        stream_parallel_tasks(
            _tasks(range(6)),
            workers=2,
            timeout_seconds=30,
            expected_environment=fx.ENVIRONMENT,
            play=_slow_head_play,
            on_result=lambda index, item: delivered.append(index),
            max_ahead=2,
        )
        self.assertEqual(sorted(delivered), list(range(6)))
        head = delivered.index(0)
        self.assertEqual(delivered[:head], [1])

    def test_max_ahead_below_workers_is_rejected(self):
        with self.assertRaisesRegex(RunStopped, "max_ahead"):
            stream_parallel_tasks(
                _tasks(range(2)),
                workers=2,
                timeout_seconds=30,
                expected_environment=fx.ENVIRONMENT,
                play=fx.fake_play,
                on_result=lambda index, item: None,
                max_ahead=1,
            )


def _tasks(placements):
    from types import SimpleNamespace

    return [
        SimpleNamespace(seed=fx.SEEDS[0], placement=placement)
        for placement in placements
    ]


class FormalEventTest(unittest.TestCase):
    """spawn workerを使う正式runner（1 / 3 worker）の合成600半荘。"""

    def root(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def event(self, workers, play=fx.fake_play, *, root=None, **kwargs):
        root = self.root() if root is None else root
        ledger_path = root / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        lock = fx.lock_document(root, workers=workers)
        lock_path = save_lock(lock, root / "lock.json")
        result = runner.run_formal_event(
            lock_path,
            ledger_path=ledger_path,
            play=play,
            live_gate=fx.gate,
            os_limits=lambda: fx.os_limits(lock["execution"]["limits"]),
            parent_peak_rss_kib=fx.parent_peak_rss_kib,
            **kwargs,
        )
        return root, lock, result

    def assert_nothing_published(self, root):
        for name in ("raw", "observations.json", "result.json"):
            self.assertFalse((root / name).exists(), name)
        self.assertEqual([p.name for p in root.iterdir() if "staging" in p.name], [])
        self.assertEqual(len(list((root / "diagnostics").glob("stop-*.json"))), 1)

    def test_one_and_three_workers_match_the_full_retention_reference(self):
        """変更前の全件保持の保存処理と、raw・resultが同じbytesになる。"""
        reference_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, reference_root, True)
        executed = fx.executed_schedule()
        payloads = {}
        for workers in (1, 3):
            root, lock, result = self.event(workers)
            reference = write_kyoku_records(
                reference_root / f"raw-{workers}",
                [item.capture for item in executed],
                producer_revision=lock["execution_target"]["revision"],
                event_facts_required=False,
                bindings=record_bindings(lock),
                layout=LAYOUT,
                capture_type=HanchanCapture,
            )
            for name in RAW_FILES:
                self.assertEqual(
                    (root / "raw" / name).read_bytes(),
                    (reference_root / f"raw-{workers}" / name).read_bytes(),
                )
            # wall秒は観測値。それ以外の観測値は変更前の作り方と一致する
            observed = json.loads((root / "observations.json").read_text("utf-8"))
            expected = build_observations(lock, executed)
            for document in (observed, expected):
                for item in document["hanchans"]:
                    item["wall_seconds"] = 0
                document["summary"] = None
            self.assertEqual(observed, expected)
            # 同じ観測値fileに対し、変更前の手順でresultを作ると完全一致する
            self.assertEqual(
                result,
                build_result(
                    lock,
                    reference,
                    observations_digest=observations_sha256(root / "observations.json"),
                    aggregation_revision=lock["execution_target"]["revision"],
                ),
            )
            self.assertEqual(
                verify_result(
                    lock_path=root / "lock.json",
                    raw_path=root / "raw",
                    result_path=root / "result.json",
                    observations_path=root / "observations.json",
                ),
                result,
            )
            # manifestはlock digest（worker数を含む）を持つので、worker数の間ではpayloadを比べる
            payloads[workers] = {
                name: (root / "raw" / name).read_bytes()
                for name in (KYOKU_RECORD_FILENAME, EVENT_FACT_FILENAME)
            }
            payloads[workers]["statistic"] = result["statistic"]
            self.assertEqual(
                [p.name for p in root.iterdir() if "staging" in p.name], []
            )
        self.assertEqual(payloads[1], payloads[3])

    def test_the_parent_keeps_a_bounded_number_of_captures(self):
        """実行中に親で生きているcaptureは、worker数 + 小さな定数以内。"""
        workers = 3
        observed = []

        def count(done, total):
            if done % 50 == 0 or done == total:
                observed.append(_live_captures())

        sinks = []
        original = runner._CanonicalSink

        def keep_sink(stream):
            sinks.append(original(stream))
            return sinks[-1]

        baseline = _live_captures()
        with mock.patch.object(runner, "_CanonicalSink", side_effect=keep_sink):
            self.event(workers, on_hanchan=count)
        self.assertEqual(len(observed), 12)
        self.assertLessEqual(max(observed) - baseline, workers + 2)
        self.assertLessEqual(
            sinks[0].max_buffered, runner.REORDER_AHEAD_PER_WORKER * workers
        )
        self.assertEqual(len(sinks[0].measured), 600)

    def test_failures_never_publish_and_remove_the_staging(self):
        for play, pattern in (
            (fx.fake_play_policy_error, "synthetic Policy failure"),
            (fx.fake_play_wrong_placement, "different hanchan"),
            (fx.fake_play_other_environment, "environment differs"),
            (fx.fake_play_large_rss, "peak RSS"),
            (_crashing_play, "worker failed"),
        ):
            with self.subTest(play=play.__name__):
                root = self.root()
                with self.assertRaisesRegex(RunStopped, pattern):
                    self.event(3, play, root=root)
                self.assert_nothing_published(root)

    def test_raw_write_failure_stops_without_publishing(self):
        original = record_store._PayloadFile.write

        def failing(self, line):
            if self.path.name == KYOKU_RECORD_FILENAME and self.rows == 100:
                raise OSError("injected raw write failure")
            return original(self, line)

        root = self.root()
        with (
            mock.patch.object(record_store._PayloadFile, "write", failing),
            self.assertRaisesRegex(RunStopped, "injected raw write failure"),
        ):
            self.event(3, root=root)
        self.assert_nothing_published(root)

    def test_rename_failure_rolls_back_everything(self):
        original = Path.rename

        def failing(self, target):
            if Path(target).name == "result.json":
                raise OSError("injected rename failure")
            return original(self, target)

        for workers in (1, 3):
            with self.subTest(workers=workers):
                root = self.root()
                with (
                    mock.patch.object(Path, "rename", failing),
                    self.assertRaisesRegex(RunStopped, "injected rename failure"),
                ):
                    self.event(workers, root=root)
                self.assert_nothing_published(root)


if __name__ == "__main__":
    unittest.main()
