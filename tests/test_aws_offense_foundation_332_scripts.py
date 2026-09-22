from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lisjong_arena import aws_operational_calibration, seed_registry
from lisjong_arena.offense_foundation import instrumentation

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _ROOT / "scripts" / "aws" / "start-offense-foundation-332.ps1"
_BOOTSTRAP = _ROOT / "scripts" / "aws" / "bootstrap-offense-foundation-332.sh"
_COLLECTOR = _ROOT / "scripts" / "aws" / "collect-offense-foundation-332.ps1"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _normalize_console_output(text):
    """Strip ANSI styling/line-wrap gutters so PowerShell error text is substring-searchable.

    PowerShell's terminal error renderer wraps long `throw` messages across
    lines with a `NNN | `/`| ` gutter prefix on each wrapped line; stripping
    that (after removing ANSI escapes) reconstructs the original message text.
    """
    plain = _ANSI_ESCAPE.sub("", text)
    plain = re.sub(r"(?m)^\s*(?:\d+\s*)?\|\s?", " ", plain)
    return " ".join(plain.split())


class AwsOffenseFoundation332ScriptTest(unittest.TestCase):
    def test_scripts_have_valid_shell_syntax(self):
        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            for script in (_LAUNCHER, _COLLECTOR):
                with self.subTest(script=script.name):
                    escaped = str(script).replace("'", "''")
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

    def test_phase_defaults_and_oversubscription_are_bounded(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"c7i.4xlarge"', text)
        self.assertIn('"c7i.8xlarge"', text)
        self.assertIn("{ 16 } else { 32 }", text)
        self.assertIn("MaxWorkers exceeds instance vCPU count", text)
        self.assertIn("-AllowWorkerOversubscription explicitly", text)
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("MAX_ALLOWED_WORKERS=16", bootstrap)
        self.assertIn("MAX_ALLOWED_WORKERS=32", bootstrap)
        self.assertIn('"$MAX_WORKERS" -gt "$VCPU"', bootstrap)
        self.assertIn("ALLOW_WORKER_OVERSUBSCRIPTION", bootstrap)
        self.assertIn("--allow-worker-oversubscription", text)

    def test_retained_volumes_are_distinct_encrypted_8_gib_gp3_resources(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"--size", "8"', launcher)
        self.assertIn('"--volume-type", "gp3"', launcher)
        self.assertIn('"--encrypted"', launcher)
        self.assertIn('"/dev/sdf"', launcher)
        self.assertIn('"/dev/sdg"', launcher)
        self.assertIn("requiredAvailabilityZone", launcher)
        self.assertNotIn(
            '"ec2", "create-tags", "--resources", $PhaseAArtifactVolumeId',
            launcher,
        )
        collector = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("[int]$retained.Size -ne 8", collector)
        self.assertIn("retained_storage_billing_continues = $true", collector)
        self.assertIn("lisjong-phase-a-input-volume", collector)
        self.assertIn("lisjong-phase-a-input-run", collector)
        # Blocker 3: the retained Phase A input volume is never retagged to
        # the Phase B run-id anywhere in the collector.
        self.assertNotIn(
            '"ec2", "create-tags", "--resources", $phaseAInputVolumeId',
            collector,
        )

    def test_phase_b_gate_precedes_billable_mutation(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        gate = "Phase B gate requires completed Phase A strict-read PASS"
        self.assertIn(gate, text)
        self.assertLess(text.index(gate), text.index('"ec2", "create-volume"'))
        self.assertLess(text.index(gate), text.index('"ec2", "run-instances"'))
        self.assertIn('"lisjong-p2-outcome") -ne "OFFENSE SUPPORT QUALIFIED"', text)
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        phase_b = bootstrap.index('PHASE_A_ROOT="$INPUT_MOUNT/issue-332/phase-A"')
        first_readback = bootstrap.index("offense_foundation readback", phase_b)
        scientific_lock = bootstrap.index('--p2-corpus "$P2_CORPUS"')
        self.assertLess(first_readback, scientific_lock)
        self.assertIn('mount -o ro,noload "$INPUT_DEVICE" "$INPUT_MOUNT"', bootstrap)

    def test_phase_a_requires_final_qualification_before_billable_execution(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[string]$QualificationPath", text)
        required = "Phase A requires the final merged-main P0/P1 qualification artifact"
        self.assertIn(required, text)
        lock = "Final merged-main qualification/protocol lock validation failed"
        self.assertIn(lock, text)
        self.assertLess(text.index(required), text.index('"ec2", "create-volume"'))
        self.assertLess(text.index(lock), text.index('"ec2", "create-volume"'))
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("LOCAL_QUALIFICATION_IDENTITY", bootstrap)
        self.assertIn("EXPECTED_QUALIFICATION_CONTRACT_B64", bootstrap)

    def test_qualification_contract_replaces_full_identity_equality_across_platforms(
        self,
    ):
        # Blocker 1: full cross-platform qualification identity equality
        # (which also binds platform-dependent representation such as exact
        # Python patch version and imported-source byte digest) must not be
        # required. Only the #332 scientific/runtime contract is compared,
        # and only after the local pre-billing qualification already passed.
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertNotIn("EXPECTED_QUALIFICATION_IDENTITY", bootstrap)
        self.assertNotIn("remote qualification differs from the pre-billing", bootstrap)
        self.assertIn("offense_foundation qualification-contract", bootstrap)
        self.assertIn(
            "offense_foundation require-qualification-contract-match", bootstrap
        )
        # The AWS-generated qualification is the one actually embedded in the
        # Phase A protocol lock/corpus, not the local pre-billing artifact.
        contract_match = bootstrap.index("require-qualification-contract-match")
        lock_call = bootstrap.index("offense_foundation lock", contract_match)
        self.assertLess(contract_match, lock_call)
        self.assertIn('--qualification "$QUALIFICATION"', bootstrap)
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("qualification-contract", launcher)
        self.assertIn("local-qualification-contract.json", launcher)
        self.assertIn("--local-qualification-identity", launcher)
        self.assertIn("--expected-qualification-contract-b64", launcher)
        self.assertNotIn("--expected-qualification-identity", launcher)

    def test_seed_registry_authority_is_decoupled_from_arena_code_revision(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('$seedRegistryBranch = "seed-registry"', launcher)
        self.assertNotIn("[string]$SeedLedgerPath", launcher)
        self.assertNotIn("[string]$SeedRegistryBranch", launcher)
        self.assertIn("git -C $repoRoot fetch --no-tags origin $fetchSpec", launcher)
        self.assertIn("--seed-ledger $resolvedSeedLedgerPath", launcher)
        self.assertIn("--seed-ledger-json-b64", launcher)
        self.assertIn("--seed-ledger-json-b64)", bootstrap)
        self.assertIn('--seed-ledger "$INPUT_ROOT/seed-ledger.json"', bootstrap)
        self.assertIn("lisjong_arena.seed_registry", bootstrap)
        self.assertLess(
            launcher.index("Canonical seed ledger validation failed"),
            launcher.index('"ec2", "create-volume"'),
        )

    def test_phase_b_arena_revision_bound_before_billable_mutation(self):
        # Blocker 2: Phase B must bind to the exact Phase A Arena revision
        # before any billable AWS resource is created.
        text = _LAUNCHER.read_text(encoding="utf-8")
        gate = "Phase B Arena revision"
        self.assertIn(gate, text)
        must_match = "must exactly match the Phase A retained Arena revision"
        self.assertIn(must_match, text)
        self.assertLess(text.index(must_match), text.index('"ec2", "create-volume"'))
        self.assertLess(text.index(must_match), text.index('"ec2", "run-instances"'))
        missing_tag = "Phase A retained volume is missing its Arena revision tag"
        self.assertIn(missing_tag, text)
        self.assertLess(text.index(missing_tag), text.index('"ec2", "create-volume"'))
        collector = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn(
            "lisjong-arena-revision,Value=$($summary.arena_revision)", collector
        )
        self.assertIn(
            "lisjong-remote-qualification-identity,Value=$($summary.remote_qualification_identity)",
            collector,
        )
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("remote_qualification_identity", bootstrap)

    def test_phase_b_teardown_verifies_phase_a_input_volume(self):
        # Blocker 3: Phase B teardown must explicitly verify the retained
        # Phase A input volume, never delete it, and never retag it.
        collector = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("phaseAInputVerification", collector)
        for needle in (
            "Phase A input volume failed post-teardown detachment",
            "Phase A input volume provenance tags differ",
            "Phase A input volume must not carry the Phase B run id",
            "retagged_to_phase_b_run_id = $false",
            "retained_billing_continues = $true",
        ):
            self.assertIn(needle, collector)
        self.assertIn("phase_a_input_volume = $phaseAInputVerification", collector)
        self.assertNotIn("delete-volume", collector)
        # No code path retags the Phase A input volume to the Phase B run-id.
        self.assertNotIn(
            '"ec2", "create-tags", "--resources", $phaseAInputVolumeId',
            collector,
        )

    def test_bootstrap_reuses_canonical_generator_and_exact_runtime_contract(self):
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("lisjong_arena.environment_verify", text)
        self.assertIn("lisjong_arena.offense_foundation qualify", text)
        self.assertIn("lisjong_arena.offense_foundation lock", text)
        self.assertIn("lisjong_arena.offense_foundation generate", text)
        self.assertIn("lisjong_arena.offense_foundation readback", text)
        self.assertIn("lisjong_arena.offense_foundation source-readback", text)
        self.assertIn('--source-record-output "$SOURCE_RECORD"', text)
        self.assertIn('--workers "$MAX_WORKERS"', text)
        self.assertIn('--operational-progress-path "$PROGRESS_PATH"', text)
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        for value in (
            'requires-python = ">=3.14,<3.15"',
            "15799e5f0fe47f2e2b2c39060de804d99c51492d",
            "8735e89e1aea000ab59368d0368d476787827741",
            "riichienv==0.4.10",
        ):
            self.assertIn(value, launcher)
        self.assertNotIn("0.4.8", launcher + text)

    def test_progress_and_completion_do_not_publish_partial_scientific_counts(self):
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("aws_execution_observability", launcher)
        self.assertIn("lisjong-progress-path", launcher)
        self.assertIn("lisjong-failsafe-deadline", launcher)
        self.assertNotIn("support[", bootstrap)
        self.assertNotIn('manifest["support"]', bootstrap)
        self.assertNotIn("qualification likelihood", bootstrap.lower())
        collector = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("OFFENSE SUPPORT NOT QUALIFIED", collector)
        self.assertIn("Phase B remains forbidden", collector)

    def test_reattachment_and_monitor_detach_never_resubmit(self):
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        collector = _COLLECTOR.read_text(encoding="utf-8")
        self.assertLess(
            launcher.index("if (-not [string]::IsNullOrWhiteSpace($ReattachRunId))"),
            launcher.index("function Send-SsmCommand"),
        )
        self.assertNotIn('"ssm", "send-command"', collector)
        self.assertIn(
            "No resubmission, termination, or teardown action was taken", collector
        )
        self.assertIn("if ($monitorDetached) { throw }", launcher)
        self.assertIn("it was not terminated or resubmitted", launcher)

    def test_billable_admission_is_never_an_operator_assertion(self):
        # Issue #340: the launcher no longer accepts an asserted runtime range;
        # the calibrated range is produced by the Phase 1 admission record.
        text = _LAUNCHER.read_text(encoding="utf-8")
        for removed in (
            "PredictedScientificRuntimeMinHours",
            "PredictedScientificRuntimeMaxHours",
            "ScientificRuntimeEstimateBasis",
            "PredictedBillableRuntimeMinHours",
            "PredictedBillableRuntimeMaxHours",
            "operator-supplied matching calibration",
        ):
            self.assertNotIn(removed, text)
        self.assertIn("$CalibrationEvidencePath", text)
        self.assertIn("aws_operational_calibration", text)
        self.assertIn("admit-phase-1", text)
        self.assertIn("admit-phase-2", text)
        self.assertIn("$phaseOneAdmission.runtime_prediction", text)
        self.assertIn("$phaseOneAdmission.cost_prediction", text)

    def test_admission_gates_precede_the_actions_they_guard(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        phase_one = text.index("admit-phase-1")
        self.assertLess(phase_one, text.index('"ec2", "create-volume"'))
        self.assertLess(phase_one, text.index('"ec2", "run-instances"'))
        no_go = "Phase 1 launch admission is NO-GO"
        self.assertIn(no_go, text)
        self.assertLess(text.index(no_go), text.index('"ec2", "create-volume"'))
        phase_two = text.index("admit-phase-2")
        self.assertLess(phase_two, text.index("bootstrap-offense-foundation-332.sh"))
        self.assertLess(
            phase_two, text.index('$longRequestPath = Join-Path $runDir "ssm-run.json"')
        )
        self.assertLess(phase_two, text.index("$scientificSubmitted = $true"))
        self.assertIn("Phase 2 launch admission is NO-GO", text)
        self.assertIn("LISJONG_332_PHASE2_PROBE=PASS", text)
        self.assertIn("recovery-identity.json", text)

    def test_the_pre_arm_interval_is_bounded_from_launch(self):
        # Issue #340 blocker: the SSM fail-safe is armed only after the
        # instance is already billable, so the launch request carries its own
        # instance-side timer covering the whole planned window.
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("UserData = $bootFailSafeUserData", text)
        self.assertIn("boot-fail-safe-user-data --window-seconds", text)
        self.assertNotIn("--on-active=${bootFailSafeSeconds}s", text)
        self.assertIn("boot_fail_safe_deadline_epoch = ", text)
        self.assertIn(
            "$bootFailSafeSeconds = [long]($setupSeconds + $hardFailSafeSeconds "
            "+ $teardownSeconds)",
            text,
        )
        self.assertLess(
            text.index("$bootFailSafeUserData = "),
            text.index('"ec2", "create-volume"'),
        )
        self.assertIn("boot_fail_safe_armed = $bootFailSafeArmed", text)
        self.assertIn("LISJONG_BOOT_FAILSAFE", text)

    def test_unpriced_material_charges_default_to_fail_closed(self):
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("an unpriced material charge is never treated as zero", text)
        self.assertIn('label = "public IPv4 address hours"; material = $true', text)
        self.assertIn('label = "data transfer out"; material = $true', text)

    def _launcher_environment(self, arena_revision, phase_a_arena_revision_tag):
        populations = {
            "TRAIN": list(range(100, 200)),
            "SELECT": list(range(200, 220)),
            "OFFLINE-EVAL": list(range(220, 240)),
        }
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, True)
        root = Path(temp)
        ledger_path = root / "seed-ledger.json"
        ledger = seed_registry.new_ledger()
        identities = {}
        for split, seeds in populations.items():
            ledger, record = seed_registry.reserve_allocation(
                ledger,
                owner_issue="lisbun/lisjong-arena#332",
                protocol="offense-foundation-v1",
                seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
                purpose="synthetic AWS preflight allocation",
                population="offense-foundation",
                split=split,
                seeds=seeds,
                arena_revision=arena_revision,
                protocol_revision="synthetic-test-v1",
                provenance_reference="synthetic test fixture",
                allocation_timestamp="2026-09-22T00:00:00Z",
            )
            identities[split] = record["allocation_identity"]
        ledger, calibration_record = seed_registry.reserve_allocation(
            ledger,
            owner_issue="lisbun/lisjong-arena#340",
            protocol=aws_operational_calibration.CALIBRATION_PROTOCOL,
            seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            purpose="synthetic operational calibration population",
            population=aws_operational_calibration.CALIBRATION_POPULATION,
            split=None,
            seeds=list(range(906_000, 906_064)),
            arena_revision=arena_revision,
            protocol_revision="synthetic-test-v1",
            provenance_reference="synthetic test fixture",
            allocation_timestamp="2026-09-22T00:00:00Z",
        )
        seed_registry.write_ledger(ledger_path, ledger)
        calibration_binding = seed_registry.allocation_binding(
            ledger, calibration_record["allocation_identity"]
        )
        request = {
            "phase": "SCIENTIFIC",
            "populations": populations,
            "allocation_bindings": {
                split: seed_registry.allocation_binding(ledger, identity)
                for split, identity in identities.items()
            },
        }
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        phase_a_tags = [
            {"Key": "Issue", "Value": "332"},
            {"Key": "Purpose", "Value": "offense-foundation-output"},
            {"Key": "lisjong-phase", "Value": "A"},
            {"Key": "lisjong-phase-complete", "Value": "true"},
            {"Key": "lisjong-strict-readback", "Value": "PASS"},
            {"Key": "lisjong-p2-outcome", "Value": "OFFENSE SUPPORT QUALIFIED"},
            {"Key": "lisjong-run-id", "Value": "phase-a-run"},
            {"Key": "lisjong-source-record-identity", "Value": "c" * 64},
        ]
        if phase_a_arena_revision_tag is not None:
            phase_a_tags.append(
                {"Key": "lisjong-arena-revision", "Value": phase_a_arena_revision_tag}
            )
        return root, ledger_path, request_path, phase_a_tags, calibration_binding

    def _run_phase_b_launcher(
        self,
        arena_revision,
        *,
        phase_a_arena_revision_tag=None,
        preflight=True,
        extra_arguments=(),
        probe_outcome="PASS",
        environment=None,
    ):
        """Run the real PowerShell Phase B launcher against a mocked `aws` CLI.

        ``preflight=False`` exercises the billable path; the mock records every
        call so a test can assert that no billable mutation or scientific
        submission happened.
        """
        if environment is None:
            (
                root,
                ledger_path,
                request_path,
                phase_a_tags,
                _calibration_binding,
            ) = self._launcher_environment(arena_revision, phase_a_arena_revision_tag)
        else:
            root, ledger_path, request_path, phase_a_tags = environment
        wrapper = root / "launch.ps1"
        calls = root / "aws-calls.txt"
        arm_epoch = 1_790_000_000
        stdout = (
            f"LISJONG_FAILSAFE_DEADLINE_EPOCH={arm_epoch + 12 * 3600}\\n"
            f"LISJONG_332_PHASE2_OBSERVED_EPOCH={arm_epoch + 600}\\n"
            f"LISJONG_332_PHASE2_PROBE={probe_outcome}\\n"
        )
        payloads = {
            "get-caller-identity": {"Account": "1"},
            "describe-instance-types": {
                "InstanceTypes": [
                    {
                        "VCpuInfo": {"DefaultVCpus": 32},
                        "MemoryInfo": {"SizeInMiB": 65536},
                    }
                ]
            },
            "describe-volumes": {
                "Volumes": [
                    {
                        "VolumeId": "vol-a1",
                        "State": "available",
                        "VolumeType": "gp3",
                        "Size": 8,
                        "Encrypted": True,
                        "Attachments": [],
                        "AvailabilityZone": "ap-northeast-1a",
                        "Tags": phase_a_tags,
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
            "create-volume": {"VolumeId": "vol-b1"},
            "run-instances": {
                "Instances": [
                    {
                        "InstanceId": "i-0123456789abcdef0",
                        "LaunchTime": "2026-09-21T00:00:00Z",
                    }
                ]
            },
            "describe-instance-attribute": {
                "InstanceInitiatedShutdownBehavior": {"Value": "terminate"}
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
                                "BlockDeviceMappings": [
                                    {
                                        "Ebs": {
                                            "DeleteOnTermination": True,
                                            "VolumeId": "vol-root",
                                        }
                                    }
                                ],
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
        lines = [
            "$global:calls = [Collections.Generic.List[string]]::new()",
            "function global:aws {",
            "  $joined = $args -join ' '",
            "  $global:calls.Add($joined)",
            "  $global:LASTEXITCODE = 0",
            "  if ($joined -eq '--version') { 'aws-cli/2.test'; return }",
        ]
        for needle in silent:
            lines.extend([f"  if ($joined.Contains('{needle}')) {{ return }}"])
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
        launcher_arguments = [
            "-Phase B",
            f"-RequestPath '{str(request_path).replace("'", "''")}'",
            "-PhaseAArtifactVolumeId 'vol-a1'",
            "-AwsProfile fake",
            "-HourlyPriceUsd 1.0",
            f"-ArenaRevision '{arena_revision}'",
            f"-OutputRoot '{str(root).replace("'", "''")}'",
        ]
        if preflight:
            launcher_arguments.append("-PreflightOnly")
        launcher_arguments.extend(extra_arguments)
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
                (
                    f"  & '{str(_LAUNCHER).replace("'", "''")}' "
                    + " ".join(launcher_arguments)
                ),
                "} catch {",
                "  $global:launcherError = [string]$_.Exception.Message",
                "}",
                # The recorded call list must survive a thrown launcher, so a
                # test can prove which AWS mutations did not happen.
                f"$global:calls | Set-Content -LiteralPath '{str(calls).replace("'", "''")}'",
                'if ($global:launcherError -ne "") {',
                '  Write-Host "LAUNCHER ERROR: $global:launcherError"',
                "  exit 1",
                "}",
            ]
        )
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
            path for path in root.iterdir() if path.is_dir() and "phase-B-" in path.name
        )
        return result, aws_calls, (run_dirs[-1] if run_dirs else None)

    def _run_phase_b_preflight(self, arena_revision, phase_a_arena_revision_tag=None):
        result, aws_calls, _run_dir = self._run_phase_b_launcher(
            arena_revision, phase_a_arena_revision_tag=phase_a_arena_revision_tag
        )
        return result, aws_calls

    def _write_phase_b_admission_inputs(
        self, directory, arena_revision, calibration_binding
    ):
        """Write a matching Phase B calibration plus a fully priced charge list."""
        project = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        lisjong = re.search(r"lisjong\.git@([0-9a-f]{40})", project).group(1)
        engine = re.search(r"lisjong-engine\.git@([0-9a-f]{40})", project).group(1)
        riichienv = re.search(r"riichienv==([0-9][0-9A-Za-z.\-]*)", project).group(1)
        start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
        tasks = []
        for index in range(64):
            began = start + timedelta(seconds=(index // 32) * 60)
            tasks.append(
                {
                    "seed": 906_000 + index,
                    "started_at": began.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    ),
                    "completed_at": (began + timedelta(seconds=60))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                }
            )
        evidence = aws_operational_calibration.build_calibration_evidence(
            calibration_run_id="synthetic-phase-b-calibration",
            calibrated_at=start + timedelta(minutes=5),
            seed_allocation=calibration_binding,
            seed_allocation_population=aws_operational_calibration.CALIBRATION_POPULATION,
            seed_ledger_revision=calibration_binding["ledger_revision"],
            arena_revision=arena_revision,
            lisjong_revision=lisjong,
            lisjong_engine_revision=engine,
            riichienv_version=riichienv,
            workload_identity="offense-foundation-332-phase-b-scientific",
            teacher_identity="lisjong.policies.TwoStepUkeirePolicy x4",
            game_mode="4p-red-half",
            instance_type="c7i.8xlarge",
            vcpu=32,
            worker_count_requested=32,
            workers_active_observed=32,
            tasks=tasks,
            batch_scientific_wall_clock_seconds=125.0,
            ec2_billable_runtime_seconds=2400.0,
            setup_overhead_seconds=900.0,
            teardown_overhead_seconds=400.0,
            durable_evidence_level=instrumentation.CALIBRATION_DURABLE_EVIDENCE_LEVEL,
            instrumentation_identity=instrumentation.GENERATION_INSTRUMENTATION_IDENTITY,
            instrumentation_path="issue-332/phase-B/operational/progress.json",
        )
        calibration_path = directory / "calibration.json"
        aws_operational_calibration.write_calibration_evidence(
            calibration_path, evidence
        )
        charges_path = directory / "charges.json"
        charges_path.write_text(
            json.dumps(
                [
                    {
                        "label": "retained encrypted 8 GiB gp3 output artifact volume",
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
                        "reason": "artifacts stay on the retained EBS volume",
                        "usd": None,
                    },
                ]
            ),
            encoding="utf-8",
        )
        return calibration_path, charges_path

    def test_preflight_does_not_report_success_on_a_no_go_admission(self):
        # Issue #340 blocker: preflight is a mechanical Go/No-Go gate, so a
        # NO-GO decision must not exit successfully even though preflight
        # creates no billable resource.
        if os.name != "nt":
            self.skipTest(
                "PowerShell launcher preflight uses the Windows operator path"
            )
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        arena_revision = "a" * 40
        result, aws_calls, run_dir = self._run_phase_b_launcher(
            arena_revision, phase_a_arena_revision_tag=arena_revision
        )
        output = _normalize_console_output(result.stdout + result.stderr)
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("Phase 1 launch admission is NO-GO", output)
        self.assertIn(
            "No create-volume, run-instances, or scientific SSM submission", output
        )
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)
        admission = json.loads(
            (run_dir / "admission-phase-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual("NO-GO", admission["decision"])
        self.assertFalse(admission["billable_resource_creation_authorized"])

    def test_phase_b_preflight_rejects_arena_revision_mismatch(self):
        # Blocker 2: a Phase B revision that differs from the Phase A
        # retained tag must fail before any billable resource is created.
        if os.name != "nt":
            self.skipTest(
                "PowerShell launcher preflight uses the Windows operator path"
            )
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        result, aws_calls = self._run_phase_b_preflight(
            "a" * 40, phase_a_arena_revision_tag="b" * 40
        )
        output = result.stdout + result.stderr
        normalized = _normalize_console_output(output)
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn(
            "must exactly match the Phase A retained Arena revision", normalized
        )
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)

    def test_phase_b_preflight_rejects_missing_phase_a_arena_revision_tag(self):
        # Blocker 2: a Phase A retained volume without the Arena revision tag
        # must fail before any billable resource is created.
        if os.name != "nt":
            self.skipTest(
                "PowerShell launcher preflight uses the Windows operator path"
            )
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        result, aws_calls = self._run_phase_b_preflight(
            "a" * 40, phase_a_arena_revision_tag=None
        )
        output = result.stdout + result.stderr
        normalized = _normalize_console_output(output)
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("missing its Arena revision tag", normalized)
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)

    def test_phase_one_no_go_creates_no_billable_resource(self):
        # Issue #340: without a matching calibration the launcher must reach
        # NO-GO and stop before create-volume / run-instances.
        if os.name != "nt":
            self.skipTest("PowerShell launcher uses the Windows operator path")
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        arena_revision = "a" * 40
        result, aws_calls, run_dir = self._run_phase_b_launcher(
            arena_revision,
            phase_a_arena_revision_tag=arena_revision,
            preflight=False,
        )
        output = _normalize_console_output(result.stdout + result.stderr)
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("Phase 1 launch admission is NO-GO", output)
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)
        self.assertIsNotNone(run_dir)
        admission = json.loads(
            (run_dir / "admission-phase-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual("NO-GO", admission["decision"])
        self.assertFalse(admission["billable_resource_creation_authorized"])
        self.assertFalse(admission["scientific_submission_authorized"])
        self.assertFalse((run_dir / "ssm-run.json").exists())
        self.assertFalse((run_dir / "state.json").exists())

    def test_atomic_progress_is_sufficient_for_production_preflight(self):
        # Issue #351: calibration keeps stronger per-seed receipts, while the
        # production generator may be admitted with atomic operational progress.
        if os.name != "nt":
            self.skipTest("PowerShell launcher uses the Windows operator path")
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        arena_revision = "a" * 40
        inputs_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, inputs_root, True)
        (
            root,
            ledger_path,
            request_path,
            phase_a_tags,
            calibration_binding,
        ) = self._launcher_environment(arena_revision, arena_revision)
        calibration_path, charges_path = self._write_phase_b_admission_inputs(
            inputs_root, arena_revision, calibration_binding
        )
        result, aws_calls, run_dir = self._run_phase_b_launcher(
            arena_revision,
            phase_a_arena_revision_tag=arena_revision,
            environment=(root, ledger_path, request_path, phase_a_tags),
            extra_arguments=[
                f"-CalibrationEvidencePath '{str(calibration_path).replace("'", "''")}'",
                f"-ChargesPath '{str(charges_path).replace("'", "''")}'",
                "-CostBudgetUsd 20.0",
            ],
        )
        output = _normalize_console_output(result.stdout + result.stderr)
        self.assertEqual(0, result.returncode, output)
        admission = json.loads(
            (run_dir / "admission-phase-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual("GO", admission["decision"], admission["blocking_reasons"])
        self.assertEqual([], admission["blocking_reasons"])
        self.assertTrue(admission["billable_resource_creation_authorized"])
        self.assertFalse(admission["scientific_submission_authorized"])
        self.assertEqual(
            instrumentation.PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL,
            admission["target"]["durable_evidence_level"],
        )
        self.assertEqual(
            "atomic-operational-progress",
            admission["target"]["durable_evidence_level"],
        )
        self.assertEqual(
            instrumentation.GENERATION_INSTRUMENTATION_IDENTITY,
            admission["target"]["instrumentation_identity"],
        )
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)



if __name__ == "__main__":
    unittest.main()
