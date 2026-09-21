from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MONITOR = _REPOSITORY_ROOT / "scripts" / "aws" / "ssm-monitor.ps1"
_RIICHILAB_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-riichilab-12h.ps1"
_WAIT_SHAPE_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-wait-shape-326.ps1"


class AwsSsmMonitorTest(unittest.TestCase):
    def _run_monitor(
        self,
        probes: list[dict[str, object]],
        *,
        guidance: bool = False,
        state_path: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        fixture = base64.b64encode(json.dumps(probes).encode()).decode()
        monitor_path = str(_MONITOR).replace("'", "''")
        recovery_path = str(state_path or Path("state.json")).replace("'", "''")
        guidance_call = ""
        if guidance:
            guidance_call = f"""
Write-SsmMonitorDetachedGuidance `
    -RunId 'run-333' `
    -AwsProfile 'test-profile' `
    -Region 'ap-northeast-1' `
    -StatePath '{recovery_path}' `
    -AuthenticationRequired ([bool]$result.AuthenticationRequired) `
    -FailSafeArmed $true `
    -FailSafeHours 8
"""
        script = f"""
$fixtureJson = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('{fixture}')
)
$script:probes = @($fixtureJson | ConvertFrom-Json)
$script:probeIndex = 0
$script:calls = @()
$script:sleeps = @()
function Invoke-AwsTextAllowFailure {{
    param([string[]]$Arguments)
    $script:calls += ($Arguments -join ' ')
    $value = $script:probes[$script:probeIndex]
    $script:probeIndex += 1
    [pscustomobject]@{{
        ExitCode = [int]$value.ExitCode
        Text = [string]$value.Text
    }}
}}
function Start-Sleep {{
    param([int]$Seconds)
    $script:sleeps += $Seconds
}}
. '{monitor_path}'
$result = Wait-SsmLongRunningInvocation `
    -CommandId 'cmd-333' `
    -InstanceId 'i-333' `
    -PollSeconds 0 `
    -MaxConsecutivePollFailures 3 `
    -PollRetryDelaysSeconds @(0, 0)
{guidance_call}
$envelope = [ordered]@{{
    Outcome = [string]$result.Outcome
    Status = [string]$result.Invocation.Status
    AuthenticationRequired = [bool]$result.AuthenticationRequired
    ConsecutivePollFailures = [int]$result.ConsecutivePollFailures
    Calls = @($script:calls)
    Sleeps = @($script:sleeps)
}}
$json = $envelope | ConvertTo-Json -Compress -Depth 10
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
Write-Output "RESULT_B64=$encoded"
"""
        completed = subprocess.run(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = completed.stdout + completed.stderr
        sentinel = next(
            (line for line in output.splitlines() if line.startswith("RESULT_B64=")),
            None,
        )
        self.assertIsNotNone(sentinel, output)
        assert sentinel is not None
        document = json.loads(base64.b64decode(sentinel.removeprefix("RESULT_B64=")))
        return completed, document

    @staticmethod
    def _success(status: str) -> dict[str, object]:
        return {"ExitCode": 0, "Text": json.dumps({"Status": status})}

    @staticmethod
    def _failure(text: str = "temporary AWS CLI failure") -> dict[str, object]:
        return {"ExitCode": 41, "Text": text}

    def test_nonterminal_statuses_continue_until_success(self) -> None:
        completed, result = self._run_monitor(
            [
                self._success("Pending"),
                self._success("InProgress"),
                self._success("Delayed"),
                self._success("Success"),
            ]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("Success", result["Outcome"])
        self.assertEqual("Success", result["Status"])
        self.assertEqual(4, len(result["Calls"]))

    def test_confirmed_terminal_failure_is_remote_failure(self) -> None:
        _, result = self._run_monitor([self._success("Failed")])
        self.assertEqual("RemoteFailure", result["Outcome"])
        self.assertEqual("Failed", result["Status"])
        self.assertFalse(result["AuthenticationRequired"])

    def test_one_transient_failure_retries_and_recovers(self) -> None:
        completed, result = self._run_monitor(
            [self._failure(), self._success("Success")]
        )
        output = completed.stdout + completed.stderr
        self.assertEqual("Success", result["Outcome"])
        self.assertEqual(2, len(result["Calls"]))
        self.assertEqual([0], result["Sleeps"])
        self.assertIn("attempt 1 of 3", output)

    def test_retry_exhaustion_detaches_generic_monitor(self) -> None:
        completed, result = self._run_monitor(
            [self._failure(), self._failure(), self._failure()],
            guidance=True,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual("MonitorDetached", result["Outcome"])
        self.assertEqual(3, result["ConsecutivePollFailures"])
        self.assertEqual(3, len(result["Calls"]))
        self.assertFalse(result["AuthenticationRequired"])
        self.assertIn("LOCAL MONITOR DETACHED", output)
        self.assertIn("REMOTE EXECUTION STATUS = UNKNOWN", output)
        self.assertIn("Do not resubmit", output)
        self.assertNotIn("scientific execution failure", output.lower())

    def test_expired_authentication_guides_run_id_reattachment(self) -> None:
        expired = self._failure(
            "ExpiredTokenException: The security token included in the request is expired"
        )
        completed, result = self._run_monitor(
            [expired, expired, expired], guidance=True
        )
        output = completed.stdout + completed.stderr
        self.assertEqual("MonitorDetached", result["Outcome"])
        self.assertTrue(result["AuthenticationRequired"])
        self.assertIn("AWS AUTHENTICATION REQUIRED", output)
        self.assertIn("aws login --profile test-profile", output)
        self.assertIn("status-run.ps1 -RunId run-333", output)
        self.assertIn("instance-side fail-safe remains armed", output)

    def test_detachment_preserves_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state = {
                "run_id": "run-333",
                "instance_id": "i-333",
                "command_id": "cmd-333",
                "region": "ap-northeast-1",
                "artifact_volume_id": "vol-333",
                "fail_safe_armed": True,
                "fail_safe_deadline_utc": "2026-09-21T20:00:00Z",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = state_path.read_bytes()
            failures = [self._failure(), self._failure(), self._failure()]
            self._run_monitor(failures, guidance=True, state_path=state_path)
            self.assertEqual(before, state_path.read_bytes())

    def test_launchers_use_monitor_without_resubmission_or_detached_termination(
        self,
    ) -> None:
        monitor = _MONITOR.read_text(encoding="utf-8")
        self.assertEqual(1, monitor.count('"ssm", "get-command-invocation"'))
        self.assertNotIn("send-command", monitor)
        self.assertNotIn("terminate-instances", monitor)
        for launcher in (_RIICHILAB_LAUNCHER, _WAIT_SHAPE_LAUNCHER):
            with self.subTest(launcher=launcher.name):
                text = launcher.read_text(encoding="utf-8")
                self.assertIn('. (Join-Path $PSScriptRoot "ssm-monitor.ps1")', text)
                self.assertIn("Wait-SsmLongRunningInvocation", text)
                self.assertIn("if ($monitorDetached) {", text)
                self.assertIn("Remote scientific execution failure confirmed", text)
                self.assertIn("lisjong-scientific-command-id", text)
                detached_guard = text.index("if ($monitorDetached) {")
                self.assertLess(
                    detached_guard,
                    text.index("Request-Termination", detached_guard),
                )

    def test_recovery_state_is_written_before_monitoring(self) -> None:
        required = ("run_id", "instance_id", "command_id", "region")
        for launcher in (_RIICHILAB_LAUNCHER, _WAIT_SHAPE_LAUNCHER):
            with self.subTest(launcher=launcher.name):
                text = launcher.read_text(encoding="utf-8")
                monitor_index = text.index(
                    "$monitorResult = Wait-SsmLongRunningInvocation"
                )
                state_index = text.rindex("Write-JsonFile", 0, monitor_index)
                state_block = text[state_index:monitor_index]
                for field in required:
                    self.assertIn(field, state_block)
                self.assertIn("fail_safe_armed", state_block)
                self.assertIn("volume", state_block)


if __name__ == "__main__":
    unittest.main()
