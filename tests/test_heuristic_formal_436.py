"""AWS event 436 routing and fail-closed admission; no live AWS or game calls."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import test_heuristic_candidate_423 as old
import test_heuristic_candidate_436 as current

from lisjong_arena.heuristic_candidate_aabb import formal436 as formal
from lisjong_arena.heuristic_candidate_aabb import rust436
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
)

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "scripts/aws"


class AdmissionTests(unittest.TestCase):
    def test_allocation_requires_event_revision_split_and_whole_population(self):
        seeds = tuple(range(500000, 500100))
        defaults = dict(
            owner_issue="lisbun/lisjong-arena#436",
            protocol=formal.PROTOCOL_ID,
            seed_domain=formal.SEED_DOMAIN,
            purpose="synthetic",
            population="heuristic-candidate-aabb-436",
            split="FORMAL-EVAL",
            seeds=seeds,
            arena_revision="a" * 40,
            protocol_revision=formal.PROTOCOL_ID,
            provenance_reference="fixture",
            allocation_timestamp="2026-10-01T00:00:00Z",
        )
        for overrides in (
            {},
            {"owner_issue": "lisbun/lisjong-arena#423"},
            {"population": "heuristic-candidate-aabb-423"},
            {"split": "DEVELOPMENT"},
            {"arena_revision": "b" * 40},
            {"seeds": seeds[:-1]},
        ):
            with self.subTest(overrides=overrides):
                ledger, record = reserve_allocation(
                    new_ledger(), **(defaults | overrides)
                )
                binding = allocation_binding(ledger, record["allocation_identity"])
                if overrides:
                    with self.assertRaises(ValueError):
                        formal.require_allocation(
                            ledger, binding, seeds, arena_revision="a" * 40
                        )
                else:
                    formal.require_allocation(
                        ledger, binding, seeds, arena_revision="a" * 40
                    )

    def test_runtime_plan_does_not_borrow_previous_candidate_timings(self):
        with mock.patch.object(
            formal.subprocess, "check_output", return_value=""
        ) as git:
            plan = formal.runtime_plan(ROOT, "a" * 40)
            self.assertFalse(plan["available"])
            self.assertFalse(plan["matching_calibration"])
            self.assertIsNone(plan["predicted_upper_seconds"])
            self.assertEqual(plan["workers"], 32)
            self.assertIn("pyproject.toml", git.call_args.args[0])
            git.return_value = "src/lisjong_arena/policy_catalog.py"
            with self.assertRaisesRegex(ValueError, "workload differs"):
                formal.runtime_plan(ROOT, "a" * 40)

    def test_saved_lock_rejects_other_event_and_worker_count(self):
        fixture = current.Event436Tests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        formal.require_aws_lock(fixture.build(max_workers=32))
        with self.assertRaisesRegex(ValueError, "32 workers"):
            formal.require_aws_lock(fixture.build(max_workers=8))
        # A fully valid historical lock must not be accepted as event 436.
        historical = old.Event423Tests()
        historical.setUp()
        self.addCleanup(historical.doCleanups)
        with self.assertRaisesRegex(ValueError, "event 436 lock"):
            formal.require_aws_lock(historical.build())


class BootstrapTests(unittest.TestCase):
    def test_event_selects_exact_candidate_and_isolates_namespace(self):
        script = (AWS / "bootstrap-heuristic-formal-423.sh").read_text()
        prefix = script[: script.index('if [[ ! "$ARENA_REVISION"')]
        for event, revision, identity in (
            (423, old.rust423.REVISION, old.rust423.PAIR[0][0]),
            (436, rust436.REVISION, rust436.PAIR[0][0]),
        ):
            with self.subTest(event=event):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        prefix
                        + '\nprintf "%s\\n" "$EVENT" "$FROZEN_LISJONG_REVISION" "$CANDIDATE_IDENTITY" "$WORK_ROOT"',
                        "fixture",
                        "--event",
                        str(event),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        str(event),
                        revision,
                        identity,
                        f"/mnt/lisjong-heuristic-formal-{event}",
                    ],
                )
        invalid = subprocess.run(
            ["bash", "-c", prefix, "fixture", "--event", "999"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(invalid.returncode, 2)

    def test_foreign_bucket_fails_before_bootstrap(self):
        script = (AWS / "bootstrap-heuristic-formal-423.sh").read_text()
        prefix = script[: script.index('if [[ "$(id -u)"')]
        result = subprocess.run(
            [
                "bash",
                "-c",
                "nproc() { echo 32; };\n" + prefix,
                "fixture",
                "--event",
                "436",
                "--arena-revision",
                "a" * 40,
                "--seeds",
                "500000:500099",
                "--allocation-binding-b64",
                "e30=",
                "--max-workers",
                "32",
                "--run-id",
                "fixture",
                "--transfer-bucket",
                "lisjong-423-fixture",
                "--region",
                "ap-northeast-1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("transfer bucket", result.stderr)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell is unavailable")
class PowerShellTests(unittest.TestCase):
    def run_harness(self, text, *arguments):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.ps1"
            path.write_text(text)
            return subprocess.run(
                [
                    shutil.which("pwsh"),
                    "-NoProfile",
                    "-File",
                    str(path),
                    str(AWS / "run-heuristic-formal-423.ps1"),
                    *arguments,
                ],
                capture_output=True,
                text=True,
            )

    def test_state_event_mismatch_stops_before_resource_access(self):
        harness = r"""
param($ScriptPath)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($ScriptPath,[ref]$null,[ref]$errors)
if (@($errors).Count) { throw ($errors | Out-String) }
$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$EvaluationEvent=436
Assert-RunEvent -State ([pscustomobject]@{issue='436'})
foreach ($state in @([pscustomobject]@{issue='423'},[pscustomobject]@{})) {
    $rejected=$false
    try { Assert-RunEvent -State $state } catch { $rejected=$true }
    if (-not $rejected) { throw 'Foreign state accepted' }
}
$EvaluationEvent=423
Assert-RunEvent -State ([pscustomobject]@{})
"""
        result = self.run_harness(harness)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
