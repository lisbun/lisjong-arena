from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-wait-shape-326.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-wait-shape-326.ps1"
_STATUS = _REPOSITORY_ROOT / "scripts" / "aws" / "status-run.ps1"
_CORE = _REPOSITORY_ROOT / "src" / "lisjong_arena" / "aws_execution_observability.py"


class AwsWaitShape326ScriptTest(unittest.TestCase):
    def test_bootstrap_has_valid_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        result = subprocess.run(
            [bash, "-n", str(_BOOTSTRAP)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_launcher_has_valid_powershell_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        path = str(_LAUNCHER).replace("'", "''")
        command = (
            f"$content = Get-Content -Raw -LiteralPath '{path}'; "
            "[scriptblock]::Create($content) | Out-Null"
        )
        result = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_status_has_valid_powershell_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        path = str(_STATUS).replace("'", "''")
        command = (
            f"$content = Get-Content -Raw -LiteralPath '{path}'; "
            "[scriptblock]::Create($content) | Out-Null"
        )
        result = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_preflight_is_non_billable_and_binds_executor_before_resource_creation(
        self,
    ) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[switch]$PreflightOnly", text)
        self.assertIn("wait_shape_qualification.pilot preflight", text)
        self.assertIn("targeted-honor-release-terminal-progression", text)
        self.assertIn(
            "15b9b3ad22569491860593509647d753f2b7160680ff0fb3c8b953575530784d",
            text,
        )
        self.assertIn("PASS: ISSUE #326 AWS PREFLIGHT ONLY", text)
        self.assertIn(
            "No EC2 instance, EBS artifact volume, or other billable execution resource was created.",
            text,
        )
        preflight_guard = (
            "if ($PreflightOnly) {\n"
            '    Write-Host "PASS: ISSUE #326 AWS PREFLIGHT ONLY"'
        )
        self.assertLess(
            text.index("wait_shape_qualification.pilot preflight"),
            text.index(preflight_guard),
        )
        self.assertLess(
            text.index(preflight_guard),
            text.index('"ec2", "create-volume"'),
        )
        self.assertLess(
            text.index(preflight_guard),
            text.index('"ec2", "run-instances"'),
        )

    def test_launcher_rejects_unbounded_instance_type_and_failsafe(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('if ($InstanceType -ne "t3.small") {', text)
        self.assertIn(
            'throw "Issue #326 locks bounded compute to t3.small."',
            text,
        )
        self.assertIn(
            "if ($FailSafeHours -lt 4 -or $FailSafeHours -gt 8) {",
            text,
        )
        self.assertIn(
            '$historical326Revision = "13317bd85bc2f80a575d487cae9dda1bebd7f5f0"',
            text,
        )
        self.assertIn("must not be restarted or instrumented", text)

    def test_launcher_keeps_bounded_compute_and_teardown_invariants(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('[string]$InstanceType = "t3.small"', text)
        self.assertIn("[ValidateSet(1, 2)][int]$MaxWorkers = 2", text)
        self.assertIn('HttpTokens = "required"', text)
        self.assertIn('InstanceInitiatedShutdownBehavior = "terminate"', text)
        self.assertIn("DeleteOnTermination -ne $true", text)
        self.assertIn("instanceInitiatedShutdownBehavior", text)
        self.assertIn("lisjong-cost-failsafe", text)
        self.assertIn('"ec2", "terminate-instances"', text)
        self.assertIn('"ec2", "wait", "instance-terminated"', text)
        self.assertNotIn("--user-data", text)
        self.assertNotIn("authorize-security-group-ingress", text.lower())

    def test_launcher_retains_only_bounded_encrypted_artifact_volume_after_compute(
        self,
    ) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"--size", "1"', text)
        self.assertIn('"--volume-type", "gp3"', text)
        self.assertIn('"--encrypted"', text)
        self.assertIn("artifact_volume_retained_after_compute = $true", text)
        self.assertIn("retained_artifact_volume_encrypted", text)
        self.assertIn("retained_artifact_volume_attachment_count", text)
        self.assertIn(
            "One encrypted 1 GiB gp3 EBS volume is intentionally retained",
            text,
        )
        self.assertIn(
            "Artifact volume $artifactVolumeId is intentionally preserved",
            text,
        )

    def test_bootstrap_runs_only_locked_pilot_and_persists_verified_artifacts(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("wait_shape_qualification.pilot preflight", text)
        self.assertIn("wait_shape_qualification.pilot run", text)
        self.assertIn("wait_shape_qualification.pilot verify", text)
        self.assertIn("--repository-collision-audit-pass", text)
        self.assertIn("--private-collision-audit-pass", text)
        self.assertIn("--no-prior-result-exposure-confirmed", text)
        self.assertIn("seeds=2000..2095", text)
        self.assertNotIn("2100..", text)
        self.assertNotIn("2196..", text)
        self.assertNotIn("2220..", text)
        self.assertIn("/mnt/lisjong-326-artifacts", text)
        self.assertIn("execution-lock.json", text)
        self.assertIn('root / "pilot-raw" / "manifest.json"', text)
        self.assertIn("f1.json", text)
        self.assertIn("f2.json", text)
        self.assertIn("qualification.json", text)
        self.assertIn("LISJONG_326_COMPLETION_JSON_B64=", text)
        self.assertNotIn("secretsmanager", text.lower())
        self.assertNotIn("LISJONG_DEV_BOT_TOKEN", text)

    def test_plan_and_pricing_precede_every_billable_resource(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        core = _CORE.read_text(encoding="utf-8")
        plan_write = text.index('"lisjong_arena.aws_execution_observability", "plan"')
        self.assertLess(plan_write, text.index('"ec2", "create-volume"'))
        self.assertLess(plan_write, text.index('"ec2", "run-instances"'))
        self.assertIn('"ec2", "describe-instance-types"', text)
        self.assertIn('"pricing", "get-products"', text)
        self.assertIn("pricing_checked_at", text)
        self.assertIn("instance_hourly_rate_usd", text)
        self.assertIn("no matching historical evidence", core)
        self.assertIn(
            "Billable execution requires a numeric calibrated runtime range",
            text,
        )
        self.assertIn("estimated_fail_safe_cost_exposure", core)
        self.assertIn("unknown / not hard-bounded", text)
        self.assertNotIn("maximum AWS bill", text + core)

    def test_future_pilot_writes_separate_progress_and_calibration(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('PROGRESS_PATH="$OPERATIONAL_ROOT/progress.json"', text)
        self.assertIn('CALIBRATION_PATH="$OPERATIONAL_ROOT/calibration.json"', text)
        self.assertIn("--operational-progress-path", text)
        self.assertIn("aws_execution_observability calibration", text)

    def test_status_rediscovery_is_run_id_based_and_does_not_resubmit_science(
        self,
    ) -> None:
        status = _STATUS.read_text(encoding="utf-8")
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory = $true)][string]$RunId", status)
        self.assertIn("Name=tag:lisjong-run-id,Values=$RunId", status)
        self.assertIn('"ec2", "describe-instances"', status)
        self.assertIn('"ec2", "describe-volumes"', status)
        self.assertIn("state.json is only an optional cache", status)
        self.assertNotIn("wait_shape_qualification.pilot", status)
        self.assertNotIn("bootstrap-wait-shape", status)
        self.assertEqual(status.count('"ssm", "send-command"'), 1)
        self.assertIn("test -r '$ProgressPath' && cat", status)
        self.assertIn("lisjong-scientific-command-id", launcher)
        self.assertIn("lisjong-progress-path", launcher)

    def test_status_surfaces_compute_failsafe_and_retained_ebs(self) -> None:
        text = _STATUS.read_text(encoding="utf-8")
        self.assertIn('"ec2", "describe-instance-types"', text)
        self.assertIn("vCPU=$vcpu", text)
        self.assertIn("memory MiB=$memoryMiB", text)
        self.assertIn("workers=$workers", text)
        self.assertIn("Fail-safe deadline", text)
        self.assertIn("retained=$retained", text)
        self.assertIn("size GiB=$($volume.Size)", text)
        self.assertIn("encrypted=$($volume.Encrypted)", text)
        self.assertIn("attachments=$(@($volume.Attachments).Count)", text)
        self.assertIn("billing continues=$billingContinues", text)


if __name__ == "__main__":
    unittest.main()
