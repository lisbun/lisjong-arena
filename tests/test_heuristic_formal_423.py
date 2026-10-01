"""Formal AWS admission and collection failures, without any live AWS calls."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lisjong_arena.heuristic_candidate_aabb import formal423 as formal
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
)

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "scripts/aws"


class AdmissionTests(unittest.TestCase):
    def test_formal_allocation_rejects_wrong_revision_and_calibration(self):
        seeds = tuple(range(500000, 500100))
        for population, split in (
            ("heuristic-candidate-aabb-423", "FORMAL-EVAL"),
            ("heuristic-candidate-423-calibration", "DEVELOPMENT"),
        ):
            ledger, record = reserve_allocation(
                new_ledger(),
                owner_issue="lisbun/lisjong-arena#423",
                protocol=formal.PROTOCOL_ID,
                seed_domain=formal.SEED_DOMAIN,
                purpose="fixture",
                population=population,
                split=split,
                seeds=seeds,
                arena_revision="a" * 40,
                protocol_revision=formal.PROTOCOL_ID,
                provenance_reference="fixture",
                allocation_timestamp="2026-09-29T00:00:00Z",
            )
            binding = allocation_binding(ledger, record["allocation_identity"])
            if split == "FORMAL-EVAL":
                formal.require_allocation(
                    ledger, binding, seeds, arena_revision="a" * 40
                )
            else:
                with self.assertRaises(ValueError):
                    formal.require_allocation(
                        ledger, binding, seeds, arena_revision="a" * 40
                    )
            with self.assertRaises(ValueError):
                formal.require_allocation(
                    ledger, binding, seeds, arena_revision="b" * 40
                )

    def test_prediction_rejects_unrelated_evidence_and_changed_runtime(self):
        result = {
            "identity": formal.CALIBRATION_ID,
            "operational_calibration": {"arena_revision": "calibration-revision"},
            "runtime_prediction": {
                "basis": "fixture",
                "headroom_adjusted_upper_seconds": 8892.0,
                "predicted_lower_seconds": 5710.0299,
                "predicted_upper_seconds": 5928.0,
            },
            "limitations": ["fixture"],
        }
        with (
            TemporaryDirectory() as tmp,
            mock.patch.object(formal, "verify", return_value=result),
            mock.patch.object(
                formal.subprocess, "check_output", return_value=""
            ) as git,
        ):
            self.assertIsNone(
                formal.calibration_plan(tmp, ROOT, "b" * 40)["workload_seconds"]
            )
            self.assertIn("pyproject.toml", git.call_args.args[0])
            git.return_value = "src/lisjong_arena/runner.py"
            with self.assertRaisesRegex(ValueError, "workload differs"):
                formal.calibration_plan(tmp, ROOT, "b" * 40)
            result["identity"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "unexpected calibration"):
                formal.calibration_plan(tmp, ROOT, "b" * 40)


class AwsTests(unittest.TestCase):
    def test_bootstrap_syntax_and_fixed_formal_entry(self):
        path = AWS / "bootstrap-heuristic-formal-423.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        script = path.read_text()
        self.assertIn('--event "$EVENT"', script)
        self.assertIn('EVENT="423"', script)
        self.assertIn('formal"+sys.argv[5]', script)
        self.assertNotIn("calibration423 run", script)
        self.assertLess(
            script.index("heuristic_candidate_aabb lock"),
            script.index("heuristic_candidate_aabb run"),
        )
        self.assertLess(
            script.index("heuristic_candidate_aabb verify"), script.index("COMPLETION=")
        )

    def test_preflight_gates_precede_billable_actions(self):
        script = (AWS / "run-heuristic-formal-423.ps1").read_text()
        launch = script.index('"create-bucket"')
        for gate in (
            "verify_wheel_file",
            "formal423 import require_allocation",
            "calibration_plan",
            "merge-base --is-ancestor",
            '"--dry-run"',
            "exposure_plus_margin_usd -gt",
        ):
            self.assertLess(script.index(gate), launch)
        self.assertIn("[ValidateSet(32)][int]$MaxWorkers = 32", script)
        self.assertIn("[ValidateSet(3600)][int]$FailSafeSeconds = 3600", script)
        self.assertIn("$SeedBlockCount = 100", script)

    def test_bootstrap_failure_uploads_diagnostics_and_preserves_exit(self):
        text = (AWS / "bootstrap-heuristic-formal-423.sh").read_text()
        trap = text[text.index("on_exit() {") : text.index("REPO_DIR=")]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "lock-stdout.txt",
                "run-stdout.txt",
                "verify-stdout.txt",
                "bootstrap.log",
            ):
                (root / name).write_text("fixture failure\n")
            script = "\n".join(
                [
                    "set -euo pipefail",
                    'OUTPUT_DIR="$1"',
                    'BOOTSTRAP_LOG="$OUTPUT_DIR/bootstrap.log"',
                    'UPLOADER_PID=""',
                    "EVENT=423",
                    'upload() { echo "uploaded:$1"; }',
                    trap,
                    "exit 7",
                ]
            )
            result = subprocess.run(
                ["bash", "-c", script, "test", tmp], capture_output=True, text=True
            )
        self.assertEqual(result.returncode, 7)
        for name in (
            "lock-stdout.txt",
            "run-stdout.txt",
            "verify-stdout.txt",
            "bootstrap.log",
        ):
            self.assertIn("uploaded:" + name, result.stdout)

    def test_collection_persists_failure_and_handles_absent_instance(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell is unavailable")
        path = AWS / "run-heuristic-formal-423.ps1"
        for event, verify_ok in ((423, True), (423, False), (436, True), (436, False)):
            with (
                self.subTest(event=event, verify_ok=verify_ok),
                TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                evidence = root / "evidence"
                evidence.mkdir()
                import hashlib

                digest = hashlib.sha256(b"{}").hexdigest()
                for name in (
                    "candidate-lock.json",
                    "comparison.json",
                    "candidate-result.json",
                ):
                    (evidence / name).write_bytes(b"{}")
                lock_content = json.dumps(
                    {
                        "execution_target": {"revision": "a" * 40},
                        "max_workers": 32,
                        "protocol": {"ordered_seeds": list(range(500000, 500100))},
                    }
                ).encode()
                (evidence / "candidate-lock.json").write_bytes(lock_content)
                lock_digest = hashlib.sha256(lock_content).hexdigest()
                (root / "state.json").write_text(
                    json.dumps(
                        {
                            "issue": str(event),
                            "instance_id": "i-fixture",
                            "command_id": "c-fixture",
                            "transfer_bucket": "fixture",
                            "region": "ap-northeast-1",
                            "launch_time_utc": "2026-09-29T00:00:00Z",
                            "instance_type": "c7i.8xlarge",
                            "vcpu": 32,
                            "workers": 32,
                            "arena_revision": "a" * 40,
                            "seeds": "500000:500099",
                            "fail_safe_deadline_utc": "2026-09-29T03:00:00Z",
                            "instance_hourly_rate_usd": 0.45,
                            "root_ebs_hourly_usd": 0.001,
                        }
                    )
                )
                (root / "completion.json").write_text(
                    json.dumps(
                        {
                            "run_id": "fixture",
                            "event": event,
                            "arena_revision": "a" * 40,
                            "workers": 32,
                            "lock_sha256": lock_digest,
                            "comparison_sha256": digest,
                            "candidate_result_sha256": digest,
                            "nproc": 32,
                            "event_start_epoch": 1,
                            "event_end_epoch": 2,
                            "classification": "INCONCLUSIVE",
                            "result_identity": "f" * 64,
                        }
                    )
                )
                harness = r"""
