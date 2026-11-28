"""#600 capture費用測定scriptのtest。

workerは差し替え（spawn）で、停止・削除・同一性の分岐を高速に固定する。実engineは
既知development seed 472005の1組（2半荘）だけで、記録・照合・削除の経路を通す。
"""

import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import measure_live_draw_capture_cost_600 as cost  # noqa: E402

# 監視threadがworker起動前から動くので、forkではなくspawnを使う
SPAWN = multiprocessing.get_context("spawn")
ONE_PAIR = [(0, 472_005, 0, (cost.C0, cost.C1))]


def _played(**overrides):
    value = {
        "cpu_seconds": 1.0,
        "wall_seconds": 1.0,
        "rss_before_kib": 1000,
        "rss_after_kib": 1000,
        "hwm_kib": 1000,
        "match_digest": "m",
        "selector_calls": [1, 1, 1, 1],
        "selection_digest": "s",
        "delivery_digest": "d",
        "live_draw_rows": None,
        "retained_hanchans": 1,
    }
    value.update(overrides)
    return value


_PHASE = {
    "hanchans": 1,
    "rows": 10,
    "serialized_bytes": 100,
    "serialized_gzip_bytes": 50,
    "stored_bytes": 200,
    "S": {},
    "W": {},
    "R": {},
}


def _fake(conn, config, *, mismatch=False, sleep=0.0, error=None):
    conn.send(("ready", {"environment": None, "pid": os.getpid()}))
    played = 0
    while True:
        message = conn.recv()
        if message[0] == "stop":
            return
        if message[0] == "play":
            if sleep:
                time.sleep(sleep)
            if error is not None:
                conn.send(("error", error))
                return
            played += 1
            c1 = config.condition == cost.C1
            conn.send(
                (
                    "ok",
                    _played(
                        cpu_seconds=1.2 if c1 else 1.0,
                        rss_after_kib=1000 + (100 * played if c1 else 10 * played),
                        match_digest="x" if (c1 and mismatch) else "m",
                        live_draw_rows=40 if c1 else None,
                        retained_hanchans=played,
                    ),
                )
            )
        else:
            Path(message[1], "f").write_bytes(b"0" * 10)
            conn.send(("ok", dict(_PHASE, hanchans=played or 1)))


def fake_ok(conn, config):
    _fake(conn, config)


def fake_mismatch(conn, config):
    _fake(conn, config, mismatch=True)


def fake_slow(conn, config):
    _fake(conn, config, sleep=5)


def fake_error(conn, config):
    _fake(conn, config, error="LiveDrawCaptureError")


def _run(target, temp_root, **kwargs):
    kwargs.setdefault("schedule", cost.pair_schedule((1, 2)))
    kwargs.setdefault("limits", dict(cost.LIMITS, hanchan_timeout_seconds=5))
    return cost.run_measurement(
        candidate={},
        incumbent={},
        wheel_path=None,
        arena_revision="a" * 40,
        engine_revision="b" * 40,
        target=target,
        context=SPAWN,
        temp_root=temp_root,
        **kwargs,
    )


class PlainProjectionTest(unittest.TestCase):
    def test_projection_is_typed_and_deterministic(self):
        class Color(Enum):
            RED = 1

        @dataclass(frozen=True)
        class Item:
            color: Color
            value: Fraction
            tags: frozenset

        item = Item(Color.RED, Fraction(1, 3), frozenset({"b", "a"}))
        self.assertEqual(cost.digest_of(item), cost.digest_of(item))
        self.assertEqual(
            cost.plain(item),
            [
                "dataclass",
                "Item",
                {
                    "color": ["enum", "Color", 1],
                    "tags": ["set", ["a", "b"]],
                    "value": ["fraction", 1, 3],
                },
            ],
        )
        self.assertNotEqual(cost.digest_of((1, 2)), cost.digest_of((2, 1)))

    def test_unknown_type_fails_closed(self):
        with self.assertRaises(TypeError):
            cost.plain(object())


class ScheduleTest(unittest.TestCase):
    def test_fixed_pairs_alternate_from_c0(self):
        schedule = cost.pair_schedule()
        self.assertEqual(len(schedule), 24)
        self.assertEqual(schedule[0], (0, 591_000, 0, (cost.C0, cost.C1)))
        self.assertEqual(schedule[1], (1, 591_000, 1, (cost.C1, cost.C0)))
        self.assertEqual(schedule[-1], (23, 591_003, 5, (cost.C1, cost.C0)))
        self.assertEqual([s[0] for s in schedule], list(range(24)))


