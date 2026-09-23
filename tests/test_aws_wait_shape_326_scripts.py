from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-wait-shape-326.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-wait-shape-326.ps1"
_STATUS = _REPOSITORY_ROOT / "scripts" / "aws" / "status-run.ps1"
_CORE = _REPOSITORY_ROOT / "src" / "lisjong_arena" / "aws_execution_observability.py"


class AwsWaitShape326ScriptTest(unittest.TestCase):
    def _run_status_with_fake_aws(
        self,
        rules: list[tuple[tuple[str, ...], dict[str, object]]],
    ) -> subprocess.CompletedProcess[str]:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        status_path = str(_STATUS).replace("'", "''")
        lines = [
            "function global:aws {",
            "    $joined = $args -join ' '",
        ]
        for required, payload in rules:
            condition = " -and ".join(
                f"$joined.Contains('{value.replace("'", "''")}')" for value in required
            )
            encoded = base64.b64encode(
                json.dumps(payload, separators=(",", ":")).encode()
            ).decode()
            lines.extend(
                [
                    f"    if ({condition}) {{",
                    "        $global:LASTEXITCODE = 0",
                    "        [Text.Encoding]::UTF8.GetString("
                    f"[Convert]::FromBase64String('{encoded}'))",
                    "        return",
                    "    }",
                ]
            )
        lines.extend(
            [
                "    $global:LASTEXITCODE = 41",
                '    [Console]::Error.WriteLine("unexpected fake AWS call: $joined")',
                "}",
                f"& '{status_path}' -RunId run-1 -AwsProfile fake -Region ap-northeast-1",
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            wrapper = Path(temp) / "status-fixture.ps1"
            wrapper.write_text("\n".join(lines), encoding="utf-8")
            return subprocess.run(
                [
                    pwsh,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(wrapper),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # A non-UTC zone exposes local-vs-UTC deadline parsing bugs.
                env={**os.environ, "TZ": "Asia/Tokyo"},
            )

    def test_bootstrap_has_valid_bash_syntax(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        try:
            result = subprocess.run(
                [bash, "-n", str(_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            self.skipTest(f"bash cannot be executed: {error}")
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

    def test_historical_launcher_is_closed_before_any_aws_operation(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        closed = "Issue #326 historical scientific allocation is closed"
        self.assertIn(closed, text)
        self.assertLess(text.index(closed), text.index("& aws --version"))

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        result = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(_LAUNCHER),
                "-AwsProfile",
                "must-not-be-used",
                "-PreflightOnly",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(closed, result.stderr + result.stdout)
        self.assertNotIn("AWS CLI is not available", result.stderr + result.stdout)

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
        self.assertNotIn("$historical326Revision", text)

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

    def test_archived_executor_uses_timer_arm_deadline_and_full_billable_window(
        self,
    ) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn(".AddHours($FailSafeHours).ToString", text)
        self.assertIn("LISJONG_FAILSAFE_DEADLINE_EPOCH=", text)
        self.assertIn("lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc", text)
        self.assertIn("$terminationConfirmedUtc - $launchTimeUtc", text)
        self.assertIn('"--scientific-runtime-seconds"', text)
        self.assertIn('"--ec2-billable-runtime-seconds"', text)
        self.assertIn("lisjong-ec2-billable-runtime-sec", text)

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
            "Billable execution requires a numeric calibrated scientific runtime range",
            text,
        )
        self.assertIn("no billable-overhead calibration", core)
        self.assertIn("estimated_fail_safe_cost_exposure", core)
        self.assertIn("unknown / not hard-bounded", text)
        self.assertNotIn("maximum AWS bill", text + core)

    def test_bootstrap_writes_progress_but_does_not_guess_billable_runtime(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('PROGRESS_PATH="$OPERATIONAL_ROOT/progress.json"', text)
        self.assertIn("--operational-progress-path", text)
        self.assertNotIn("aws_execution_observability calibration", text)
        self.assertNotIn("CALIBRATION_PATH", text)

    def test_running_status_executes_with_percentage_exposure_and_warning(self) -> None:
        now = datetime.now(UTC)
        launch = now - timedelta(hours=1)
        deadline = now + timedelta(hours=1)
        estimated_finish = deadline + timedelta(minutes=5)
        instance = {
            "InstanceId": "i-running",
            "State": {"Name": "running"},
            "InstanceType": "t3.small",
            "LaunchTime": launch.isoformat(),
            "Tags": [
                {"Key": "lisjong-run-id", "Value": "run-1"},
                {
                    "Key": "lisjong-progress-path",
                    "Value": "/mnt/run/operational/progress.json",
                },
                {"Key": "lisjong-worker-count", "Value": "2"},
                {
                    "Key": "lisjong-failsafe-deadline",
                    "Value": deadline.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                },
                {"Key": "lisjong-instance-hourly-rate-usd", "Value": "0.1"},
                {"Key": "lisjong-scientific-command-id", "Value": "cmd-science"},
            ],
            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-root"}}],
        }
        progress = {
            "completed_units": 37,
            "total_units": 96,
            "unit_kind": "hanchan",
            "elapsed_seconds": 3600,
            "throughput_per_hour": 37.0,
            "eta_status": "estimated",
            "eta_seconds": 600,
            "estimated_finish_at": estimated_finish.isoformat(),
        }
        rules = [
            (("describe-instances",), {"Reservations": [{"Instances": [instance]}]}),
            (("describe-volumes",), {"Volumes": []}),
            (
                ("describe-instance-types",),
                {
                    "InstanceTypes": [
                        {
                            "VCpuInfo": {"DefaultVCpus": 2},
                            "MemoryInfo": {"SizeInMiB": 2048},
                        }
                    ]
                },
            ),
            (
                ("describe-instance-information",),
                {
                    "InstanceInformationList": [
                        {"InstanceId": "i-running", "PingStatus": "Online"}
                    ]
                },
            ),
            (("get-command-invocation", "cmd-science"), {"Status": "InProgress"}),
            (("send-command",), {"Command": {"CommandId": "cmd-probe"}}),
            (
                ("get-command-invocation", "cmd-probe"),
                {"Status": "Success", "StandardOutputContent": json.dumps(progress)},
            ),
        ]
        result = self._run_status_with_fake_aws(rules)
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertIn("Phase: RUN", output)
        self.assertIn("Progress: 37/96 hanchan (38.5%)", output)
        self.assertIn("Estimated fail-safe EC2 cost exposure: USD", output)
        self.assertIn("Estimated finish reaches or exceeds", output)
        remaining = int(re.search(r"remaining seconds=(-?\d+)", output).group(1))
        self.assertAlmostEqual(3600, remaining, delta=300)

    def test_terminated_status_executes_and_displays_final_calibration(self) -> None:
        rules = self._completed_status_rules(include_instance=True)
        result = self._run_status_with_fake_aws(rules)
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertIn("Phase: COMPLETE", output)
        self.assertIn("state=terminated / compute billing continues=False", output)
        self.assertIn("scientific runtime seconds=6000", output)
        self.assertIn("EC2 billable runtime seconds=7200", output)
        self.assertIn("Estimated realized EC2 cost: USD 0.2", output)
        self.assertIn(
            "Prediction error: scientific runtime seconds=600 / EC2 billable runtime seconds=unavailable / cost USD=unavailable",
            output,
        )

    def test_local_state_missing_status_recovers_completion_from_run_id(self) -> None:
        rules = self._completed_status_rules(include_instance=False)
        result = self._run_status_with_fake_aws(rules)
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertIn("Phase: COMPLETE", output)
        self.assertIn("state=not-found / compute billing continues=False", output)
        self.assertIn("EBS: vol-artifact", output)
        self.assertIn("EC2 billable runtime seconds=7200", output)

    def _completed_status_rules(
        self, *, include_instance: bool
    ) -> list[tuple[tuple[str, ...], dict[str, object]]]:
        tags = [
            {"Key": "Purpose", "Value": "wait-shape-pilot-artifact"},
            {"Key": "lisjong-run-id", "Value": "run-1"},
            {"Key": "lisjong-worker-count", "Value": "2"},
            {"Key": "lisjong-scientific-runtime-sec", "Value": "6000"},
            {"Key": "lisjong-ec2-billable-runtime-sec", "Value": "7200"},
            {"Key": "lisjong-throughput-per-hour", "Value": "57.6"},
            {"Key": "lisjong-realized-cost-usd", "Value": "0.2"},
            {"Key": "lisjong-scientific-runtime-error-sec", "Value": "600"},
        ]
        volume = {
            "VolumeId": "vol-artifact",
            "State": "available",
            "Size": 1,
            "Encrypted": True,
            "Attachments": [],
            "Tags": tags,
        }
        instances: list[dict[str, object]] = []
        if include_instance:
            instances.append(
                {
                    "InstanceId": "i-complete",
                    "State": {"Name": "terminated"},
                    "InstanceType": "t3.small",
                    "LaunchTime": "2026-09-21T00:00:00Z",
                    "Tags": [{"Key": "lisjong-run-id", "Value": "run-1"}],
                    "BlockDeviceMappings": [],
                }
            )
        rules: list[tuple[tuple[str, ...], dict[str, object]]] = [
            (("describe-instances",), {"Reservations": [{"Instances": instances}]}),
            (("describe-volumes",), {"Volumes": [volume]}),
        ]
        if include_instance:
            rules.append(
                (
                    ("describe-instance-types",),
                    {
                        "InstanceTypes": [
                            {
                                "VCpuInfo": {"DefaultVCpus": 2},
                                "MemoryInfo": {"SizeInMiB": 2048},
                            }
                        ]
                    },
                )
            )
        return rules

    def test_status_rediscovery_is_run_id_based_and_does_not_resubmit_science(
        self,
    ) -> None:
        status = _STATUS.read_text(encoding="utf-8")
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory = $true)][string]$RunId", status)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", status)
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
