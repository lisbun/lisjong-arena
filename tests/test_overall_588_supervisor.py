"""#588 Overall event supervisor: staging, guards, quarantine and receipts.

Real processes run under the supervisor's completion barrier, but systemd and the
scope cgroup are replaced by a process group and a fake cgroup directory. The real
Overall CLI is replaced by a small fake that writes the four event outputs.
"""

import importlib.util
import json
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/overall-588/supervisor.py"
spec = importlib.util.spec_from_file_location("overall_588_supervisor", SCRIPT)
sup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sup)

MIB = 1024 * 1024
ZERO_EVENTS = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"

FAKE_CLI = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    from pathlib import Path

    mode = os.environ.get("FAKE_588_MODE", "")
    args = sys.argv[1:]
    command = args[0]

    def opt(name):
        return args[args.index(name) + 1]

    if command == "lock":
        names = {"--comparison-artifact": "comparison_artifact",
                 "--overall-result": "overall_result",
                 "--kyoku-records": "kyoku_records",
                 "--kyoku-summary": "kyoku_summary"}
        lock = {"lock_identity": "lock-x",
                "lock_version": 1 if mode == "lock-v1" else 2,
                "artifact_destinations": {v: opt(k) for k, v in names.items()}}
        Path(opt("--out")).write_text(json.dumps(lock))
    elif command == "run":
        lock = json.loads(Path(opt("--lock")).read_text())
        out = {k: Path(v) for k, v in lock["artifact_destinations"].items()}
        out["comparison_artifact"].write_text('{"c": 1}')
        if mode == "run-crash":
            sys.exit(1)
        if mode == "run-hang":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            Path(os.environ["FAKE_588_PID"]).write_text(str(child.pid))
            time.sleep(600)
        if mode == "run-big":
            out["comparison_artifact"].write_text("x" * 4096)
            time.sleep(2)
        out["kyoku_records"].write_text('{"rows": [1, 2, 3]}')
        out["kyoku_summary"].write_text('{"s": 1}')
        out["overall_result"].write_text('{"result_identity": "result-x"}')
        if mode == "run-extra":
            (out["overall_result"].parent / "stray.json").write_text("{}")
        if mode == "run-leftover":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            Path(os.environ["FAKE_588_PID"]).write_text(str(child.pid))
        if mode == "run-slow":
            time.sleep(1.5)
    elif command == "verify":
        if mode == "verify-fail":
            sys.exit(1)
        result = json.loads(Path(opt("--result")).read_text())
        if json.loads(Path(opt("--comparison")).read_text()) != {"c": 1}:
            sys.exit(1)
        rows = json.loads(Path(opt("--kyoku-records")).read_text())
        if rows != {"rows": [1, 2, 3]}:
            sys.exit(1)
        identity = "other" if mode == "verify-mismatch" else result["result_identity"]
        print(f"result_identity={identity}")
    """
)


def _plan(**overrides) -> dict:
    plan = {
        "schema": sup.PLAN_SCHEMA,
        "event_name": "t588",
        "label": "DEVELOPMENT",
        "entry": "development-reduced",
        "dev_seed_blocks": 2,
        "workers": 8,
        "lock_arguments": ["--seeds", "472005,472006"],
        "budgets": {
            "total_wall_seconds": 60,
            "stage_wall_seconds": {"lock": 20, "run": 20, "verify": 20},
            "memory_soft_peak_bytes": 512 * MIB,
            "memory_max_bytes": 1024 * MIB,
            "min_free_disk_bytes": 0,
            "max_staging_bytes": 1 << 30,
            "poll_seconds": 0.05,
            "max_cost_usd": None,
        },
        "seed_ledger": None,
        "approval_reference": None,
        "inject": None,
    }
    budgets = overrides.pop("budgets", {})
    plan.update(overrides)
    plan["budgets"] = {**plan["budgets"], **budgets}
    return plan


def _formal(**overrides) -> dict:
    budgets = {
        "total_wall_seconds": 18000,
        "stage_wall_seconds": {"lock": 600, "run": 14400, "verify": 1800},
        "memory_soft_peak_bytes": 6 * 1024 * MIB,
        "memory_max_bytes": 8 * 1024 * MIB,
        "max_cost_usd": 0,
        **overrides.pop("budgets", {}),
    }
    fields = {
        "label": "FORMAL",
        "entry": "formal",
        "dev_seed_blocks": None,
        "seed_ledger": "ledger-1",
        "approval_reference": "issue-comment-1",
        **overrides,
    }
    return _plan(budgets=budgets, **fields)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a dead parent may linger as a zombie until reaped.
    status = Path(f"/proc/{pid}/stat")
    return status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] != "Z"


@unittest.skipIf(sys.platform == "win32", "POSIX process groups and /proc")
class SupervisorEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.fake_cli = self.tmp / "fake_cli.py"
        self.fake_cli.write_text(FAKE_CLI)
        self.controllers = self.tmp / "cgroup.controllers"
        self.controllers.write_text("cpu memory pids\n")
        self.pid_file = self.tmp / "child.pid"
        self.groups: list[int] = []

    def _cleanup(self):
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.pid_file.exists():
            try:
                os.kill(int(self.pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError, ValueError:
                pass

    def event(
        self,
        plan,
        *,
        mode="",
        cgroup=None,
        state=None,
        free_bytes=None,
    ):
        """Prepare and run one event; return (exit code, receipt, event dir)."""
        plan_file = self.tmp / "plan-input.json"
        plan_file.write_text(json.dumps(plan))
        event = self.tmp / "event"
        self.assertEqual(
            sup.main(["prepare", "--event-dir", str(event), "--plan", str(plan_file)]),
            0,
        )
        dirs: dict[str, Path] = {}
        procs: dict[str, object] = {}
        cgroup = cgroup or {}
        state = state or {}

        def launch(unit, plan_, stage, argv, logs):
            done = logs / f"{stage}.done.json"
            release = logs / f"{stage}.release"
            command = [
                sys.executable,
                str(SCRIPT),
                "_exec",
                str(done),
                str(release),
                *argv,
            ]
            env = {
                **os.environ,
                "FAKE_588_MODE": mode,
                "FAKE_588_PID": str(self.pid_file),
            }
            with (
                (logs / f"{stage}.stdout").open("x") as out,
                (logs / f"{stage}.stderr").open("x") as err,
            ):
                process = sup.subprocess.Popen(
                    command, stdout=out, stderr=err, start_new_session=True, env=env
                )
            self.groups.append(process.pid)
            directory = self.tmp / f"cg-{stage}"
            directory.mkdir()
            files = {
                "memory.current": "1000",
                "memory.peak": "2000",
                "memory.max": str(plan_["budgets"]["memory_max_bytes"]),
                "memory.swap.max": "0",
                "memory.swap.current": "0",
                "memory.events": ZERO_EVENTS,
                "cgroup.procs": f"{process.pid}\n",
                **cgroup.get(stage, {}),
            }
            for name, text in files.items():
                if text is not None:
                    (directory / name).write_text(text)
            dirs[f"{unit}.scope"] = directory
            procs[f"{unit}.scope"] = process
            return command, process

        def scope_state(scope):
            stage = scope.rsplit("-", 1)[1].removesuffix(".scope")
            if stage in state:
                return dict(state[stage])
            process = procs.get(scope)
            if process is not None and _group_alive(process.pid):
                return {"ActiveState": "active", "Result": "success"}
            return {"ActiveState": "inactive", "Result": "success"}

        def stop_scope(scope, process):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return 0.0

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    sup,
                    "_entry_argv",
                    lambda plan_: [sys.executable, str(self.fake_cli)],
                )
            )
            stack.enter_context(patch.object(sup, "_launch", launch))
            stack.enter_context(
                patch.object(sup, "_cgroup_dir", lambda scope: dirs.get(scope))
            )
            stack.enter_context(patch.object(sup, "_scope_state", scope_state))
            stack.enter_context(patch.object(sup, "_stop_scope", stop_scope))
            stack.enter_context(patch.object(sup, "_reset_scope", lambda scope: None))
            stack.enter_context(
                patch.object(sup, "CGROUP_CONTROLLERS", self.controllers)
            )
            if free_bytes is not None:
                stack.enter_context(
                    patch.object(sup, "_free_bytes", lambda path: free_bytes)
                )
            code = sup.main(["run", "--event-dir", str(event)])
        receipt_path = event / "receipt.json"
        receipt = (
            json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        )
        return code, receipt, event

    def assert_not_published(self, receipt, event, status, reason):
        self.assertEqual(receipt["status"], status)
        self.assertEqual(receipt["trigger"]["reason"], reason)
        self.assertIsNone(receipt["published"])
        self.assertIsNone(receipt["result_identity"])
        self.assertFalse((event / "published").exists())
        self.assertFalse((event / "staging").exists())
        self.assertNotEqual(sup.check_event(event), 0)

    # ------------------------------------------------------------ success

    def test_success_publishes_after_fresh_verify_with_sums(self):
        code, receipt, event = self.event(_plan())
        self.assertEqual(code, sup.EXIT_PASS)
        self.assertEqual(receipt["status"], "PASS")
        self.assertIsNone(receipt["trigger"])
        self.assertEqual(receipt["result_identity"], "result-x")
        self.assertEqual(
            [stage["stage"] for stage in receipt["stages"]], list(sup.STAGES)
        )
        for stage in receipt["stages"]:
            self.assertIsNone(stage["reason"])
            self.assertTrue(stage["final_readback"])
            self.assertEqual(stage["memory_peak_bytes"], 2000)
            self.assertFalse(stage["leftover_processes"])
        published = event / "published"
        self.assertEqual(
            sorted(entry.name for entry in published.iterdir()),
            sorted([*sup.OUTPUTS.values(), sup.SUMS_NAME]),
        )
        self.assertFalse((event / "staging").exists())
        self.assertFalse((event / "quarantine").exists())
        self.assertEqual(receipt["lock"]["lock_version"], 2)
        self.assertEqual(sup.check_event(event), 0)
        # verify ran as its own process with the --no-report CLI flag
        self.assertIn("--no-report", receipt["stages"][2]["argv"])

    def test_receipt_is_one_shot(self):
        code, receipt, event = self.event(_plan())
        self.assertEqual(code, 0)
        with patch.object(sup, "CGROUP_CONTROLLERS", self.controllers):
            self.assertEqual(
                sup.main(["run", "--event-dir", str(event)]), sup.EXIT_REFUSED
            )
        self.assertEqual(json.loads((event / "receipt.json").read_text()), receipt)

    def test_check_detects_a_changed_published_file(self):
        code, _receipt, event = self.event(_plan())
        self.assertEqual(code, 0)
        target = event / "published" / "kyoku-summary.json"
        os.chmod(event / "published", 0o755)
        os.chmod(target, 0o644)
        target.write_text('{"s": 2}')
        self.assertEqual(sup.check_event(event), 1)

    # ------------------------------------------------------------ STOP

    def test_child_crash_quarantines_partial_output(self):
        code, receipt, event = self.event(_plan(), mode="run-crash")
        self.assertEqual(code, sup.EXIT_STOP)
        self.assert_not_published(receipt, event, "STOP", "child-exit")
        self.assertEqual(receipt["quarantine"]["contents"], ["comparison.json"])
        self.assertIsNone(receipt["quarantine_error"])

    def test_hung_stage_hits_wall_and_whole_tree_is_killed(self):
        plan = _plan(
            budgets={"stage_wall_seconds": {"lock": 20, "run": 2, "verify": 20}}
        )
        code, receipt, event = self.event(plan, mode="run-hang")
        self.assertEqual(code, sup.EXIT_STOP)
        self.assert_not_published(receipt, event, "STOP", "wall")
        run = receipt["stages"][1]
        self.assertGreaterEqual(run["elapsed_seconds"], 2)
        grandchild = int(self.pid_file.read_text())
        deadline = time.monotonic() + 5
        while _pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(grandchild))

    def test_total_wall_is_enforced_across_stages(self):
        plan = _plan(budgets={"total_wall_seconds": 1})
        code, receipt, event = self.event(plan, mode="run-slow")
        self.assertEqual(code, sup.EXIT_STOP)
        self.assertEqual(receipt["trigger"]["reason"], "total-wall")
        self.assertFalse((event / "published").exists())

    def test_memory_guards(self):
        cases = {
            "memory-soft": {"memory.peak": str(512 * MIB + 1)},
            "memory-oom": {
                "memory.events": ZERO_EVENTS.replace("oom_kill 0", "oom_kill 1")
            },
            "swap": {"memory.swap.current": "4096"},
        }
        for reason, files in cases.items():
            with self.subTest(reason):
                self.setUp()
                code, receipt, event = self.event(
                    _plan(), mode="run-slow", cgroup={"run": files}
                )
                self.assertEqual(code, sup.EXIT_STOP)
                self.assert_not_published(receipt, event, "STOP", reason)

    def test_kernel_oom_kill_result_is_memory_oom(self):
        code, receipt, event = self.event(
            _plan(),
            mode="run-crash",
            state={"run": {"ActiveState": "failed", "Result": "oom-kill"}},
        )
        self.assertEqual(code, sup.EXIT_STOP)
        self.assert_not_published(receipt, event, "STOP", "memory-oom")

    def test_unreadable_or_unlimited_cgroup_fails_closed(self):
        cases = {
            "monitor-unreadable": {"memory.current": None},
            "monitor-isolation": {"memory.max": "max"},
        }
        for reason, files in cases.items():
            with self.subTest(reason):
                self.setUp()
                code, receipt, event = self.event(
                    _plan(), mode="run-slow", cgroup={"run": files}
                )
                self.assertEqual(code, sup.EXIT_STOP)
                self.assert_not_published(receipt, event, "STOP", reason)
        with self.subTest("swap limit missing"):
            self.setUp()
            code, receipt, event = self.event(
                _plan(), mode="run-slow", cgroup={"run": {"memory.swap.max": "max"}}
            )
            self.assert_not_published(receipt, event, "STOP", "monitor-isolation")

    def test_disk_guards(self):
        with self.subTest("low"):
            plan = _plan(budgets={"min_free_disk_bytes": 1000})
            code, receipt, event = self.event(plan, mode="run-slow", free_bytes=999)
            # below the floor already before start: refused, nothing ran
            self.assertEqual(code, sup.EXIT_REFUSED)
            self.assertIsNone(receipt)
        with self.subTest("output"):
            self.setUp()
            plan = _plan(budgets={"max_staging_bytes": 1024})
            code, receipt, event = self.event(plan, mode="run-big")
            self.assertEqual(code, sup.EXIT_STOP)
            self.assert_not_published(receipt, event, "STOP", "disk-output")

    # ------------------------------------------------------------ INVALID

    def test_unexpected_output_set_is_invalid(self):
        code, receipt, event = self.event(_plan(), mode="run-extra")
        self.assertEqual(code, sup.EXIT_INVALID)
        self.assert_not_published(receipt, event, "INVALID", "output-set")
        self.assertIn("stray.json", receipt["quarantine"]["contents"])

    def test_lock_v1_or_foreign_destinations_are_invalid(self):
        code, receipt, event = self.event(_plan(), mode="lock-v1")
        self.assertEqual(code, sup.EXIT_INVALID)
        self.assert_not_published(receipt, event, "INVALID", "lock-mismatch")
        self.assertEqual(len(receipt["stages"]), 1)

    def test_verifier_failure_and_mismatch_are_invalid(self):
        for mode, reason in (
            ("verify-fail", "verify-failed"),
            ("verify-mismatch", "result-mismatch"),
        ):
            with self.subTest(mode):
                self.setUp()
                code, receipt, event = self.event(_plan(), mode=mode)
                self.assertEqual(code, sup.EXIT_INVALID)
                self.assert_not_published(receipt, event, "INVALID", reason)
                self.assertEqual(
                    sorted(receipt["quarantine"]["contents"]),
                    sorted(sup.OUTPUTS.values()),
                )

    def test_injected_kyoku_mutation_fails_verification(self):
        for inject in ("mutate-kyoku", "truncate-kyoku"):
            with self.subTest(inject):
                self.setUp()
                code, receipt, event = self.event(_plan(inject=inject))
                self.assertEqual(code, sup.EXIT_INVALID)
                self.assert_not_published(receipt, event, "INVALID", "verify-failed")
                self.assertEqual(
                    [item["kind"] for item in receipt["injected"]], [inject]
                )

    def test_leftover_process_is_killed_and_invalid(self):
        code, receipt, event = self.event(_plan(), mode="run-leftover")
        self.assertEqual(code, sup.EXIT_INVALID)
        self.assert_not_published(receipt, event, "INVALID", "cleanup-leftover")
        self.assertTrue(receipt["stages"][1]["leftover_processes"])
        self.assertFalse(_pid_alive(int(self.pid_file.read_text())))

    def test_quarantine_failure_is_invalid(self):
        code, receipt, event = self.event(_plan(inject="cleanup-fail"))
        self.assertEqual(code, sup.EXIT_INVALID)
        self.assertEqual(receipt["status"], "INVALID")
        self.assertEqual(receipt["trigger"]["reason"], "quarantine-error")
        self.assertIsNotNone(receipt["quarantine_error"])
        self.assertFalse((event / "published").exists())
        self.assertNotEqual(sup.check_event(event), 0)

    def test_publish_failure_after_rename_is_withdrawn(self):
        real_sums = sup._sums
        calls = []

        def sums(directory):
            calls.append(directory)
            return real_sums(directory) if len(calls) == 1 else "changed\n"

        with patch.object(sup, "_sums", sums):
            code, receipt, event = self.event(_plan())
        self.assertEqual(code, sup.EXIT_INVALID)
        self.assert_not_published(receipt, event, "INVALID", "publish-error")
        self.assertIn(sup.SUMS_NAME, receipt["quarantine"]["contents"])

    def test_injected_monitor_and_disk_faults(self):
        for inject, reason in (
            ("cgroup-unreadable", "monitor-unreadable"),
            ("disk-low", "disk-low"),
            ("disk-unreadable", "disk-unreadable"),
        ):
            with self.subTest(inject), patch.object(sup, "INJECT_AFTER_SECONDS", 0.0):
                self.setUp()
                plan = _plan(inject=inject, budgets={"min_free_disk_bytes": 1})
                code, receipt, event = self.event(plan, mode="run-slow")
                self.assertEqual(code, sup.EXIT_STOP)
                self.assert_not_published(receipt, event, "STOP", reason)


class PlanValidationTests(unittest.TestCase):
    def test_development_and_formal_plans_validate(self):
        sup.validate_plan(_plan())
        sup.validate_plan(_formal())

    def test_formal_plan_rules(self):
        cases = {
            "entry": {"entry": "development-reduced", "dev_seed_blocks": 2},
            "inject": {"inject": "hang"},
            "workers": {"workers": 4},
            "ledger": {"seed_ledger": None},
            "approval": {"approval_reference": None},
            "soft>=max": {"budgets": {"memory_soft_peak_bytes": 8 * 1024 * MIB}},
            "walls": {"budgets": {"total_wall_seconds": 100}},
            "cost": {"budgets": {"max_cost_usd": None}},
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(sup.PlanError):
                sup.validate_plan(_formal(**overrides))

    def test_reserved_lock_flags_and_shape(self):
        for arguments in (
            ["--seeds", "1", "--out", "x"],
            ["--seeds", "1", "--workers=4"],
            ["--seeds", "1", "--comparison-artifact", "x"],
            ["--workers", "8"],
        ):
            with self.subTest(arguments), self.assertRaises(sup.PlanError):
                sup.validate_plan(_plan(lock_arguments=arguments))
        for bad in (
            _plan(event_name="Bad Name"),
            _plan(dev_seed_blocks=9),
            _plan(budgets={"memory_max_bytes": 1024 * MIB + 1}),
            _plan(budgets={"poll_seconds": 0}),
            {**_plan(), "extra": 1},
        ):
            with self.assertRaises(sup.PlanError):
                sup.validate_plan(bad)

    def test_stage_commands(self):
        plan = _formal()
        paths = sup._paths(Path("/e"))
        lock = sup.stage_argv(plan, paths, "lock")
        self.assertEqual(
            lock[1:4], ["-m", "lisjong_arena.overall_champion_aabb", "lock"]
        )
        self.assertEqual(lock[lock.index("--out") + 1], "/e/lock.json")
        self.assertEqual(lock[lock.index("--workers") + 1], "8")
        self.assertEqual(
            lock[lock.index("--kyoku-summary") + 1], "/e/staging/kyoku-summary.json"
        )
        run = sup.stage_argv(plan, paths, "run")
        self.assertIn("--no-report", run)
        verify = sup.stage_argv(plan, paths, "verify")
        self.assertEqual(
            verify[verify.index("--comparison") + 1], "/e/staging/comparison.json"
        )
        dev = sup.stage_argv(_plan(), paths, "run")
        self.assertEqual(dev[1:4], [str(sup.DEV_ENTRY), "--seed-blocks", "2"])

    def test_cgroup_reader_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, text in {
                "memory.current": "1",
                "memory.peak": "2",
                "memory.max": "4096",
                "memory.swap.max": "0",
                "memory.swap.current": "0",
                "memory.events": ZERO_EVENTS,
                "cgroup.procs": "10\n11\n",
            }.items():
                (directory / name).write_text(text)
            sample = sup.read_cgroup(directory)
            self.assertEqual(sample["pids"], [10, 11])
            for name, text in (
                ("memory.peak", "max"),
                ("memory.events", "oom 0\n"),
                ("memory.events", ZERO_EVENTS + "oom 0\n"),
            ):
                original = (directory / name).read_text()
                (directory / name).write_text(text)
                with self.subTest(name=name, text=text), self.assertRaises(ValueError):
                    sup.read_cgroup(directory)
                (directory / name).write_text(original)


class CliReportTests(unittest.TestCase):
    def test_no_report_flags_exist(self):
        from lisjong_arena.overall_champion_aabb.__main__ import _parser

        parser = _parser()
        run = parser.parse_args(["run", "--lock", "x", "--no-report"])
        self.assertTrue(run.no_report)
        verify = parser.parse_args(
            [
                "verify",
                "--lock",
                "x",
                "--comparison",
                "c",
                "--result",
                "r",
                "--no-report",
            ]
        )
        self.assertTrue(verify.no_report)
        self.assertFalse(parser.parse_args(["run", "--lock", "x"]).no_report)


if __name__ == "__main__":
    unittest.main()