class StopAndCleanupTest(unittest.TestCase):
    def setUp(self):
        # testの一時directoryはこのtest専用のrootの下にだけ作る（他の実行に触れない）
        holder = tempfile.TemporaryDirectory(prefix="arena-600-test-")
        self.addCleanup(holder.cleanup)
        self.root = holder.name

    def run_fake(self, target, **kwargs):
        return _run(target, self.root, **kwargs)

    def assert_cleaned(self, report):
        self.assertTrue(report["cleanup"]["removed"])
        self.assertFalse(report["cleanup"]["exists"])

    def test_completed_report_has_costs_and_no_digests(self):
        report = self.run_fake(fake_ok)
        self.assertEqual(report["status"], cost.STATUS_COMPLETED)
        self.assert_cleaned(report)
        self.assertEqual(report["temp_directory_mode"], "0o700")
        self.assertEqual(len(report["pairs"]), 12)
        self.assertNotIn("match_digest", report["pairs"][0][cost.C0])
        summary = report["summary"]
        self.assertAlmostEqual(
            summary["capture_overhead_cpu_seconds"]["total_ratio"], 1.2
        )
        self.assertEqual(summary["retained_rss_growth_kib_per_hanchan"][cost.C1], 100)
        self.assertEqual(
            summary["extrapolation"]["360"]["retained_rss_kib"],
            100 * 360 * cost.EXTRAPOLATION_SAFETY,
        )
        self.assertIsNotNone(report["probe"])
        self.assertIsNotNone(report["phase2"])
        self.assertEqual(summary["retained_memory"], "MEASURED")
        harness = report["harness"]
        for key in (
            "worker_startup_wall_seconds",
            "ipc_residual_wall_seconds",
            "probe_wall_seconds",
            "cleanup_wall_seconds",
            "monitor_thread_cpu_seconds",
            "parent_process_cpu_seconds",
        ):
            self.assertIn(key, harness)
        self.assertEqual(
            set(harness["worker_startup_wall_seconds"]), {cost.C0, cost.C1}
        )
        self.assertEqual(report["workers_stopped"], {cost.C0: True, cost.C1: True})
        self.assertEqual(list(Path(self.root).iterdir()), [])

    def test_identity_mismatch_stops_invalid(self):
        report = self.run_fake(fake_mismatch)
        self.assertEqual(report["status"], cost.STATUS_INVALID)
        self.assertIn("match_digest", report["stop_reason"])
        self.assertEqual(report["pairs"], [])
        self.assert_cleaned(report)

    def test_hanchan_timeout_stops(self):
        report = self.run_fake(
            fake_slow, limits=dict(cost.LIMITS, hanchan_timeout_seconds=1)
        )
        self.assertEqual(report["status"], cost.STATUS_LIMIT)
        self.assertIn("timed out", report["stop_reason"])
        self.assert_cleaned(report)

    def test_worker_error_reports_type_name_only(self):
        report = self.run_fake(fake_error)
        self.assertEqual(report["status"], cost.STATUS_FAILED)
        self.assertEqual(report["stop_reason"], "C0 worker raised LiveDrawCaptureError")
        self.assert_cleaned(report)

    def test_rss_limit_stops(self):
        report = self.run_fake(fake_slow, limits=dict(cost.LIMITS, rss_kib=1))
        self.assertEqual(report["status"], cost.STATUS_LIMIT)
        self.assertIn("VmRSS", report["stop_reason"])
        self.assert_cleaned(report)

    def test_monitoring_failure_stops(self):
        original = cost.proc_status_kib

        def broken(pid, field):
            if pid == "self":
                return original(pid, field)
            raise OSError("unreadable")

        with mock.patch.object(cost, "proc_status_kib", broken):
            report = self.run_fake(fake_slow)
        self.assertEqual(report["status"], cost.STATUS_FAILED)
        self.assertEqual(report["stop_reason"], "monitoring failed: OSError")
        self.assert_cleaned(report)

    def test_first_pair_projection_over_budget_stops(self):
        report = self.run_fake(
            fake_ok,
            limits=dict(cost.LIMITS, wall_seconds=60),
            projection_pairs=10**9,
        )
        self.assertEqual(report["status"], cost.STATUS_BUDGET)
        self.assertEqual(len(report["pairs"]), 1)
        self.assert_cleaned(report)

    def test_cleanup_failure_is_reported_with_its_location(self):
        def failing(directory):
            raise PermissionError("denied")

        report = self.run_fake(fake_ok, remover=failing)
        remaining = list(Path(self.root).iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(report["status"], cost.STATUS_FAILED)
        self.assertFalse(report["cleanup"]["removed"])
        self.assertEqual(report["cleanup"]["path"], str(remaining[0]))
        self.assertIn(str(remaining[0]), report["stop_reason"])
        self.assertIn("do not run again", report["stop_reason"])

    def test_cleanup_exception_is_a_failure_even_if_the_directory_is_gone(self):
        def removes_then_raises(directory):
            shutil.rmtree(directory)
            raise OSError("late error")

        report = self.run_fake(fake_ok, remover=removes_then_raises)
        self.assertFalse(report["cleanup"]["removed"])
        self.assertFalse(report["cleanup"]["exists"])
        self.assertEqual(report["status"], cost.STATUS_FAILED)

    def test_stop_after_the_last_response_is_not_completed(self):
        # 段階2の応答と同時に一時diskが上限を超えた状況。最後の同期sampleで止める
        original = cost.tree_bytes

        def large_once_phase2_exists(root):
            if (Path(root) / "phase2").exists():
                return 10**12
            return original(root)

        with mock.patch.object(cost, "tree_bytes", large_once_phase2_exists):
            report = self.run_fake(fake_ok)
        self.assertEqual(report["status"], cost.STATUS_LIMIT)
        self.assertIn("temporary disk", report["stop_reason"])
        self.assert_cleaned(report)

    def test_disk_scan_failure_stops(self):
        def unreadable(root):
            raise PermissionError("scan")

        with mock.patch.object(cost, "tree_bytes", unreadable):
            report = self.run_fake(fake_slow)
        self.assertEqual(report["status"], cost.STATUS_FAILED)
        self.assertEqual(report["stop_reason"], "monitoring failed: PermissionError")
        self.assert_cleaned(report)

    def test_worker_that_does_not_stop_is_a_failure(self):
        original = cost.Worker.close

        def reports_alive(worker):
            original(worker)  # 実際には止め、止まらなかった報告だけを模す
            return False

        with mock.patch.object(cost.Worker, "close", reports_alive):
            report = self.run_fake(fake_ok)
        self.assertEqual(report["status"], cost.STATUS_FAILED)
        self.assertIn("did not stop", report["stop_reason"])


class RequestRaceTest(unittest.TestCase):
    def test_response_after_a_trip_is_not_accepted(self):
        class TripsAfterReceive:
            calls = 0

            def check(self):
                self.calls += 1
                if self.calls >= 3:
                    raise cost.MeasurementStop(cost.STATUS_LIMIT, "tripped")

        parent, child = SPAWN.Pipe()
        child.send(("ok", {"wall_seconds": 0.0}))
        worker = cost.Worker.__new__(cost.Worker)
        worker.condition = cost.C1
        worker.conn = parent
        worker.process = mock.Mock(is_alive=mock.Mock(return_value=True))
        worker.ipc_wall_seconds = 0.0
        with self.assertRaises(cost.MeasurementStop) as stopped:
            worker.request(None, 5, TripsAfterReceive())
        self.assertEqual(stopped.exception.status, cost.STATUS_LIMIT)


class TreeBytesTest(unittest.TestCase):
    def test_missing_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(OSError):
                cost.tree_bytes(Path(root) / "missing")


class SummaryTest(unittest.TestCase):
    def report_with_growth(self, step):
        pairs = [
            {
                "index": i,
                cost.C0: dict(_played(), rss_after_kib=1000, live_draw_rows=None),
                cost.C1: dict(
                    _played(), rss_after_kib=1000 + step * i, live_draw_rows=40
                ),
            }
            for i in range(3)
        ]
        return {"pairs": pairs, "phase2": None}

    def test_negative_zero_positive_growth(self):
        for step, established in ((-5, False), (0, False), (7, True)):
            with self.subTest(step=step):
                summary = cost.summarize(self.report_with_growth(step))
                retained = summary["extrapolation"]["360"]["retained_rss_kib"]
                if established:
                    self.assertEqual(summary["retained_memory"], "MEASURED")
                    self.assertEqual(retained, 7 * 360 * cost.EXTRAPOLATION_SAFETY)
                else:
                    self.assertEqual(summary["retained_memory"], "NOT-ESTABLISHED")
                    self.assertIsNone(retained)


class RealEngineSmokeTest(unittest.TestCase):
    """既知development seed 472005の1組（C0 / C1を1半荘ずつ）。費用の値は検査しない。"""

    def test_one_pair_on_the_real_engine(self):
        from lisjong_arena.single_round_artifact import (
            collect_execution_provenance,
            execution_provenance_to_dict,
        )

        holder = tempfile.TemporaryDirectory(prefix="arena-600-test-")
        self.addCleanup(holder.cleanup)
        root = holder.name
        provenance = execution_provenance_to_dict(collect_execution_provenance())
        candidate, incumbent = cost.participants(provenance["lisjong_revision"])
        report = cost.run_measurement(
            candidate=candidate,
            incumbent=incumbent,
            wheel_path=None,
            arena_revision="0" * 40,
            engine_revision=provenance["lisjong_engine_revision"],
            schedule=ONE_PAIR,
            temp_root=root,
        )
        self.assertEqual(report["status"], cost.STATUS_COMPLETED, report["stop_reason"])
        self.assertTrue(report["cleanup"]["removed"])
        pair = report["pairs"][0]
        self.assertGreater(pair[cost.C1]["live_draw_rows"], 0)
        self.assertIsNone(pair[cost.C0]["live_draw_rows"])
        self.assertEqual(report["phase2"]["hanchans"], 1)
        self.assertEqual(report["phase2"]["rows"], pair[cost.C1]["live_draw_rows"])
        self.assertGreater(report["phase2"]["stored_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