param($ScriptPath, $Root, $VerifyOk, [int]$EvaluationEvent)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($ScriptPath,[ref]$null,[ref]$errors)
if (@($errors).Count) { throw ($errors | Out-String) }
$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$Region='ap-northeast-1'; $KeepBucket=$false; $SeedBlockCount=100
$invariant=[Globalization.CultureInfo]::InvariantCulture
$PublicIpv4HourlyUsd=.005; $S3AndTransferBoundUsd=.1
$Objects=@('candidate-lock.json','comparison.json','candidate-result.json')
function Get-RunDirectory { return $Root }
function Get-RunInstance { return $null }
function Invoke-AwsTextAllowFailure { return [pscustomobject]@{ExitCode=1;Text='404 Not Found'} }
$script:eventChecked=$false
function Invoke-LocalPython {
param([string[]]$Arguments)
if ($Arguments[0] -eq '-c') {
    if ($Arguments[1] -notmatch 'formal436 import require_aws_lock') { throw 'Wrong event checker' }
    $script:eventChecked=$true
    return [pscustomobject]@{ExitCode=0;Text=''}
}
if ($EvaluationEvent -eq 436 -and -not $script:eventChecked) { throw 'Missing event check' }
return [pscustomobject]@{ExitCode=$(if ($VerifyOk -eq 'True') {0} else {1});Text="result_identity=$('f'*64)`nclassification=INCONCLUSIVE"} }
function Remove-TransferBucket { Set-Content (Join-Path $Root 'deleted') 'yes' }
function Get-ResidualResources { return [pscustomobject]@{instances_not_terminated=@();volumes=@();snapshots=@();network_interfaces=@();elastic_ips=@();buckets=@()} }
try { Invoke-Collect -Id 'fixture' } catch { if ($VerifyOk -eq 'True') { throw }; Write-Host $_ }
"""
                runner = root / "test.ps1"
                runner.write_text(harness)
                process = subprocess.run(
                    [
                        pwsh,
                        "-NoProfile",
                        "-File",
                        str(runner),
                        str(path),
                        tmp,
                        str(verify_ok),
                        str(event),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                collected = json.loads((root / "collection.json").read_text())
                self.assertIsNone(collected["terminated_time_utc"])
                self.assertIsNone(collected["billable_runtime_seconds"])
                self.assertEqual((root / "deleted").exists(), verify_ok)
                self.assertEqual(
                    collected["result"],
                    "INCONCLUSIVE" if verify_ok else "STOP / INVALID",
                )
                if not verify_ok:
                    self.assertIn("verification_error", collected)


if __name__ == "__main__":
    unittest.main()
