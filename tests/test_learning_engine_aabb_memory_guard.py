"""#580 Learning正式評価のcgroup memory guard（lock v2）。

cgroup v2のfileは一時directory、systemctlは関数で差し替える。runnerはspawn 8 workerで
合成600半荘を実行し、scopeは ``FakeScope`` に差し替える。実engine・実model・正式seed・
AWSは使わない。実cgroupでの確認は ``scripts/aws/check-learning-cgroup-guard.py``。
"""

import copy
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    RunStopped,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    EVENT_FACT_FILENAME,
    KYOKU_RECORD_FILENAME,
    MANIFEST_FILENAME,
    HanchanCapture,
    write_kyoku_records,
)
from lisjong_arena.learning_candidate_engine_aabb import memory_guard as mg
from lisjong_arena.learning_candidate_engine_aabb import runner
from lisjong_arena.learning_candidate_engine_aabb.lock import (
    LOCK_SCHEMA_V1,
    LOCK_SCHEMA_V2,
    LearningEngineAabbLockError,
    parse_lock_document,
    save_lock,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import LAYOUT
from lisjong_arena.learning_candidate_engine_aabb.result import (
    record_bindings,
    verify_result,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

GIB = 1024**3
SOFT = 6 * GIB
HARD = 8 * GIB
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/lp580-run.scope"
UNIT = "lp580-run.scope"
ZERO_EVENTS = dict.fromkeys(mg.EVENT_KEYS, 0)
POST_RUN_STAGES = (
    "after the post-run live gate",
    "after the strict verify of the raw records",
    "after the pre-write checks",
    "before publishing",
    "after publishing",
)


def _redigest(lock: dict) -> dict:
    payload = {key: value for key, value in lock.items() if key != "lock_digest"}
    return {**payload, "lock_digest": document_identity(payload)}


class LockV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_v2_binds_the_memory_guard_into_the_digest(self):
        lock = fx.lock_document_v2(self.tmp)
        self.assertEqual(lock["schema"], LOCK_SCHEMA_V2)
        self.assertEqual(lock["execution"]["memory_guard"], mg.memory_guard_document())
        self.assertEqual(lock["execution"]["limits"], fx.LIMITS_V2)
        self.assertEqual(lock["execution"]["workers"], 8)
        self.assertEqual(
            mg.memory_guard_document(),
            {
                "hard_max_bytes": 8_589_934_592,
                "kind": "cgroup_peak_v1",
                "poll_seconds": 5,
                "rss_sum": "diagnostic_only",
                "scope_policy": "exact_systemd_scope_v1",
                "soft_peak_bytes": 6_442_450_944,
                "stop_comparison": "memory.peak > soft_peak_bytes",
                "stop_events": ["oom", "oom_group_kill", "oom_kill"],
                "swap_max_bytes": 0,
            },
        )
        self.assertEqual(parse_lock_document(lock), lock)

    def test_v1_is_unchanged(self):
        lock = fx.lock_document(self.tmp)
        self.assertEqual(lock["schema"], LOCK_SCHEMA_V1)
        self.assertNotIn("memory_guard", lock["execution"])
        self.assertEqual(lock["execution"]["limits"], fx.LIMITS)

    def test_v2_assembly_rejects_other_contracts(self):
        guard = mg.memory_guard_document()
        cases = {
            "workers 3": {"workers": 3},
            "workers 1": {"workers": 1},
            "v1 limits": {"limits": fx.LIMITS},
            "soft 4 GiB": {"memory_guard": {**guard, "soft_peak_bytes": 4 * GIB}},
            "soft as float": {"memory_guard": {**guard, "soft_peak_bytes": SOFT * 1.0}},
            "hard 4 GiB": {"memory_guard": {**guard, "hard_max_bytes": 4 * GIB}},
            "swap as bool": {"memory_guard": {**guard, "swap_max_bytes": False}},
            "other kind": {"memory_guard": {**guard, "kind": "rss_sum_v1"}},
            "rss stops": {"memory_guard": {**guard, "rss_sum": "stop"}},
            "no oom_group_kill": {
                "memory_guard": {**guard, "stop_events": ["oom", "oom_kill"]}
            },
            "extra field": {"memory_guard": {**guard, "max_events": "stop"}},
            "not an object": {"memory_guard": "cgroup_peak_v1"},
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(LearningEngineAabbLockError):
                fx.lock_document_v2(self.tmp, **overrides)

    def test_parse_rejects_tampered_v2_locks(self):
        lock = fx.lock_document_v2(self.tmp)
        damages = {
            "memory guard soft": lambda d: d["execution"]["memory_guard"].update(
                soft_peak_bytes=SOFT + 1
            ),
            "memory guard dropped": lambda d: d["execution"].pop("memory_guard"),
            "peak_rss_kib added": lambda d: d["execution"]["limits"].update(
                peak_rss_kib=4_194_304
            ),
            "workers": lambda d: d["execution"].update(workers=7),
            "v1 schema with a guard": lambda d: d.update(schema=LOCK_SCHEMA_V1),
            "unknown schema": lambda d: d.update(
                schema="arena-learning-candidate-engine-aabb-lock-v3"
            ),
        }
        for name, damage in damages.items():
            with self.subTest(name):
                document = copy.deepcopy(lock)
                damage(document)
                with self.assertRaises(LearningEngineAabbLockError):
                    parse_lock_document(_redigest(document))
        document = copy.deepcopy(lock)
        document["execution"]["memory_guard"]["poll_seconds"] = 1
        with self.assertRaisesRegex(LearningEngineAabbLockError, "memory_guard"):
            parse_lock_document(_redigest(document))
        document = copy.deepcopy(lock)
        document["execution"]["memory_guard"]["poll_seconds"] = 1
        with self.assertRaises(LearningEngineAabbLockError):
            parse_lock_document(document)  # digest no longer matches either


class _CgroupFiles:
    """一時directoryに作ったcgroup v2 scopeとsystemctlの応答。"""

    def __init__(self, root: Path, path: str = SCOPE):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.path = path
        self.proc = root / "proc-cgroup"
        self.proc.write_text(f"0::{path}\n", encoding="ascii")
        self.sys = root / "sys"
        self.directory = self.sys.joinpath(*path.split("/")[1:])
        self.directory.mkdir(parents=True)
        self.files = {
            "cgroup.controllers": "cpu memory pids",
            "memory.current": str(GIB),
            "memory.events": self.events_text(ZERO_EVENTS),
            "memory.max": str(HARD),
            "memory.peak": str(2 * GIB),
            "memory.stat": "anon 100\nfile 200\nkernel 300\nshmem 0\n",
            "memory.swap.max": "0",
        }
        for name, text in self.files.items():
            self.write(name, text)
        self.shown = {
            "ActiveState": "active",
            "ControlGroup": path,
            "MemoryMax": str(HARD),
            "MemorySwapMax": "0",
            "OOMPolicy": "kill",
            "RuntimeMaxUSec": "1h",
        }
        self.shown_units = []

    @staticmethod
    def events_text(events):
        return "".join(f"{key} {value}\n" for key, value in events.items())

    def write(self, name, text):
        (self.directory / name).write_bytes((text.rstrip("\n") + "\n").encode())

    def show(self, unit, properties):
        self.shown_units.append(unit)
        self.assertion = set(properties)
        return dict(self.shown)

    def scope(self):
        return mg.CgroupScope(
            proc_cgroup=self.proc, cgroup_root=self.sys, show=self.show
        )


class CgroupScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.files = _CgroupFiles(self.tmp)

    def test_resolves_the_exact_scope_and_samples_it(self):
        scope = self.files.scope()
        identity = scope.resolve(3_600.0)
        self.assertEqual((identity.path, identity.unit), (SCOPE, UNIT))
        self.assertEqual(self.files.shown_units, [UNIT])
        self.assertEqual(self.files.assertion, set(mg.SYSTEMD_PROPERTIES))
        sample = scope.sample(identity)
        self.assertEqual(sample["peak_bytes"], 2 * GIB)
        self.assertEqual(sample["current_bytes"], GIB)
        self.assertEqual(sample["events"], ZERO_EVENTS)
        self.assertEqual(sample["stat"], {"anon": 100, "file": 200, "kernel": 300})
        self.files.write("memory.stat", "anon 1\nfile 2")
        self.assertIsNone(scope.sample(identity)["stat"]["kernel"])

    def test_wrong_unit_or_os_limit_stops_before_running(self):
        damages = {
            "parent service": lambda f: f.proc.write_text(
                "0::/system.slice/amazon-ssm-agent.service\n", encoding="ascii"
            ),
            "cgroup v1 only": lambda f: f.proc.write_text(
                "4:memory:/user.slice\n", encoding="ascii"
            ),
            "two unified entries": lambda f: f.proc.write_text(
                f"0::{SCOPE}\n0::/other.scope\n", encoding="ascii"
            ),
            "relative path": lambda f: f.proc.write_text(
                "0::user.slice/lp580-run.scope\n", encoding="ascii"
            ),
            "dot-dot": lambda f: f.proc.write_text(
                "0::/user.slice/../lp580-run.scope\n", encoding="ascii"
            ),
            "spoofed ControlGroup": lambda f: f.shown.update(
                ControlGroup="/user.slice/other/lp580-run.scope"
            ),
            "inactive unit": lambda f: f.shown.update(ActiveState="inactive"),
            "systemd MemoryMax 4G": lambda f: f.shown.update(MemoryMax=str(4 * GIB)),
            "systemd MemoryMax infinity": lambda f: f.shown.update(
                MemoryMax="infinity"
            ),
            "systemd swap": lambda f: f.shown.update(MemorySwapMax="infinity"),
            "OOMPolicy continue": lambda f: f.shown.update(OOMPolicy="continue"),
            "RuntimeMaxSec": lambda f: f.shown.update(RuntimeMaxUSec="2h"),
            "RuntimeMaxSec infinity": lambda f: f.shown.update(
                RuntimeMaxUSec="infinity"
            ),
            "missing property": lambda f: f.shown.pop("MemoryMax"),
            "memory.max": lambda f: f.write("memory.max", str(HARD - 1)),
            "memory.max unlimited": lambda f: f.write("memory.max", "max"),
            "memory.swap.max": lambda f: f.write("memory.swap.max", "max"),
            "no memory controller": lambda f: f.write("cgroup.controllers", "cpu"),
            "no scope directory": lambda f: shutil.rmtree(f.directory),
        }
        for name, damage in damages.items():
            with self.subTest(name):
                files = _CgroupFiles(self.tmp / name.replace(" ", "-"))
                damage(files)
                with self.assertRaises(RunStopped):
                    files.scope().resolve(3_600.0)

    def test_systemctl_failure_stops(self):
        def show(unit, properties):
            raise OSError("no user manager")

        scope = mg.CgroupScope(
            proc_cgroup=self.files.proc, cgroup_root=self.files.sys, show=show
        )
        with self.assertRaisesRegex(RunStopped, "cannot verify the cgroup"):
            scope.resolve(3_600.0)

    def test_sample_failures_stop(self):
        def moved(f):
            f.proc.write_text("0::/user.slice/other.scope\n", encoding="ascii")

        def replaced(f):
            f.directory.rename(f.directory.with_name("old"))
            f.directory.mkdir()
            for name, text in f.files.items():
                f.write(name, text)

        damages = {
            "process moved": moved,
            "scope replaced": replaced,
            "scope removed": lambda f: shutil.rmtree(f.directory),
            "memory.max changed": lambda f: f.write("memory.max", "max"),
            "swap changed": lambda f: f.write("memory.swap.max", "1"),
            "peak missing": lambda f: (f.directory / "memory.peak").unlink(),
            "peak negative": lambda f: f.write("memory.peak", "-1"),
            "peak float": lambda f: f.write("memory.peak", "1.5"),
            "peak max": lambda f: f.write("memory.peak", "max"),
            "peak empty": lambda f: f.write("memory.peak", ""),
            "current garbage": lambda f: f.write("memory.current", "12x"),
            "events missing key": lambda f: f.write(
                "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0"
            ),
            "events duplicate": lambda f: f.write(
                "memory.events", f.events_text(ZERO_EVENTS) + "oom 0"
            ),
            "events malformed": lambda f: f.write(
                "memory.events", f.events_text(ZERO_EVENTS) + "oom_kill"
            ),
            "stat without anon": lambda f: f.write("memory.stat", "file 2"),
            "non-ascii digits": lambda f: f.write("memory.peak", "١"),
        }
        for name, damage in damages.items():
            with self.subTest(name):
                files = _CgroupFiles(self.tmp / name.replace(" ", "-"))
                scope = files.scope()
                identity = scope.resolve(3_600.0)
                damage(files)
                with self.assertRaises(RunStopped):
                    scope.sample(identity)


class FakeScope:
    """in-processのscope。testが ``peak`` / ``events`` を変える。"""

    def __init__(self, runtime=float(fx.LIMITS_V2["wall_seconds"])):
        self.runtime = runtime
        self.peak = 2 * GIB
        self.events = dict(ZERO_EVENTS)
        self.fail = None
        self.samples = 0

    def resolve(self, runtime_max_seconds):
        if runtime_max_seconds != self.runtime:
            raise RunStopped("RuntimeMaxSec differs")
        return mg.ScopeIdentity(SCOPE, UNIT, 7, 8)

    def sample(self, identity):
        self.samples += 1
        if self.fail is not None:
            raise RunStopped(self.fail)
        return {
            "current_bytes": GIB,
            "events": dict(self.events),
            "peak_bytes": self.peak,
            "stat": {"anon": 1, "file": 2, "kernel": 3},
            "time": "2026-11-08T00:00:00.000000Z",
        }


class _Budget:
    def __init__(self):
        self.stages = []

    def check(self, stage):
        self.stages.append(stage)


def _item(pid, kib):
    return ExecutedHanchan(None, 0.5, kib, None, {}, worker_pid=pid)


class GuardTest(unittest.TestCase):
    def guard(self, scope=None):
        self.scope = scope or FakeScope()
        self.budget = _Budget()
        guard = mg.CgroupMemoryGuard(
            mg.memory_guard_document(),
            fx.LIMITS_V2,
            self.budget,
            scope=self.scope,
            parent_peak_rss_kib=lambda: 700_000,
        )
        guard.start()
        return guard

    def test_soft_peak_boundary(self):
        guard = self.guard()
        self.scope.peak = SOFT
        guard.check("exactly 6 GiB")
        self.scope.peak = SOFT + 1
        with self.assertRaisesRegex(
            RunStopped,
            r"memory.peak 6442450945 bytes exceeded the locked soft limit "
            r"6442450944 bytes \(one byte over\)",
        ):
            guard.check("one byte over")
        document = guard.diagnostic(lock_digest="d" * 64, error=RunStopped("x"))
        self.assertEqual(document["last"]["peak_bytes"], SOFT + 1)
        self.assertEqual(document["max_peak_bytes"], SOFT + 1)
        self.assertEqual(document["status"], "STOP / INVALID")

    def test_a_peak_above_soft_at_start_stops(self):
        scope = FakeScope()
        scope.peak = SOFT + 1
        with self.assertRaisesRegex(RunStopped, r"\(start\)"):
            self.guard(scope)

    def test_peak_between_checks_is_seen_by_the_high_water_mark(self):
        """memory.peakはcheck間の一時的なピークを残すので、次のcheckでSTOPする。"""
        guard = self.guard()
        self.scope.peak = SOFT + 4096  # current has already fallen back
        with self.assertRaisesRegex(RunStopped, r"\(poll\)"):
            guard.poll()

    def test_oom_events_stop_and_max_is_a_warning(self):
        guard = self.guard()
        self.scope.events["max"] = 3
        self.scope.events["high"] = 9
        guard.check("max only")
        self.assertEqual(
            guard.diagnostic(lock_digest="d", error=None)["max_events_delta"], 3
        )
        for key in mg.STOP_EVENTS:
            with self.subTest(key):
                guard = self.guard()
                self.scope.events[key] = 1
                with self.assertRaisesRegex(RunStopped, f"{key} increased by 1"):
                    guard.check("event")

    def test_baseline_counts_are_not_new_events(self):
        scope = FakeScope()
        scope.events.update(oom=2, oom_kill=2, max=40)
        guard = self.guard(scope)
        guard.check("unchanged")
        scope.events["oom_kill"] = 3
        with self.assertRaisesRegex(RunStopped, "oom_kill increased by 1"):
            guard.check("new kill")

    def test_counter_or_peak_reset_stops(self):
        guard = self.guard()
        self.scope.events["max"] = 5
        guard.check("max")
        guard = self.guard()
        self.scope.peak = 3 * GIB
        guard.check("higher")
        self.scope.peak = 3 * GIB - 1
        with self.assertRaisesRegex(RunStopped, "memory.peak decreased"):
            guard.check("reset")
        scope = FakeScope()
        scope.events["max"] = 5
        guard = self.guard(scope)
        scope.events["max"] = 4
        with self.assertRaisesRegex(RunStopped, "max decreased"):
            guard.check("reset")

    def test_unreadable_scope_and_unstarted_guard_stop(self):
        guard = mg.CgroupMemoryGuard(
            mg.memory_guard_document(),
            fx.LIMITS_V2,
            _Budget(),
            scope=FakeScope(),
            parent_peak_rss_kib=lambda: None,
        )
        with self.assertRaisesRegex(RunStopped, "not started"):
            guard.check("early")
        guard = self.guard()
        self.scope.fail = "the cgroup scope lp580-run.scope was replaced"
        with self.assertRaisesRegex(RunStopped, "replaced"):
            guard.poll()

    def test_rss_sum_is_a_diagnostic_only(self):
        """旧方式なら4 GiBを大きく超えるRSS和でもSTOPしない。"""
        guard = self.guard()
        for pid in range(100, 108):
            guard(_item(pid, 3_000_000))
            guard(_item(pid, 2_000_000))
        guard.before_publish([])
        document = guard.diagnostic(lock_digest="d" * 64, error=None)
        self.assertEqual(document["rss_diagnostic"]["worker_peak_rss_kib_sum"], 24e6)
        self.assertEqual(document["rss_diagnostic"]["parent_peak_rss_kib"], 700_000)
        self.assertEqual(len(document["rss_diagnostic"]["worker_peak_rss_kib"]), 8)
        self.assertEqual(document["sample_counts"], {"after a hanchan": 16, "poll": 0})
        self.assertEqual(
            [item["stage"] for item in document["checkpoints"]],
            ["start", "before publishing"],
        )
        self.assertEqual(
            self.budget.stages, ["after a hanchan"] * 16 + ["before publishing"]
        )
        self.assertEqual(document["status"], "PASS")
        self.assertTrue(document["not_a_result"])
        self.assertEqual(document["scope"]["unit"], UNIT)
        with self.assertRaisesRegex(RunStopped, "peak RSS unavailable"):
            guard(_item(None, 1))


class FormalEventV2Test(unittest.TestCase):
    """spawn 8 workerの正式runner（lock v2）と合成600半荘。"""

    def root(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def event(self, play=fx.fake_play, *, root=None, scope=None, **kwargs):
        root = self.root() if root is None else root
        ledger_path = root / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        lock = fx.lock_document_v2(root, **kwargs.pop("lock_overrides", {}))
        lock_path = save_lock(lock, root / "lock.json")
        self.scope = scope or FakeScope()

        def no_v1_probe():
            raise AssertionError("a v2 lock must not use the v1 OS probe")

        kwargs.setdefault("live_gate", fx.gate)
        result = runner.run_formal_event(
            lock_path,
            ledger_path=ledger_path,
            play=play,
            os_limits=no_v1_probe,
            parent_peak_rss_kib=fx.parent_peak_rss_kib,
            memory_scope=lambda: self.scope,
            **kwargs,
        )
        return root, lock, result

    def memory_documents(self, root):
        return [
            json.loads(path.read_text("utf-8"))
            for path in sorted((root / "diagnostics").glob("memory-*.json"))
        ]

    def assert_stopped(self, root, pattern):
        for name in ("raw", "observations.json", "result.json"):
            self.assertFalse((root / name).exists(), name)
        self.assertEqual([p.name for p in root.iterdir() if "staging" in p.name], [])
        stops = list((root / "diagnostics").glob("stop-*.json"))
        self.assertEqual(len(stops), 1)
        self.assertRegex(json.loads(stops[0].read_text("utf-8"))["error"], pattern)
        memory = self.memory_documents(root)
        self.assertEqual(memory[-1]["status"], "STOP / INVALID")
        self.assertRegex(memory[-1]["error"], pattern)
        return memory[-1]

    def test_eight_workers_publish_the_same_records_and_a_memory_diagnostic(self):
        polls = []
        original = mg.CgroupMemoryGuard.poll

        def counted(guard):
            polls.append(1)
            return original(guard)

        with mock.patch.object(mg.CgroupMemoryGuard, "poll", counted):
            root, lock, result = self.event()
        reference = self.root() / "raw"
        write_kyoku_records(
            reference,
            [item.capture for item in fx.executed_schedule()],
            producer_revision=lock["execution_target"]["revision"],
            event_facts_required=False,
            bindings=record_bindings(lock),
            layout=LAYOUT,
            capture_type=HanchanCapture,
        )
        for name in (KYOKU_RECORD_FILENAME, EVENT_FACT_FILENAME, MANIFEST_FILENAME):
            self.assertEqual(
                (root / "raw" / name).read_bytes(), (reference / name).read_bytes()
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
        self.assertEqual([p.name for p in root.iterdir() if "staging" in p.name], [])
        self.assertFalse(list((root / "diagnostics").glob("stop-*.json")))
        [memory] = self.memory_documents(root)
        self.assertEqual(memory["schema"], mg.MEMORY_SCHEMA)
        self.assertEqual(memory["status"], "PASS")
        self.assertEqual(memory["lock_digest"], lock["lock_digest"])
        self.assertEqual(memory["memory_guard"], mg.memory_guard_document())
        self.assertEqual(memory["sample_counts"]["after a hanchan"], 600)
        self.assertEqual(memory["sample_counts"]["poll"], len(polls))
        self.assertGreater(len(polls), 0)
        self.assertEqual(
            [item["stage"] for item in memory["checkpoints"]],
            [
                "start",
                "after the pre-run live gate",
                *POST_RUN_STAGES,
            ],
        )
        self.assertGreater(memory["rss_diagnostic"]["worker_peak_rss_kib_sum"], 0)
        # 監視値は科学的な保存物に入らない
        for path in (root / "result.json", root / "observations.json"):
            self.assertNotIn("memory.peak", path.read_text("utf-8"))
            self.assertNotIn("memory_guard", path.read_text("utf-8"))

    def test_a_soft_stop_at_any_checkpoint_publishes_nothing(self):
        original = mg.CgroupMemoryGuard.check
        for stage in ("poll", "after a hanchan", *POST_RUN_STAGES):
            with self.subTest(stage):

                def check(guard, current, stage=stage):
                    if current == stage:
                        self.scope.peak = SOFT + 1
                    return original(guard, current)

                root = self.root()
                with (
                    mock.patch.object(mg.CgroupMemoryGuard, "check", check),
                    self.assertRaisesRegex(RunStopped, "soft limit"),
                ):
                    self.event(root=root)
                memory = self.assert_stopped(root, rf"soft limit .*\({stage}\)")
                self.assertEqual(memory["last"]["peak_bytes"], SOFT + 1)

    def test_oom_event_and_lost_scope_stop_the_run(self):
        original = mg.CgroupMemoryGuard.check
        for name, damage, pattern in (
            ("oom_kill", lambda s: s.events.update(oom_kill=1), "oom_kill increased"),
            ("scope lost", lambda s: setattr(s, "fail", "scope vanished"), "vanished"),
        ):
            with self.subTest(name):
                calls = {"n": 0}

                def check(guard, current, damage=damage):
                    calls["n"] += 1
                    if calls["n"] == 50:
                        damage(self.scope)
                    return original(guard, current)

                root = self.root()
                with (
                    mock.patch.object(mg.CgroupMemoryGuard, "check", check),
                    self.assertRaisesRegex(RunStopped, pattern),
                ):
                    self.event(root=root)
                self.assert_stopped(root, pattern)

    def test_a_wrong_scope_stops_before_the_live_gate(self):
        root = self.root()
        gates = []
        with self.assertRaisesRegex(RunStopped, "RuntimeMaxSec"):
            self.event(root=root, scope=FakeScope(runtime=1.0), live_gate=gates.append)
        self.assertEqual(gates, [])
        memory = self.assert_stopped(root, "RuntimeMaxSec")
        self.assertIsNone(memory["scope"])
        self.assertIsNone(memory["baseline"])

    @unittest.skipIf(sys.platform == "win32", "SIGTERM to self ends the process")
    def test_sigterm_stops_and_cleans_up(self):
        root = self.root()
        previous = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(2.0, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaisesRegex(RunStopped, "SIGTERM"):
            self.event(fx.fake_play_slow, root=root)
        self.assert_stopped(root, "SIGTERM")
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_v1_locks_keep_the_rss_sum_guard(self):
        """v1 lockはcgroup guardを使わない（従来の意味のまま）。"""
        root = self.root()
        ledger_path = root / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        lock = fx.lock_document(root, workers=3)
        lock_path = save_lock(lock, root / "lock.json")

        def no_scope():
            raise AssertionError("a v1 lock must not read the cgroup scope")

        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            runner.run_formal_event(
                lock_path,
                ledger_path=ledger_path,
                play=fx.fake_play_large_rss,
                live_gate=fx.gate,
                os_limits=lambda: fx.os_limits(lock["execution"]["limits"]),
                parent_peak_rss_kib=fx.parent_peak_rss_kib,
                memory_scope=no_scope,
            )
        self.assertFalse(list((root / "diagnostics").glob("memory-*.json")))


if __name__ == "__main__":
    unittest.main()
