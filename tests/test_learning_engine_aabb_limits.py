"""#565 正式runの資源上限（OS上限の読取り・照合、全体wall、親 + workerのメモリ）。

cgroupとsystemctlは一時directoryと関数で差し替える。実scopeは使わない。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.learning_candidate_engine_aabb.limits import (
    EventBudget,
    parse_systemd_timespan,
    probe_os_limits,
    require_memory,
    require_os_limits,
)

LIMITS = {
    "peak_rss_kib": 4 * 1024 * 1024,
    "timeout_seconds": 300,
    "wall_seconds": 14_400,
}
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-u7.scope"


class TimespanTest(unittest.TestCase):
    def test_systemd_timespans(self):
        for text, seconds in (
            ("4h", 14_400.0),
            ("1h 40min", 6_000.0),
            ("2min 30s", 150.0),
            ("1d 2h", 93_600.0),
            ("1.500000s", 1.5),
            ("500ms\n", 0.5),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_systemd_timespan(text), seconds)

    def test_infinity_is_unlimited(self):
        self.assertIsNone(parse_systemd_timespan("infinity\n"))

    def test_unknown_text_fails_closed(self):
        for text in ("", "4 hours", "1M", "-1s", "4h junk"):
            with self.subTest(text=text):
                with self.assertRaises(RunStopped):
                    parse_systemd_timespan(text)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = self.tmp / "cgroup"
        self.proc.write_text(f"0::{SCOPE}\n", encoding="ascii")
        self.root = self.tmp / "sys"
        self.scope = self.root.joinpath(*SCOPE.split("/")[1:])
        self.scope.mkdir(parents=True)
        (self.scope / "memory.max").write_text("4294967296\n", encoding="ascii")
        (self.scope / "memory.swap.max").write_text("0\n", encoding="ascii")
        self.units = []

    def probe(self, runtime="4h\n"):
        def systemctl(unit):
            self.units.append(unit)
            return runtime

        return probe_os_limits(
            proc_cgroup=self.proc, cgroup_root=self.root, systemctl=systemctl
        )

    def test_reads_the_scope_of_this_process(self):
        observed = self.probe()
        self.assertEqual(
            observed,
            {
                "memory_max_bytes": 4 * 1024**3,
                "memory_swap_max_bytes": 0,
                "runtime_max_seconds": 14_400.0,
                "unit": "run-u7.scope",
            },
        )
        self.assertEqual(self.units, ["run-u7.scope"])
        require_os_limits(LIMITS, observed)

    def test_unlimited_values_are_none_and_rejected(self):
        (self.scope / "memory.max").write_text("max\n", encoding="ascii")
        (self.scope / "memory.swap.max").write_text("max\n", encoding="ascii")
        observed = self.probe(runtime="infinity\n")
        self.assertIsNone(observed["memory_max_bytes"])
        self.assertIsNone(observed["memory_swap_max_bytes"])
        self.assertIsNone(observed["runtime_max_seconds"])
        with self.assertRaisesRegex(RunStopped, "memory.max"):
            require_os_limits(LIMITS, observed)

    def test_unreadable_limits_fail_closed(self):
        cases = {
            "cgroup v1 only": lambda: self.proc.write_text(
                "4:memory:/user.slice\n", encoding="ascii"
            ),
            "no memory controller": lambda: (self.scope / "memory.max").unlink(),
            "no swap file": lambda: (self.scope / "memory.swap.max").unlink(),
            "garbage": lambda: (self.scope / "memory.max").write_text(
                "lots\n", encoding="ascii"
            ),
        }
        for name, damage in cases.items():
            with self.subTest(name=name):
                self.setUp()
                damage()
                with self.assertRaises(RunStopped):
                    self.probe()

    def test_systemctl_failure_fails_closed(self):
        def systemctl(unit):
            raise OSError("no user manager")

        with self.assertRaisesRegex(RunStopped, "cannot read the OS resource"):
            probe_os_limits(
                proc_cgroup=self.proc, cgroup_root=self.root, systemctl=systemctl
            )


class RequireTest(unittest.TestCase):
    def observed(self, **changes):
        document = {
            "memory_max_bytes": LIMITS["peak_rss_kib"] * 1024,
            "memory_swap_max_bytes": 0,
            "runtime_max_seconds": float(LIMITS["wall_seconds"]),
            "unit": "run-u7.scope",
        }
        document.update(changes)
        return document

    def test_os_limits_must_equal_the_lock(self):
        require_os_limits(LIMITS, self.observed())
        for changes, pattern in (
            ({"memory_max_bytes": LIMITS["peak_rss_kib"] * 1024 - 1}, "memory.max"),
            ({"memory_swap_max_bytes": 1}, "memory.swap.max"),
            ({"runtime_max_seconds": LIMITS["wall_seconds"] + 1.0}, "RuntimeMaxSec"),
            ({"runtime_max_seconds": None}, "RuntimeMaxSec"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RunStopped, pattern):
                    require_os_limits(LIMITS, self.observed(**changes))

    def test_memory_is_parent_plus_worker(self):
        limit = LIMITS["peak_rss_kib"]
        require_memory(LIMITS, limit - 10, 10, "stage")
        with self.assertRaisesRegex(RunStopped, r"parent .* \(stage\)"):
            require_memory(LIMITS, limit - 10, 11, "stage")
        for parent, worker in ((None, 1), (1, None)):
            with self.assertRaisesRegex(RunStopped, "cannot be measured"):
                require_memory(LIMITS, parent, worker, "stage")


class BudgetTest(unittest.TestCase):
    def test_larger_of_monotonic_and_realtime(self):
        now = {"clock": 0.0, "realtime": 1_000.0}
        budget = EventBudget(
            100,
            clock=lambda: now["clock"],
            realtime=lambda: now["realtime"],
        )
        now["clock"] = 30.0
        now["realtime"] = 1_090.0
        self.assertEqual(budget.remaining(), 10.0)
        budget.check("within")
        now["realtime"] = 1_101.0
        with self.assertRaisesRegex(RunStopped, r"wall time 101 s .*\(late\)"):
            budget.check("late")

    def test_counts_from_the_given_start(self):
        budget = EventBudget(
            100, clock=lambda: 150.0, realtime=lambda: 0.0, started=(40.0, 0.0)
        )
        with self.assertRaisesRegex(RunStopped, "wall time 110 s"):
            budget.check("start before the lock was read")


if __name__ == "__main__":
    unittest.main()
