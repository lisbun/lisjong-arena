from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-riichilab-12h.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-riichilab-12h.ps1"


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
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
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
