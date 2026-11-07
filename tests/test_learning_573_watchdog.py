"""Scope attribution and completion barrier regressions for the #573 operator."""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/aws/learning-573-watchdog.py"
spec = importlib.util.spec_from_file_location("learning_watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


ZERO_EVENTS = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
MIB = 1024 * 1024


class WatchdogTests(unittest.TestCase):
    def run_stage(
        self,
        directory,
        *,
        peak_error=False,
        soft=None,
        peak_bytes=134217728,
        events=ZERO_EVENTS,
        guard_output=(),
        scope_state=None,
    ):
        directory = Path(directory)
        start = directory / "start.json"
        start.write_text(json.dumps({"limit_seconds": 60}))
        scope = directory / "scope"
        scope.mkdir()
        peak = scope / "memory.peak"
        peak.write_text(str(peak_bytes))
        (scope / "memory.events").write_text(events)
        args = argparse.Namespace(
            log_dir=str(directory),
            start_file=str(start),
            stage="test",
            wall=30,
            runtime_max=30,
            memory_max="4G",
            disk_limit_bytes=10000000,
            disk_poll=1,
            watch=[str(directory)],
            watch_optional=[],
            command=["dummy"],
            memory_soft_peak_bytes=soft,
            guard_output=[str(path) for path in guard_output],
            unit_prefix="lp573",
        )
        with ExitStack() as stack:
            for name, value in (
                ("_cumulative", 0),
                ("_du", 0),
                ("_scope_state", scope_state or {}),
            ):
                stack.enter_context(patch.object(watchdog, name, return_value=value))
            resolver = stack.enter_context(
                patch.object(watchdog, "_scope_peak_path", return_value=peak)
            )
            if peak_error:
                resolver.side_effect = OSError("cannot read scope")
            process = stack.enter_context(
                patch.object(watchdog.subprocess, "Popen")
            ).return_value
            process.poll.side_effect = [None, 0]
            process.wait.return_value = 0
            stack.enter_context(patch.object(watchdog.subprocess, "run"))
            stack.enter_context(patch.object(watchdog.time, "sleep"))
            if not peak_error:
                # The workload finishes immediately after its wrapper is launched.
                def launch(*unused):
                    (directory / "test.command-done.json").write_text(
                        '{"returncode": 0}'
                    )
                    return process

                watchdog.subprocess.Popen.side_effect = launch
            code = watchdog.run(args)
        return code, json.loads((directory / "test.watchdog.json").read_text())

    def test_success_requires_final_scope_sample_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, record = self.run_stage(tmp)
            self.assertEqual(code, 0)
            self.assertTrue(record["final_peak_recorded"])
            self.assertTrue((Path(tmp) / "test.release").exists())
            self.assertEqual(record["memory_peak_bytes"], 134217728)

    def test_memory_guard_mode_passes_at_the_exact_soft_peak(self):
        """#580: memory.peak == soft is not a STOP; outputs stay in place."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            output.write_text("{}")
            code, record = self.run_stage(
                tmp, soft=128 * MIB, peak_bytes=128 * MIB, guard_output=[output]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(record["stopped_by"])
            self.assertEqual(record["memory_events_baseline"]["oom_kill"], 0)
            self.assertEqual(record["memory_max_events_delta"], 0)
            self.assertEqual(record["quarantined"], [])
            self.assertTrue(output.exists())

    def test_soft_peak_exceeded_stops_and_quarantines_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = [Path(tmp) / "raw", Path(tmp) / "result.json"]
            outputs[0].mkdir()
            outputs[1].write_text("{}")
            staging = Path(tmp) / ".raw.staging-abc"
            staging.mkdir()
            code, record = self.run_stage(
                tmp,
                soft=128 * MIB,
                peak_bytes=128 * MIB + 1,
                guard_output=[*outputs, Path(tmp) / "observations.json"],
            )
            self.assertEqual(code, 124)
            self.assertEqual(record["stopped_by"], "memory-soft")
            self.assertEqual(
                sorted(Path(item["from"]).name for item in record["quarantined"]),
                [".raw.staging-abc", "raw", "result.json"],
            )
            for path in (*outputs, staging):
                self.assertFalse(path.exists())
            self.assertEqual(len(list((Path(tmp) / "test.quarantine").iterdir())), 3)

    def test_oom_events_and_oom_kill_result_stop(self):
        for name, options in (
            ("oom at scope start", {"events": ZERO_EVENTS.replace("oom 0", "oom 1")}),
            ("oom-kill result", {"scope_state": {"Result": "oom-kill"}}),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                code, record = self.run_stage(tmp, soft=256 * MIB, **options)
                self.assertEqual(code, 124)
                self.assertEqual(record["stopped_by"], "memory-oom")

    def test_malformed_events_fail_closed(self):
        for events in ("oom 0\n", "oom x\n", ZERO_EVENTS + "oom 0\n"):
            with self.subTest(events=events), tempfile.TemporaryDirectory() as tmp:
                code, record = self.run_stage(tmp, soft=256 * MIB, events=events)
                self.assertEqual(code, 124)
                self.assertEqual(record["stopped_by"], "watchdog-error")

    def test_event_increase_and_decrease(self):
        baseline = {key: 0 for key in ("max", "oom", "oom_kill", "oom_group_kill")}
        self.assertFalse(watchdog._event_stop(baseline, {**baseline, "max": 5}))
        for key in watchdog.STOP_EVENTS:
            self.assertTrue(watchdog._event_stop(baseline, {**baseline, key: 1}))
        with self.assertRaisesRegex(RuntimeError, "decreased"):
            watchdog._event_stop({**baseline, "max": 2}, {**baseline, "max": 1})

    def test_failed_scope_read_stops_even_if_launcher_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, record = self.run_stage(tmp, peak_error=True)
            self.assertEqual(code, 124)
            self.assertEqual(record["stopped_by"], "watchdog-error")
            self.assertFalse(record["final_peak_recorded"])
            self.assertFalse((Path(tmp) / "test.release").exists())

    def test_exact_scope_and_no_launcher_pid_lookup(self):
        unit = "lp573-probe.scope"
        with patch.object(watchdog.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "/user.slice/" + unit + "\n"
            )
            path = watchdog._scope_peak_path(unit)
        self.assertEqual(path.name, "memory.peak")
        self.assertEqual(path.parent.name, unit)
        self.assertIn("ControlGroup", " ".join(run.call_args.args[0]))

    def test_startup_unavailable_is_distinct_from_wrong_scope(self):
        with patch.object(watchdog.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "\n")
            self.assertIsNone(watchdog._scope_peak_path("lp573-probe.scope"))
            for path in (
                "/system.slice/amazon-ssm-agent.service",
                "/user.slice/../lp573-probe.scope",
                "relative/lp573-probe.scope",
            ):
                run.return_value.stdout = path
                with self.assertRaises(RuntimeError):
                    watchdog._scope_peak_path("lp573-probe.scope")

    def test_unreadable_or_malformed_peak_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.peak"
            with self.assertRaises(OSError):
                watchdog._read_peak(path)
            for value in ("max", "-1", "", "12garbage"):
                path.write_text(value)
                with self.assertRaises(RuntimeError):
                    watchdog._read_peak(path)
            path.write_text("134217728\n")
            self.assertEqual(watchdog._read_peak(path), 134217728)

    def test_command_exit_keeps_scope_wrapper_alive_until_final_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, release = Path(tmp) / "done.json", Path(tmp) / "release"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "_exec",
                    str(done),
                    str(release),
                    sys.executable,
                    "-c",
                    "raise SystemExit(7)",
                ]
            )
            try:
                deadline = time.monotonic() + 15
                while not done.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(done.exists())
                self.assertEqual(json.loads(done.read_text()), {"returncode": 7})
                self.assertIsNone(
                    proc.poll(), "scope must survive the completed workload"
                )
                release.touch()
                self.assertEqual(proc.wait(timeout=10), 7)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
