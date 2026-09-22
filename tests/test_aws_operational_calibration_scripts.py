from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lisjong_arena import aws_operational_calibration, seed_registry
from lisjong_arena.offense_foundation import instrumentation

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _ROOT / "scripts" / "aws" / "start-operational-calibration-340.ps1"
_BOOTSTRAP = _ROOT / "scripts" / "aws" / "bootstrap-operational-calibration-340.sh"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_CALIBRATION_FIRST_SEED = 906_000
_CALIBRATION_UNITS = 16


def _normalize_console_output(text):
    plain = _ANSI_ESCAPE.sub("", text)
    plain = re.sub(r"(?m)^\s*(?:\d+\s*)?\|\s?", " ", plain)
    return " ".join(plain.split())


class OperationalCalibrationScriptTest(unittest.TestCase):
    def test_scripts_have_valid_shell_syntax(self):
        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            escaped = str(_LAUNCHER).replace("'", "''")
            command = (
                f"$content = Get-Content -Raw -LiteralPath '{escaped}'; "
                "[scriptblock]::Create($content) | Out-Null"
            )
            result = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if bash is not None:
            try:
                result = subprocess.run(
                    [bash, "-n", str(_BOOTSTRAP)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as error:
                self.skipTest(f"bash cannot be executed: {error}")
            self.assertEqual(0, result.returncode, result.stderr)

    def test_a_calibration_can_never_run_a_scientific_population(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        for forbidden in (
            "offense_foundation lock",
            "offense_foundation generate",
            "offense_foundation readback",
            "source-readback",
            "p2-corpus",
            "populations",
            "allocation_bindings",
            "TRAIN",
            "SELECT",
            "OFFLINE-EVAL",
        ):
            self.assertNotIn(forbidden, bootstrap)
            self.assertNotIn(forbidden, launcher)
        self.assertIn("offense_foundation calibrate", bootstrap)
        self.assertNotIn("validate-request", launcher)
        self.assertNotIn("scientific_submission_authorized", launcher)
        self.assertIn("workload_submission_authorized", launcher)

    def test_the_calibration_needs_no_prior_calibration(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("admit-calibration", launcher)
        self.assertNotIn("admit-phase-1", launcher)
        self.assertNotIn("-CalibrationEvidencePath", launcher)
        self.assertIn("calibration is the measurement", launcher)

    def test_calibration_gates_precede_the_actions_they_guard(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        admission = launcher.index("admit-calibration")
        self.assertLess(admission, launcher.index('"ec2", "create-volume"'))
        self.assertLess(admission, launcher.index('"ec2", "run-instances"'))
        no_go = "Calibration admission is NO-GO"
        self.assertIn(no_go, launcher)
        self.assertLess(launcher.index(no_go), launcher.index('"ec2", "create-volume"'))
        phase_two = launcher.index("admit-phase-2")
        self.assertLess(
            phase_two, launcher.index("bootstrap-operational-calibration-340.sh")
        )
        self.assertLess(
            phase_two,
            launcher.index('$longRequestPath = Join-Path $runDir "ssm-run.json"'),
        )
        self.assertLess(phase_two, launcher.index("$workloadSubmitted = $true"))

    def test_the_pre_arm_interval_is_bounded_from_launch(self):
        # Issue #340 blocker: the SSM fail-safe can only be armed once SSM is
        # reachable, so the launch request itself must carry an instance-side
        # timer that bounds the pre-arm interval.
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("$bootFailSafeUserData", launcher)
        self.assertIn("UserData = $bootFailSafeUserData", launcher)
        self.assertIn("lisjong-boot-failsafe", launcher)
        self.assertIn(
            "$bootFailSafeSeconds = [long]($preArmAllowanceSeconds "
            "+ $hardFailSafeSeconds + $teardownSeconds)",
            launcher,
        )
        # The user data is built before the billable resources are created.
        self.assertLess(
            launcher.index("$bootFailSafeUserData = "),
            launcher.index('"ec2", "create-volume"'),
        )
        self.assertLess(
            launcher.index("UserData = $bootFailSafeUserData"),
            launcher.index("$failSafeRequestPath = Join-Path"),
        )
        self.assertIn("$PreArmAllowanceMinutes = 20", launcher)
        self.assertIn("setup_seconds = $preArmAllowanceSeconds", launcher)
        self.assertIn("boot_fail_safe_armed = $bootFailSafeArmed", launcher)

    def test_the_calibration_lifecycle_is_bounded_and_teardown_confirmed(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("FailSafeHours -lt 1 -or $FailSafeHours -gt 12", launcher)
        self.assertIn("$MaxUnitCount = 64", launcher)
        self.assertIn("$MaxWorkerCount = 32", launcher)
        self.assertIn("MAX_ALLOWED_UNITS=256", bootstrap)
        self.assertIn("lisjong-cost-failsafe", launcher)
        self.assertIn("lisjong-340-normal-teardown", bootstrap)
        self.assertIn('"ec2", "wait", "instance-terminated"', launcher)
        self.assertIn("an unpriced material charge is never treated as zero", launcher)
        # The measured billable window is built from the real EC2 clock.
        self.assertIn("ec2_billable_runtime_seconds", launcher)
        self.assertIn("setup_overhead_seconds", launcher)
        self.assertIn("teardown_overhead_seconds", launcher)

    def test_the_calibration_publishes_durable_receipts_and_no_scientific_data(self):
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("--receipt-dir", bootstrap)
        self.assertIn("--operational-progress-path", bootstrap)
        self.assertIn('rm -rf "$SCRATCH_ROOT"', bootstrap)
        self.assertIn("no scientific-looking data is ever retained", bootstrap)
        self.assertEqual(
            "per-seed-durable-receipt",
            instrumentation.CALIBRATION_DURABLE_EVIDENCE_LEVEL,
        )

    def _qualification(self, directory):
        """Produce a real qualification artifact, or skip if it is unavailable."""
        output = directory / "qualification.json"
        result = subprocess.run(
            [
                str(_ROOT / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "lisjong_arena.offense_foundation",
                "qualify",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=_ROOT,
        )
        if result.returncode != 0:
            self.skipTest(
                "a real qualification artifact is unavailable here "
                f"({result.stdout.strip() or result.stderr.strip()})"
            )
        return output

    def _run_launcher(self, *, probe_outcome="PASS", preflight=False):
        arena_revision = "a" * 40
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp, True)
        qualification = self._qualification(temp)
        ledger = seed_registry.new_ledger()
        ledger, record = seed_registry.reserve_allocation(
            ledger,
            owner_issue="lisbun/lisjong-arena#340",
            protocol=aws_operational_calibration.CALIBRATION_PROTOCOL,
            seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            purpose="synthetic operational calibration population",
            population=aws_operational_calibration.CALIBRATION_POPULATION,
            split=None,
            seeds=list(
                range(
                    _CALIBRATION_FIRST_SEED,
                    _CALIBRATION_FIRST_SEED + _CALIBRATION_UNITS,
                )
            ),
            arena_revision=arena_revision,
            protocol_revision="synthetic-test-v1",
            provenance_reference="synthetic test fixture",
            allocation_timestamp="2026-09-22T00:00:00Z",
        )
        ledger_path = temp / "seed-ledger.json"
        seed_registry.write_ledger(ledger_path, ledger)
        charges_path = temp / "charges.json"
        charges_path.write_text(
            json.dumps(
                [
                    {
                        "label": "retained encrypted 8 GiB gp3 calibration volume",
                        "material": True,
                        "max_usd": None,
                        "reason": None,
                        "usd": 0.96,
                    },
                    {
                        "label": "public IPv4 address hours",
                        "material": True,
                        "max_usd": 0.07,
                        "reason": None,
                        "usd": None,
                    },
                    {
                        "label": "data transfer out",
                        "material": False,
                        "max_usd": None,
                        "reason": "evidence stays on the retained EBS volume",
                        "usd": None,
                    },
                ]
            ),
            encoding="utf-8",
        )
        arm_epoch = 1_790_000_000
        stdout = (
            f"LISJONG_FAILSAFE_DEADLINE_EPOCH={arm_epoch + 4 * 3600}\\n"
            f"LISJONG_340_PHASE2_OBSERVED_EPOCH={arm_epoch + 600}\\n"
            f"LISJONG_340_PHASE2_PROBE={probe_outcome}\\n"
        )
        payloads = {
            "get-caller-identity": {"Account": "1"},
            "describe-instance-types": {
                "InstanceTypes": [
                    {
                        "VCpuInfo": {"DefaultVCpus": 16},
                        "MemoryInfo": {"SizeInMiB": 32768},
                    }
                ]
            },
            "global-infrastructure": {"Parameter": {"Value": "Asia Pacific (Tokyo)"}},
            "get-role": {"Role": {"Arn": "arn:aws:iam::1:role/test"}},
            "list-instance-profiles": {
                "InstanceProfiles": [{"InstanceProfileName": "test-profile"}]
            },
            "describe-security-groups": {
                "SecurityGroups": [
                    {
                        "GroupId": "sg-1",
                        "VpcId": "vpc-1",
                        "IpPermissions": [],
                        "IpPermissionsEgress": [{}],
                    }
                ]
            },
            "describe-subnets": {
                "Subnets": [
                    {
                        "SubnetId": "subnet-1",
                        "VpcId": "vpc-1",
                        "AvailabilityZone": "ap-northeast-1a",
                        "MapPublicIpOnLaunch": True,
                    }
                ]
            },
            "ami-amazon-linux": {"Parameter": {"Value": "ami-1234abcd"}},
            "create-volume": {"VolumeId": "vol-c1"},
            "run-instances": {
                "Instances": [
                    {
                        "InstanceId": "i-0123456789abcdef0",
                        "LaunchTime": "2026-09-21T00:00:00Z",
                    }
                ]
            },
            "describe-instance-information": {
                "InstanceInformationList": [
                    {"InstanceId": "i-0123456789abcdef0", "PingStatus": "Online"}
                ]
            },
            "describe-instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-0123456789abcdef0",
                                "MetadataOptions": {"HttpTokens": "required"},
                            }
                        ]
                    }
                ]
            },
            "get-command-invocation": {
                "Status": "Success",
                "StandardOutputContent": stdout,
                "StandardErrorContent": "",
            },
            "send-command": {"Command": {"CommandId": "cmd-1"}},
            "describe-volumes": {
                "Volumes": [
                    {"VolumeId": "vol-c1", "State": "available", "Attachments": []}
                ]
            },
        }
        silent = (
            "wait volume-available",
            "wait volume-in-use",
            "wait instance-running",
            "wait instance-terminated",
            "attach-volume",
            "terminate-instances",
            "delete-volume",
            "create-tags",
        )
        calls = temp / "aws-calls.txt"
        lines = [
            "$global:calls = [Collections.Generic.List[string]]::new()",
            "function global:aws {",
            "  $joined = $args -join ' '",
            "  $global:calls.Add($joined)",
            "  $global:LASTEXITCODE = 0",
            "  if ($joined -eq '--version') { 'aws-cli/2.test'; return }",
        ]
        for needle in silent:
            lines.append(f"  if ($joined.Contains('{needle}')) {{ return }}")
        for needle, payload in payloads.items():
            encoded = json.dumps(payload, separators=(",", ":")).replace("'", "''")
            lines.extend(
                [
                    f"  if ($joined.Contains('{needle}')) {{",
                    f"    '{encoded}'",
                    "    return",
                    "  }",
                ]
            )
        arguments = [
            f"-QualificationPath '{str(qualification).replace("'", "''")}'",
            f"-AllocationIdentity '{record['allocation_identity']}'",
            f"-FirstSeed {_CALIBRATION_FIRST_SEED}",
            f"-UnitCount {_CALIBRATION_UNITS}",
            "-MaxWorkers 16",
            "-FailSafeHours 4",
            "-AwsProfile fake",
            "-HourlyPriceUsd 1.0",
            "-CalibrationCostBudgetUsd 10.0",
            f"-ChargesPath '{str(charges_path).replace("'", "''")}'",
            f"-ArenaRevision '{arena_revision}'",
            f"-OutputRoot '{str(temp).replace("'", "''")}'",
        ]
        if preflight:
            arguments.append("-PreflightOnly")
        lines.extend(
            [
                "  $global:LASTEXITCODE = 51",
                '  throw "unexpected AWS call: $joined"',
                "}",
                f"$global:seedLedgerPath = '{str(ledger_path).replace("'", "''")}'",
                "function global:git {",
                "  $joined = $args -join ' '",
                "  $global:LASTEXITCODE = 0",
                "  if ($joined.Contains(' fetch ')) { return }",
                "  if ($joined.Contains(' show ')) { Get-Content -LiteralPath $global:seedLedgerPath; return }",
                "  $global:LASTEXITCODE = 52",
                '  throw "unexpected git call: $joined"',
                "}",
                '$global:launcherError = ""',
                "try {",
                (f"  & '{str(_LAUNCHER).replace("'", "''")}' " + " ".join(arguments)),
                "} catch {",
                "  $global:launcherError = [string]$_.Exception.Message",
                "}",
                f"$global:calls | Set-Content -LiteralPath '{str(calls).replace("'", "''")}'",
                'if ($global:launcherError -ne "") {',
                '  Write-Host "LAUNCHER ERROR: $global:launcherError"',
                "  exit 1",
                "}",
            ]
        )
        wrapper = temp / "launch.ps1"
        wrapper.write_text("\n".join(lines), encoding="utf-8")
        result = subprocess.run(
            [
                shutil.which("pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(wrapper),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=_ROOT,
        )
        aws_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""
        run_dirs = sorted(
            path
            for path in temp.iterdir()
            if path.is_dir() and "calibration-" in path.name
        )
        return result, aws_calls, (run_dirs[-1] if run_dirs else None)

    def _require_windows_pwsh(self):
        if os.name != "nt":
            self.skipTest("the PowerShell launcher uses the Windows operator path")
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")

    def test_preflight_admits_a_bounded_calibration_without_billable_mutation(self):
        self._require_windows_pwsh()
        result, aws_calls, run_dir = self._run_launcher(preflight=True)
        output = _normalize_console_output(result.stdout + result.stderr)
        self.assertEqual(0, result.returncode, output)
        self.assertIn("CALIBRATION PREFLIGHT ONLY", output)
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)
        admission = json.loads(
            (run_dir / "admission-calibration.json").read_text(encoding="utf-8")
        )
        self.assertEqual("GO", admission["decision"], admission["blocking_reasons"])
        self.assertEqual(0, admission["admission_phase"])
        self.assertFalse(admission["scientific_submission_authorized"])
        # Phase 0 authorizes billable creation only; phase 2 authorizes the
        # workload, so reading this record cannot bypass the second gate.
        self.assertTrue(admission["billable_resource_creation_authorized"])
        self.assertFalse(admission["workload_submission_authorized"])
        self.assertFalse(admission["runtime_prediction"]["available"])
        # The PLAN for a calibration carries no calibrated runtime range.
        plan = json.loads(
            (run_dir / "operational-plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual("LOW", plan["scientific_runtime_estimate"]["confidence"])

    def test_phase_two_no_go_submits_no_calibration_workload(self):
        # Issue #340: a Phase 2 failure after provisioning must submit no
        # workload and must proceed to bounded cleanup.
        self._require_windows_pwsh()
        result, aws_calls, run_dir = self._run_launcher(probe_outcome="FAIL")
        output = _normalize_console_output(result.stdout + result.stderr)
        self.assertNotEqual(0, result.returncode, output)
        self.assertIsNotNone(run_dir)
        admission = json.loads(
            (run_dir / "admission-calibration.json").read_text(encoding="utf-8")
        )
        self.assertEqual("GO", admission["decision"], output)
        phase_two = json.loads(
            (run_dir / "admission-phase-2.json").read_text(encoding="utf-8")
        )
        self.assertEqual("NO-GO", phase_two["decision"])
        self.assertFalse(phase_two["workload_submission_authorized"])
        self.assertFalse(phase_two["scientific_submission_authorized"])
        self.assertIn("Phase 2 calibration admission is NO-GO", output)
        self.assertIn("run-instances", aws_calls)
        self.assertFalse((run_dir / "ssm-run.json").exists())
        self.assertFalse((run_dir / "state.json").exists())
        self.assertNotIn("bootstrap-operational-calibration-340.sh", aws_calls)
        self.assertIn("terminate-instances", aws_calls)
        self.assertIn("delete-volume", aws_calls)

    def test_run_instances_itself_bounds_the_instance_independently(self):
        # Issue #340 blocker: losing the local launcher after `run-instances`
        # but before SSM is reachable must not leave an unbounded billable
        # instance, so the launch request carries its own boot-clock timer.
        self._require_windows_pwsh()
        _result, aws_calls, run_dir = self._run_launcher(probe_outcome="FAIL")
        self.assertIn("run-instances", aws_calls)
        request = json.loads(
            (run_dir / "run-instances.json").read_text(encoding="utf-8")
        )
        self.assertEqual("terminate", request["InstanceInitiatedShutdownBehavior"])
        user_data = base64.b64decode(request["UserData"]).decode("utf-8")
        self.assertIn("systemd-run", user_data)
        self.assertIn("--unit=lisjong-boot-failsafe", user_data)
        self.assertIn("/usr/bin/systemctl poweroff", user_data)
        seconds = int(re.search(r"--on-active=(\d+)s", user_data).group(1))
        admission = json.loads(
            (run_dir / "admission-calibration.json").read_text(encoding="utf-8")
        )
        budget = admission["budget"]
        expected = (
            float(budget["setup_seconds"])
            + float(budget["hard_fail_safe_seconds"])
            + float(budget["teardown_seconds"])
        )
        # The instance-side bound is exactly the window phase 0 priced.
        self.assertEqual(expected, float(seconds))
        self.assertEqual(
            expected,
            admission["cost_prediction"][
                "predicted_ec2_billable_runtime_range_seconds"
            ][1],
        )
        # Phase 2 records what was observed about both fail-safes.
        observation = json.loads(
            (run_dir / "phase-2-observation.json").read_text(encoding="utf-8")
        )
        self.assertIn("boot_fail_safe_armed", observation)


if __name__ == "__main__":
    unittest.main()
