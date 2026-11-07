"""#574: bounded parallel Learning formal executor (synthetic tasks only).

No real game, model, formal allocation or AWS dependency is used in these tests.
"""

import multiprocessing
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    RunStopped,
    run_tasks,
)
from lisjong_arena.learning_candidate_engine_aabb.limits import EventBudget
from lisjong_arena.learning_candidate_engine_aabb.parallel import (
    ParallelResourceGuard,
    _stop_pool,
    require_workers,
    run_parallel_tasks,
)


def _delayed_fake_play(task):
    if task.placement == 0:
        time.sleep(0.4)
    return fx.fake_play(task)


def _crashing_fake_play(task):
    if task.placement == 0:
        os._exit(17)
    return fx.fake_play(task)


def _overlap_fake_play(task):
    """A task must see the other worker start before either may complete."""
    from pathlib import Path

    marker = Path(task.marker_dir)
    (marker / f"started-{task.placement}").touch()
    deadline = time.monotonic() + 10.0
    while len(list(marker.glob("started-*"))) < 2:
        if time.monotonic() >= deadline:
            raise RuntimeError("two workers did not overlap")
        time.sleep(0.02)
    return fx.fake_play(task)


class ParallelTasksTest(unittest.TestCase):
    def test_shutdown_waits_for_os_exit_without_racing_manager_reaping(self):
        process = multiprocessing.get_context("spawn").Process(
            target=time.sleep, args=(60,)
        )
        process.start()
        try:
            pool = SimpleNamespace(
                _processes={process.pid: process}, kill_workers=process.kill
            )
            with (
                mock.patch.object(
                    process, "join", side_effect=AssertionError("manager owns reaping")
                ),
                mock.patch.object(
                    process, "is_alive", side_effect=AssertionError("racy waitpid")
                ),
            ):
                _stop_pool(pool)
            process.join(timeout=3)
            self.assertFalse(process.is_alive())
        finally:
            if process.is_alive():
                process.kill()
            process.join()

    def test_unconfirmed_shutdown_remains_fail_closed(self):
        pool = SimpleNamespace(
            _processes={1: SimpleNamespace(sentinel=42)}, kill_workers=mock.Mock()
        )
        with mock.patch(
            "lisjong_arena.learning_candidate_engine_aabb.parallel.wait_processes",
            return_value=[],
        ):
            with self.assertRaisesRegex(RunStopped, "did not exit"):
                _stop_pool(pool)
        pool.kill_workers.assert_called_once()

    def setUp(self):
        # Existing synthetic fixture identity, never a formal seed allocation.
        self.tasks = [
            SimpleNamespace(seed=fx.SEEDS[0], placement=placement)
            for placement in range(6)
        ]

    def parallel(self, play=fx.fake_play, **kwargs):
        return run_parallel_tasks(
            self.tasks,
            workers=3,
            timeout_seconds=20,
            expected_environment=fx.ENVIRONMENT,
            play=play,
            **kwargs,
        )

    def test_workers_are_bounded_in_the_lock(self):
        for workers in (1, 2, 4, 6, 8):
            self.assertEqual(require_workers(workers), workers)
        for workers in (0, 9, True, False, 1.0, "6"):
            with self.subTest(workers=workers), self.assertRaises(RunStopped):
                require_workers(workers)

    def test_parallel_returns_locked_schedule_order(self):
        observed = []
        result = self.parallel(
            _delayed_fake_play,
            on_hanchan=lambda done, total: observed.append((done, total)),
        )
        self.assertEqual(
            [(item.capture.seed, item.capture.placement) for item in result],
            [(task.seed, task.placement) for task in self.tasks],
        )
        self.assertEqual(observed, [(index, 6) for index in range(1, 7)])
        self.assertTrue(all(item.worker_pid for item in result))

    def test_two_workers_actually_overlap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as marker:
            tasks = [
                SimpleNamespace(
                    seed=fx.SEEDS[0],
                    placement=placement,
                    marker_dir=marker,
                )
                for placement in range(2)
            ]
            results = run_parallel_tasks(
                tasks,
                workers=2,
                timeout_seconds=30,
                expected_environment=fx.ENVIRONMENT,
                play=_overlap_fake_play,
            )
            self.assertEqual(len(results), 2)
            self.assertEqual(len({item.worker_pid for item in results}), 2)

    def test_formal_synthetic_parallel_bundle_and_verify(self):
        """K=100 synthetic schedule, no real Policy or new seed reservation."""
        import shutil
        import tempfile
        from pathlib import Path

        from lisjong_arena import seed_registry
        from lisjong_arena.learning_candidate_engine_aabb.lock import save_lock
        from lisjong_arena.learning_candidate_engine_aabb.result import verify_result
        from lisjong_arena.learning_candidate_engine_aabb.runner import (
            run_formal_event,
        )

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        ledger_path = root / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        lock = fx.lock_document(root, workers=3)
        lock_path = save_lock(lock, root / "lock.json")
        result = run_formal_event(
            lock_path,
            ledger_path=ledger_path,
            play=fx.fake_play,
            live_gate=fx.gate,
            os_limits=lambda: fx.os_limits(lock["execution"]["limits"]),
            parent_peak_rss_kib=fx.parent_peak_rss_kib,
        )
        verified = verify_result(
            lock_path=lock_path,
            raw_path=root / "raw",
            result_path=root / "result.json",
            observations_path=root / "observations.json",
        )
        self.assertEqual(result["result_digest"], verified["result_digest"])
        self.assertEqual(result["statistic"]["k"], 100)

    def test_formal_parallel_failure_never_publishes_partial_artifacts(self):
        import shutil
        import tempfile
        from pathlib import Path

        from lisjong_arena import seed_registry
        from lisjong_arena.learning_candidate_engine_aabb.lock import save_lock
        from lisjong_arena.learning_candidate_engine_aabb.runner import (
            run_formal_event,
        )

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        ledger_path = root / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        lock = fx.lock_document(root, workers=3)
        lock_path = save_lock(lock, root / "lock.json")
        with self.assertRaisesRegex(RunStopped, "synthetic Policy failure"):
            run_formal_event(
                lock_path,
                ledger_path=ledger_path,
                play=fx.fake_play_policy_error,
                live_gate=fx.gate,
                os_limits=lambda: fx.os_limits(lock["execution"]["limits"]),
                parent_peak_rss_kib=fx.parent_peak_rss_kib,
            )
        for name in ("raw", "observations.json", "result.json"):
            self.assertFalse((root / name).exists())
        self.assertEqual(len(list((root / "diagnostics").glob("stop-*.json"))), 1)

    def test_parallel_and_serial_payloads_are_equal(self):
        serial = run_tasks(
            self.tasks,
            key_fields=("placement",),
            timeout_seconds=20,
            expected_environment=fx.ENVIRONMENT,
            play=fx.fake_play,
        )
        parallel = self.parallel()
        self.assertEqual(
            [item.capture for item in parallel],
            [item.capture for item in serial],
        )

    def test_worker_environment_drift_stops(self):
        with self.assertRaisesRegex(RunStopped, "environment differs"):
            self.parallel(fx.fake_play_other_environment)

    def test_worker_crash_stops(self):
        with self.assertRaisesRegex(RunStopped, "worker failed"):
            self.parallel(_crashing_fake_play)

    def test_task_timeout_stops_all_workers(self):
        with self.assertRaisesRegex(RunStopped, "locked timeout"):
            run_parallel_tasks(
                self.tasks[:2],
                workers=2,
                timeout_seconds=2,
                expected_environment=fx.ENVIRONMENT,
                play=fx.fake_play_slow,
            )
        # Every process is joined/terminated; no leaked workers from this test.
        self.assertEqual(
            [p for p in multiprocessing.active_children() if p.is_alive()], []
        )

    def test_overall_deadline_stops(self):
        with self.assertRaisesRegex(RunStopped, "event wall time limit"):
            run_parallel_tasks(
                self.tasks[:2],
                workers=2,
                timeout_seconds=20,
                expected_environment=fx.ENVIRONMENT,
                play=fx.fake_play_slow,
                remaining_seconds=lambda: 0.0,
            )

    def test_cumulative_worker_memory_is_not_single_worker_max(self):
        guard = ParallelResourceGuard(
            {"peak_rss_kib": 100},
            EventBudget(3600),
            lambda: 50,
        )
        guard(ExecutedHanchan(None, 0.1, 30, worker_pid=1001))
        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            guard(ExecutedHanchan(None, 0.1, 30, worker_pid=1002))


if __name__ == "__main__":
    unittest.main()
