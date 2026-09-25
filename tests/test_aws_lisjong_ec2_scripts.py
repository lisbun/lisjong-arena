"""#379 generic EC2 lifecycle wrapper contract tests (no AWS call)."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_AWS = Path(__file__).resolve().parents[1] / "scripts" / "aws"
_WRAPPER = _AWS / "lisjong-ec2.ps1"
_RUNNER = _AWS / "lisjong-ec2-runner.sh"


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _RUNNER.read_text(encoding="utf-8")

    def test_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(_RUNNER)], check=True)

    def test_logs_and_progress_sync_periodically_not_only_on_exit(self) -> None:
        self.assertIn('while sleep "$SYNC_SECONDS"; do sync_logs; done', self.text)
        self.assertIn('"$OUTPUT_DIR"/progress*', self.text)
        self.assertLess(
            self.text.index("SYNC_PID=$!"),
            self.text.index('bash "$INPUT_DIR/$BOOTSTRAP"'),
        )

    def test_inputs_are_checked_and_completion_is_uploaded_last(self) -> None:
        self.assertLess(
            self.text.index("sha256sum --strict -c manifest.sha256"),
            self.text.index('bash "$INPUT_DIR/$BOOTSTRAP"'),
        )
        finalize = self.text[self.text.index("finalize() {") :]
        self.assertLess(
            finalize.index('put "$OUTPUT_DIR/sha256sums.txt"'),
            finalize.index('put "$OUTPUT_DIR/_completion.json"'),
        )
        self.assertIn('"$WORKERS" -gt "$(nproc)"', self.text)


class WrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _WRAPPER.read_text(encoding="utf-8")

    def test_reuses_existing_monitor_and_fail_safe(self) -> None:
        self.assertIn('. (Join-Path $PSScriptRoot "ssm-monitor.ps1")', self.text)
        self.assertIn(
            "lisjong_arena.aws_operational_calibration boot-fail-safe-user-data",
            self.text,
        )
        self.assertNotIn("pendingTime", self.text)

    def test_launch_shape(self) -> None:
        self.assertIn('HttpTokens = "required"', self.text)
        self.assertIn('InstanceInitiatedShutdownBehavior = "terminate"', self.text)
        self.assertIn('VolumeType = "gp3"; Encrypted = $true', self.text)
        self.assertIn("[ValidateRange(8, 1024)][int]$RootVolumeGiB = 30", self.text)
        self.assertIn("[ValidateRange(1, 24)][double]$FailSafeHours = 8", self.text)
        self.assertIn("if ($Workers -gt $vcpu) { throw", self.text)
        self.assertIsNone(re.search(r"(?i)spot", self.text))

    def test_no_billable_call_before_plan_match_and_dry_run(self) -> None:
        create = self.text.index('"s3api", "create-bucket"')
        self.assertLess(self.text.index('"run-instances", "--dry-run"'), create)
        self.assertLess(self.text.index("differ from the reviewed plan"), create)
        self.assertLess(self.text.index("Launch requires -Plan"), create)

    def test_collect_does_not_terminate_unfinished_work_without_force(self) -> None:
        collect = self.text[self.text.index("function Invoke-Collect") :]
        stop = collect.index("Stop-RunInstance")
        self.assertLess(collect.index("-not $ForceTerminate"), stop)
        self.assertLess(collect.index("Wait-Workload"), stop)
        self.assertLess(collect.index('"$Id/output/_completion.json"'), stop)
        self.assertLess(collect.index('"$Id/output/sha256sums.txt"'), stop)
        self.assertLess(stop, collect.index("Remove-RunBucket"))

    def test_powershell_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        script = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{_WRAPPER}',[ref]$null,[ref]$e); if (@($e).Count) {{ $e; exit 1 }}"
        )
        subprocess.run([pwsh, "-NoProfile", "-Command", script], check=True)


if __name__ == "__main__":
    unittest.main()
