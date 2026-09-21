from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-wait-shape-326.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-wait-shape-326.ps1"


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

    def test_launcher_keeps_bounded_compute_and_teardown_invariants(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('[string]$InstanceType = "t3.small"', text)
        self.assertIn("[ValidateSet(1, 2)][int]$MaxWorkers = 2", text)
        self.assertIn('HttpTokens = "required"', text)
        self.assertIn('InstanceInitiatedShutdownBehavior = "terminate"', text)
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


if __name__ == "__main__":
    unittest.main()
