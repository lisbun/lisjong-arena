from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-riichilab-12h.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-riichilab-12h.ps1"
_COLLECTOR = _REPOSITORY_ROOT / "scripts" / "aws" / "collect-riichilab-12h.ps1"


class AwsRiichiLabAutomationScriptTest(unittest.TestCase):
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

    def test_powershell_scripts_have_valid_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        for script in (_LAUNCHER, _COLLECTOR):
            with self.subTest(script=script.name):
                path = str(script).replace("'", "''")
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

    def test_launcher_keeps_aws_access_and_teardown_invariants(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('HttpTokens = "required"', text)
        self.assertIn('InstanceInitiatedShutdownBehavior = "terminate"', text)
        self.assertIn('"AWS-RunShellScript"', text)
        self.assertIn("executionTimeout", text)
        self.assertIn("lisjong-cost-failsafe", text)
        self.assertIn("terminate-instances", text)
        self.assertIn("collect-riichilab-12h.ps1", text)
        self.assertNotIn("--user-data", text)
        self.assertNotIn("authorize-security-group-ingress", text.lower())

    def test_launcher_supports_non_billable_preflight_and_detached_submit(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[switch]$PreflightOnly", text)
        self.assertIn("[switch]$SubmitOnly", text)
        self.assertIn('Write-Host "PASS: AWS PREFLIGHT ONLY"', text)
        self.assertIn("projected_known_cost_usd", text)
        self.assertLess(
            text.index("if ($PreflightOnly)"), text.index('"ec2", "run-instances"')
        )
        self.assertIn('Write-Host "SUBMITTED: remote run is detached', text)
        self.assertLess(text.index("if ($SubmitOnly)"), text.index('$lastStatus = ""'))
        self.assertIn("collect-riichilab-12h.ps1", text)

    def test_collector_preserves_remote_run_and_teardown_invariants(self) -> None:
        text = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn('"ssm", "get-command-invocation"', text)
        self.assertIn('if ($status -in @("Pending", "InProgress", "Delayed"))', text)
        self.assertIn("No termination or teardown action was taken.", text)
        self.assertIn("LISJONG_COMPLETION_JSON_B64=", text)
        self.assertIn("approximate_public_ipv4_cost_usd", text)
        self.assertIn('state" -Value "remote_verified_teardown_pending"', text)
        self.assertIn('"ec2", "terminate-instances"', text)
        self.assertIn('"ec2", "describe-volumes"', text)
        self.assertIn('"ec2", "describe-snapshots"', text)
        self.assertNotIn("--user-data", text)
        self.assertNotIn("authorize-security-group-ingress", text.lower())

    def test_bootstrap_keeps_runtime_secret_ephemeral(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("secretsmanager get-secret-value", text)
        self.assertIn('export LISJONG_DEV_BOT_TOKEN="$TOKEN"', text)
        self.assertIn("unset LISJONG_DEV_BOT_TOKEN", text)
        self.assertNotIn('echo "$TOKEN"', text)
        self.assertNotIn('printf "$TOKEN"', text)


if __name__ == "__main__":
    unittest.main()
