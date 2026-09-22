from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lisjong_arena import seed_registry

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

    def _run_phase_b_preflight(self, arena_revision, phase_a_arena_revision_tag=None):
        """Run the real PowerShell Phase B preflight against a mocked `aws` CLI.

        ``phase_a_arena_revision_tag`` is the ``lisjong-arena-revision`` tag
        value on the mocked retained Phase A volume; omit it to simulate a
        volume with no such tag at all (Blocker 2 missing-tag case).
        """
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
        seed_registry.write_ledger(ledger_path, ledger)
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
        wrapper = root / "preflight.ps1"
        calls = root / "aws-calls.txt"
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
        }
        lines = [
            "$global:calls = [Collections.Generic.List[string]]::new()",
            "function global:aws {",
            "  $joined = $args -join ' '",
            "  $global:calls.Add($joined)",
            "  $global:LASTEXITCODE = 0",
            "  if ($joined -eq '--version') { 'aws-cli/2.test'; return }",
        ]
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
                (
                    f"& '{str(_LAUNCHER).replace("'", "''")}' -Phase B "
                    f"-RequestPath '{str(request_path).replace("'", "''")}' "
                    "-PhaseAArtifactVolumeId 'vol-a1' "
                    "-AwsProfile fake -PreflightOnly -HourlyPriceUsd 1.0 "
                    f"-ArenaRevision '{arena_revision}' "
                    f"-OutputRoot '{str(root).replace("'", "''")}'"
                ),
                f"$global:calls | Set-Content -LiteralPath '{str(calls).replace("'", "''")}'",
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
        return result, aws_calls

    def test_preflight_executes_without_billable_aws_mutation(self):
        if os.name != "nt":
            self.skipTest(
                "PowerShell launcher preflight uses the Windows operator path"
            )
        if shutil.which("pwsh") is None:
            self.skipTest("pwsh is unavailable")
        arena_revision = "a" * 40
        result, aws_calls = self._run_phase_b_preflight(
            arena_revision, phase_a_arena_revision_tag=arena_revision
        )
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertIn("PREFLIGHT ONLY", output)
        self.assertNotIn("create-volume", aws_calls)
        self.assertNotIn("run-instances", aws_calls)
        self.assertNotIn("send-command", aws_calls)

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


if __name__ == "__main__":
    unittest.main()
