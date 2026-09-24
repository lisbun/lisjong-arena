"""#367 diagnostic sweep driver (scripts/aws/sweep_l03_engine_367.py).

The driver is operational tooling for a diagnostic-only AWS throughput run. These
tests pin the frozen #366 population, one durable row per game, continuation
after a game failure, the summary contract, and that no scientific / target /
Seed Registry path is reachable. Hanchan execution is replaced by fakes.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
_AWS = _ROOT / "scripts" / "aws"
sys.path.insert(0, str(_AWS))
import sweep_l03_engine_367 as driver  # noqa: E402

_LAUNCHER = _AWS / "run-l03-engine-367.ps1"
_BOOTSTRAP = _AWS / "bootstrap-l03-engine-367.sh"


def _capture(survivors):
    return SimpleNamespace(selection=SimpleNamespace(survivors=survivors))


def _completed(rounds=9, reason="target_reached"):
    return SimpleNamespace(
        history=tuple(range(rounds)), end_reason=SimpleNamespace(value=reason)
    )


def _fake_play(seed, focal_seat_index):
    if seed % 7 == 3:
        raise ValueError(f"engine rejected seed {seed}")
    return _completed(), (_capture(None), _capture((1,)), _capture((0, 2)))


def _fake_task(ordinal, seed, seat):
    return driver.run_one(ordinal, seed, seat, play=_fake_play)


def _threads(max_workers):
    return ThreadPoolExecutor(max_workers=max_workers)


class PopulationTest(unittest.TestCase):
    def test_frozen_population_matches_366(self):
        population = driver.diagnostic_population()
        self.assertEqual(len(population), 400)
        self.assertEqual([p[0] for p in population], list(range(400)))
        self.assertEqual([p[1] for p in population], list(range(910000, 910400)))
        self.assertTrue(all(seat == ordinal % 4 for ordinal, _, seat in population))
        self.assertEqual(
            driver.FROZEN_REVISIONS,
            {
                "lisjong": "aed9c840bc120471e557fc0c8444965c0b81a9c3",
                "lisjong-engine": "96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b",
                "lisjong-arena": "668469910bf2e74de583c2b1ac00a77483e8b356",
            },
        )


class RunOneTest(unittest.TestCase):
    def test_pass_row_counts_focal_and_multi_survivor_decisions(self):
        row = driver.run_one(1, 910001, 1, play=_fake_play)
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["focal_seat"], "SOUTH")
        self.assertEqual(row["focal_decision_count"], 3)
        self.assertEqual(row["focal_multi_survivor_decision_count"], 1)
        self.assertEqual(row["completed_round_count"], 9)
        self.assertEqual(row["end_reason"], "target_reached")
        self.assertGreaterEqual(row["duration_seconds"], 0)
        self.assertLessEqual(row["started_at"], row["completed_at"])

    def test_game_failure_is_a_row_not_an_exception(self):
        row = driver.run_one(3, 910003, 3, play=_fake_play)
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["exception_class"], "builtins.ValueError")
        self.assertEqual(row["exception_message"], "engine rejected seed 910003")
        self.assertIn("ValueError", row["traceback"])
        self.assertNotIn("focal_decision_count", row)


class SweepTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "out"

    def _sweep(self, count=12, task=_fake_task, workers=3):
        return driver.sweep(
            self.output,
            workers=workers,
            population=driver.diagnostic_population(count),
            executor_factory=_threads,
            task=task,
            progress=False,
        )

    def test_failures_are_recorded_and_the_sweep_continues(self):
        summary = self._sweep()
        rows = driver.read_rows(self.output / driver.ROWS_FILENAME)
        self.assertEqual(sorted(r["game_ordinal"] for r in rows), list(range(12)))
        failed = sorted(r["game_ordinal"] for r in rows if r["status"] == "FAIL")
        self.assertEqual(failed, [3, 10])  # seeds 910003 / 910010: seed % 7 == 3
        self.assertEqual(summary["result"], driver.RESULT_FAILURES)
        self.assertEqual((summary["pass"], summary["fail"]), (10, 2))
        self.assertEqual(summary["failed_games"], [3, 10])
        self.assertEqual(
            summary["failures"],
            [
                {
                    "count": 1,
                    "exception_class": "builtins.ValueError",
                    "exception_message": f"engine rejected seed {seed}",
                }
                for seed in (910003, 910010)
            ],
        )
        self.assertEqual(summary["focal_decisions"], 30)
        self.assertEqual(summary["focal_multi_survivor_decisions"], 10)
        self.assertEqual(summary["workers"], 3)
        written = json.loads((self.output / driver.SUMMARY_FILENAME).read_text())
        self.assertEqual(written, summary)

    def test_interrupted_sweep_keeps_rows_already_completed(self):
        def task(ordinal, seed, seat):
            if ordinal == 3:
                raise KeyboardInterrupt  # not a game failure: aborts the sweep
            return driver.run_one(ordinal, seed, seat, play=_fake_play)

        with self.assertRaises(KeyboardInterrupt):
            self._sweep(count=6, task=task, workers=1)
        rows = driver.read_rows(self.output / driver.ROWS_FILENAME)
        self.assertEqual([r["game_ordinal"] for r in rows], [0, 1, 2])
        self.assertFalse((self.output / driver.SUMMARY_FILENAME).exists())

    def test_worker_crash_is_recorded_as_fail_row(self):
        def task(ordinal, seed, seat):
            if ordinal == 2:
                raise RuntimeError("worker process died")
            return driver.run_one(ordinal, seed, seat, play=_fake_play)

        summary = self._sweep(count=6, task=task)
        rows = {
            r["game_ordinal"]: r
            for r in driver.read_rows(self.output / driver.ROWS_FILENAME)
        }
        self.assertEqual(rows[2]["status"], "FAIL")
        self.assertEqual(rows[2]["seed"], 910002)
        self.assertEqual(rows[2]["exception_class"], "builtins.RuntimeError")
        self.assertEqual(summary["attempted"], 6)
        self.assertEqual(summary["failed_games"], [2, 3])

    def test_refuses_to_overwrite_existing_rows(self):
        self._sweep(count=2)
        with self.assertRaises(FileExistsError):
            self._sweep(count=2)

    def test_rejects_worker_count_out_of_range(self):
        for workers in (0, 33, 2.0):
            with self.assertRaises(driver.SweepError):
                self._sweep(count=2, workers=workers)


class SummarizeTest(unittest.TestCase):
    def _row(self, ordinal, *, status="PASS", start=0.0, duration=10.0, seed=None):
        begin = f"2026-09-24T00:00:{start:09.6f}+00:00"
        end = f"2026-09-24T00:00:{start + duration:09.6f}+00:00"
        row = {
            "completed_at": end,
            "duration_seconds": duration,
            "focal_seat": driver.SEATS[ordinal % 4],
            "focal_seat_index": ordinal % 4,
            "game_ordinal": ordinal,
            "seed": 910000 + ordinal if seed is None else seed,
            "started_at": begin,
            "status": status,
        }
        if status == "PASS":
            row.update(
                completed_round_count=8,
                end_reason="target_reached",
                focal_decision_count=200,
                focal_multi_survivor_decision_count=50,
            )
        else:
            row.update(exception_class="x.E", exception_message="m", traceback="t")
        return row

    def test_full_population_pass(self):
        rows = [self._row(i, start=float(i % 40)) for i in range(400)]
        summary = driver.summarize(rows, workers=16, workload_seconds=3600.0)
        self.assertEqual(summary["result"], driver.RESULT_PASS)
        self.assertEqual(
            summary["focal_seat_counts"],
            {"EAST": 100, "SOUTH": 100, "WEST": 100, "NORTH": 100},
        )
        self.assertEqual(summary["focal_decisions"], 80000)
        self.assertEqual(summary["throughput_hanchan_per_hour"], 400.0)
        self.assertEqual(summary["diagnostic_seeds"], [910000, 910399])
        self.assertIs(summary["scientific"], False)

    def test_missing_or_mismatched_rows_are_invalid(self):
        rows = [self._row(i) for i in range(399)]
        self.assertEqual(driver.summarize(rows)["result"], driver.RESULT_INVALID)
        rows = [self._row(i) for i in range(400)]
        rows[5] = self._row(5, seed=123)
        summary = driver.summarize(rows)
        self.assertEqual(summary["result"], driver.RESULT_INVALID)
        self.assertIn("game 5", summary["contract_errors"][0])
        rows = [self._row(i) for i in range(400)] + [self._row(0)]
        self.assertEqual(driver.summarize(rows)["result"], driver.RESULT_INVALID)

    def test_duration_percentiles_and_concurrency(self):
        population = driver.diagnostic_population(10)
        rows = [
            self._row(i, start=0.0 if i < 5 else 10.0, duration=float(i + 1))
            for i in range(10)
        ]
        summary = driver.summarize(rows, population=population)
        self.assertEqual(summary["game_duration_seconds"]["p50"], 5.0)
        self.assertEqual(summary["game_duration_seconds"]["p90"], 9.0)
        self.assertEqual(summary["concurrency"]["peak"], 5)
        self.assertGreater(summary["concurrency"]["mean"], 0)

    def test_summarize_cli_recomputes_from_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rows.jsonl"
            rows = [self._row(i) for i in range(400)]
            rows[7] = self._row(7, status="FAIL")
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            completed = subprocess.run(
                [sys.executable, str(_AWS / "sweep_l03_engine_367.py"), "summarize"]
                + ["--rows", str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["failed_games"], [7])


class BoundaryTest(unittest.TestCase):
    def test_driver_has_no_scientific_target_or_seed_registry_path(self):
        text = (_AWS / "sweep_l03_engine_367.py").read_text(encoding="utf-8")
        for forbidden in (
            "build_outcome_targets",
            "seed_registry",
            "generate_focal_outcome_source",
            "write_game",
            "build_manifest",
            "riichienv",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("run_policy_hanchan", text)
        self.assertIn("RuleSet.default()", text)

    def test_bootstrap_has_valid_bash_syntax_and_frozen_revisions(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(_BOOTSTRAP)], check=True)
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn(driver.FROZEN_REVISIONS["lisjong-arena"], text)
        self.assertIn(driver.FROZEN_REVISIONS["lisjong-engine"], text)
        self.assertIn("verify-environment", text)

    def test_launcher_defaults_and_bounds(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('[string]$InstanceType = "c7i.4xlarge"', text)
        self.assertIn("[ValidateRange(1, 32)][int]$MaxWorkers = 16", text)
        self.assertIn("[ValidateRange(1800, 7200)][int]$FailSafeSeconds = 7200", text)
        self.assertIn("[double]$CostBudgetUsd = 5.0", text)
        self.assertIsNone(re.search(r"(?i)spot", text))
        for revision in driver.FROZEN_REVISIONS.values():
            self.assertIn(revision, text)
        # Pricing, dry-run and fail-safe precede the workload submission.
        self.assertLess(
            text.index("Get-PricingDimension -Filters"), text.index('"create-bucket"')
        )
        self.assertLess(text.index('"--dry-run"'), text.index('"create-bucket"'))
        self.assertLess(
            text.index("LISJONG_367_FAILSAFE_ARMED"),
            text.index("$sweepSubmitted = $true"),
        )

    def test_launcher_parses_ec2_termination_gmt_as_utc(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[Globalization.DateTimeStyles]::AssumeUniversal", text)
        self.assertIn("[Globalization.DateTimeStyles]::AdjustToUniversal", text)
        self.assertNotIn(
            "[datetime]::Parse($Matches[1], $invariant).ToUniversalTime()",
            text,
        )

    def test_launcher_has_valid_powershell_syntax_when_pwsh_is_available(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        script = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{_LAUNCHER}',[ref]$null,[ref]$e); if (@($e).Count) {{ $e; exit 1 }}"
        )
        subprocess.run([pwsh, "-NoProfile", "-Command", script], check=True)


if __name__ == "__main__":
    unittest.main()