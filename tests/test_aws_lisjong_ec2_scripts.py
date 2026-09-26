"""#379 generic EC2 lifecycle wrapper tests (no real AWS call).

Static contract checks plus a Preflight run of the wrapper against a stub
``aws`` executable that answers the read-only / dry-run calls.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_AWS = _ROOT / "scripts" / "aws"
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

    def test_checkout_keeps_lf_on_windows(self) -> None:
        git = shutil.which("git")
        if git is None or not (_ROOT / ".git").exists():
            self.skipTest("git checkout is unavailable")
        attributes = subprocess.run(
            [git, "-C", str(_ROOT), "check-attr", "eol", "--", str(_RUNNER)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertTrue(attributes.strip().endswith("eol: lf"), attributes)
        self.assertNotIn(b"\r", _RUNNER.read_bytes())

    def test_logs_and_progress_sync_periodically_not_only_on_exit(self) -> None:
        self.assertIn('sleep "$SYNC_SECONDS" &', self.text)
        self.assertIn('wait "$SLEEP_PID"\n        sync_logs', self.text)
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


# Fakes for running the real runner outside EC2: `aws s3 cp` maps s3:// to a local
# directory (FAIL_RECURSIVE / FAIL_KEY make uploads fail), `id -u` reports root and
# `systemd-run` only records its arguments.
_FAKE_AWS = """#!/usr/bin/env bash
recursive=0; excludes=(); positional=()
while (($#)); do
    case "$1" in
        --only-show-errors) shift ;;
        --recursive) recursive=1; shift ;;
        --region) shift 2 ;;
        --exclude) excludes+=("$2"); shift 2 ;;
        *) positional+=("$1"); shift ;;
    esac
done
src="${positional[2]}"; dst="${positional[3]}"
if [[ "$dst" == s3://* ]]; then
    if ((recursive)) && [[ -n "${FAIL_RECURSIVE:-}" ]]; then exit 1; fi
    if [[ -n "${FAIL_KEY:-}" && "$dst" == *"$FAIL_KEY" ]]; then exit 1; fi
fi
map() { if [[ "$1" == s3://* ]]; then echo "$FAKE_S3/${1#s3://}"; else echo "$1"; fi; }
from="$(map "$src")"; to="$(map "$dst")"
if ((recursive)); then
    (cd "$from" && find . -type f) | while IFS= read -r name; do
        name="${name#./}"; skip=0
        for pattern in "${excludes[@]}"; do [[ "$name" == $pattern ]] && skip=1; done
        if ((skip == 0)); then mkdir -p "$(dirname "$to/$name")"; cp "$from/$name" "$to/$name"; fi
    done
else
    mkdir -p "$(dirname "$to")"; cp "$from" "$to"
fi
"""
_FAKE_ID = '#!/usr/bin/env bash\nif [[ "$1" == "-u" ]]; then echo 0; else exec /usr/bin/id "$@"; fi\n'
_FAKE_SYSTEMD_RUN = '#!/usr/bin/env bash\necho "$*" >>"$SYSTEMD_RUN_LOG"\n'
_RUNNER_WORK_ROOT = 'WORK_ROOT="/mnt/lisjong-ec2"'


class RunnerAutoTerminateTest(unittest.TestCase):
    """Runs the real runner with its work root moved to a temp dir (#396)."""

    RUN_ID = "smoke-20260926T120000Z-4e3061d2"
    BUCKET = "lisjong-ec2-123456789012-4e3061d2"

    def setUp(self) -> None:
        self.bash = shutil.which("bash")
        if self.bash is None or "system32" in self.bash.lower():
            self.skipTest("a POSIX bash is unavailable")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = self.tmp.as_posix()
        text = _RUNNER.read_text(encoding="utf-8")
        self.assertIn(_RUNNER_WORK_ROOT, text)
        self.runner = self.tmp / "runner.sh"
        self.runner.write_bytes(
            text.replace(_RUNNER_WORK_ROOT, f'WORK_ROOT="{root}/work"').encode()
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("aws", _FAKE_AWS),
            ("id", _FAKE_ID),
            ("systemd-run", _FAKE_SYSTEMD_RUN),
        ):
            (bin_dir / name).write_bytes(body.encode())
            (bin_dir / name).chmod(0o755)
        bootstrap = (
            b'echo ok >"$LISJONG_OUTPUT_DIR/result.txt"\nexit "${BOOT_EXIT:-0}"\n'
        )
        inputs = self.tmp / "s3" / self.BUCKET / self.RUN_ID / "input"
        inputs.mkdir(parents=True)
        (inputs / "boot.sh").write_bytes(bootstrap)
        digest = hashlib.sha256(bootstrap).hexdigest()
        (inputs / "manifest.sha256").write_bytes(f"{digest}  boot.sh\n".encode())
        self.output = self.tmp / "s3" / self.BUCKET / self.RUN_ID / "output"
        self.systemd_log = self.tmp / "systemd-run.log"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
            "FAKE_S3": (self.tmp / "s3").as_posix(),
            "SYSTEMD_RUN_LOG": self.systemd_log.as_posix(),
        }

    def _run(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, self.runner.as_posix(), "--run-id", self.RUN_ID,
             "--bucket", self.BUCKET, "--region", "ap-northeast-1", "--workers", "1",
             "--bootstrap", "boot.sh", "--args-b64", ""],
            env={**self.env, **env}, capture_output=True, text=True, timeout=120,
            check=False,
        )  # fmt: skip

    def _poweroff_scheduled(self) -> bool:
        return self.systemd_log.exists() and "poweroff" in self.systemd_log.read_text()

    def test_success_with_complete_upload_schedules_a_delayed_poweroff(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output / "_completion.json").is_file())
        self.assertTrue((self.output / "result.txt").is_file())
        call = self.systemd_log.read_text().split()
        self.assertIn("--on-active=60s", call)
        self.assertIn("--unit=lisjong-auto-terminate", call)
        self.assertEqual(call[-2:], ["/usr/bin/systemctl", "poweroff"])
        self.assertIn("LISJONG_EC2_AUTO_TERMINATE scheduled in 60s", result.stdout)

    def test_failed_workload_keeps_the_instance(self) -> None:
        result = self._run(BOOT_EXIT="3")
        self.assertEqual(result.returncode, 3)
        self.assertTrue((self.output / "_completion.json").is_file())
        self.assertFalse(self._poweroff_scheduled())

    def test_failed_output_upload_keeps_the_instance(self) -> None:
        result = self._run(FAIL_RECURSIVE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self._poweroff_scheduled())
        self.assertIn("LISJONG_EC2_AUTO_TERMINATE skipped", result.stdout)

    def test_failed_completion_upload_keeps_the_instance(self) -> None:
        result = self._run(FAIL_KEY="/output/_completion.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.output / "_completion.json").exists())
        self.assertFalse(self._poweroff_scheduled())


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


# Read-only answers keyed by "service operation"; dry-runs and the free bucket
# name answer the way the AWS CLI does (error text, exit code 254).
_STUB_AWS = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    with open({log!r}, "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    while args and args[0] in ("--profile", "--region"):
        args = args[2:]
    key = " ".join(args[:2])

    def fail(text):
        sys.stderr.write(text + "\\n")
        sys.exit(254)

    if "--dry-run" in args:
        fail("An error occurred (DryRunOperation) when calling the operation: "
             "Request would have succeeded, but DryRun flag is set.")
    if key == "s3api create-bucket":
        fail("stub aws: first billable call reached")
    if key == "ec2 describe-security-groups" and any("tag:" in a for a in args):
        print(json.dumps({{"SecurityGroups": []}}))
        sys.exit(0)
    # A launched run (Collect tests) lives in the state dir: instances.json and the
    # runner's output/ files; the fake bucket keys are "<run-id>/output/<name>".
    state = {state!r}
    instances = os.path.join(state, "instances.json")
    if key == "ec2 describe-instances" and os.path.exists(instances):
        print(open(instances, encoding="utf-8").read())
        sys.exit(0)
    if key == "ssm get-command-invocation" and os.path.exists(instances):
        print(json.dumps({{"Status": "Success", "ResponseCode": 0}}))
        sys.exit(0)
    if key == "s3api list-objects-v2":
        prefix = args[args.index("--prefix") + 1]
        root = os.path.join(state, "output")
        keys = [prefix + os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                for d, _, files in os.walk(root) for f in files]
        print(json.dumps({{"Contents": [{{"Key": k}} for k in sorted(keys)]}}))
        sys.exit(0)
    if key == "s3 cp" and args[3].startswith("s3://"):
        name = args[3].split("/output/", 1)[1]
        with open(os.path.join(state, "output", name), "rb") as source:
            data = source.read()
        with open(args[4], "wb") as target:
            target.write(data)
        sys.exit(0)
    if key == "s3api head-bucket":
        if os.path.exists({bucket!r}):
            sys.exit(0)
        fail("An error occurred (404) when calling the HeadBucket operation: Not Found")
    if key == "s3 rb":
        # Like the real CLI, `s3 rb` rejects options it does not define.
        unknown = [a for a in args[3:] if a.startswith("--") and a != "--force"]
        if unknown:
            fail("Unknown options: " + ",".join(unknown))
        os.remove({bucket!r})
        sys.exit(0)
    if key == "pricing get-products":
        price = "0.096" if "Field=volumeApiName,Value=gp3" in args else "0.0272"
        product = {{"terms": {{"OnDemand": {{"t": {{"priceDimensions": {{
            "d": {{"pricePerUnit": {{"USD": price}}}}}}}}}}}}}}
        print(json.dumps({{"PriceList": [json.dumps(product)]}}))
        sys.exit(0)
    if key == "ssm get-parameter":
        value = "Asia Pacific (Tokyo)" if "longName" in args[3] else "ami-0123abcd"
        print(json.dumps({{"Parameter": {{"Value": value}}}}))
        sys.exit(0)
    answers = {{
        "sts get-caller-identity": {{"Account": "123456789012"}},
        "ec2 describe-instance-types": {{"InstanceTypes": [
            {{"VCpuInfo": {{"DefaultVCpus": 2}}, "MemoryInfo": {{"SizeInMiB": 2048}}}}]}},
        "ec2 describe-instance-type-offerings": {{"InstanceTypeOfferings": [
            {{"Location": "ap-northeast-1a"}}]}},
        "service-quotas get-service-quota": {{"Quota": {{"Value": 32.0}}}},
        "ec2 describe-instances": {{"Reservations": []}},
        "iam get-role": {{"Role": {{"RoleName": "r", "Arn": "arn:aws:iam::123456789012:role/r"}}}},
        "iam list-instance-profiles-for-role": {{"InstanceProfiles": [
            {{"InstanceProfileName": "r"}}]}},
        "ec2 describe-vpcs": {{"Vpcs": [{{"VpcId": "vpc-1"}}]}},
        "ec2 describe-subnets": {{"Subnets": [{{"SubnetId": "subnet-1",
            "AvailabilityZone": "ap-northeast-1a", "MapPublicIpOnLaunch": True}}]}},
        "ec2 describe-images": {{"Images": [{{"RootDeviceName": "/dev/xvda"}}]}},
        "ec2 describe-security-groups": {{"SecurityGroups": [{{"GroupId": "sg-default"}}]}},
        "ec2 describe-volumes": {{"Volumes": []}},
        "ec2 describe-snapshots": {{"Snapshots": []}},
        "ec2 describe-network-interfaces": {{"NetworkInterfaces": []}},
        "ec2 describe-addresses": {{"Addresses": []}},
    }}
    if key not in answers:
        fail("stub aws: unexpected call " + key)
    print(json.dumps(answers[key]))
    """
)


def _message(result: subprocess.CompletedProcess[str]) -> str:
    """Error text with ANSI colour, error-view gutters and console wrapping removed."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
    return re.sub(r"\W+", " ", text)


class PreflightStubAwsTest(unittest.TestCase):
    """Runs -Action Preflight end to end with every AWS call stubbed."""

    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        if self.pwsh is None:
            self.skipTest("pwsh is unavailable")
        version = subprocess.run(
            [
                self.pwsh,
                "-NoProfile",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if tuple(int(part) for part in version.split(".")[:2]) < (7, 5):
            self.skipTest(f"lisjong-ec2.ps1 requires pwsh >= 7.5 (found {version})")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The wrapper resolves the Arena .venv from its own location.
        scripts = self.tmp / "repo" / "scripts" / "aws"
        scripts.mkdir(parents=True)
        for name in ("lisjong-ec2.ps1", "lisjong-ec2-runner.sh", "ssm-monitor.ps1"):
            shutil.copy(_AWS / name, scripts / name)
        self.wrapper = scripts / "lisjong-ec2.ps1"
        venv_bin = self.tmp / "repo" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        python.chmod(0o755)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.log = self.tmp / "aws-calls.jsonl"
        # Exists while the stub's transfer bucket exists.
        self.bucket_marker = self.tmp / "bucket-exists"
        self.state = self.tmp / "stub-state"
        self.state.mkdir()
        aws = bin_dir / "aws"
        aws.write_text(
            _STUB_AWS.format(
                python=sys.executable,
                log=str(self.log),
                bucket=str(self.bucket_marker),
                state=str(self.state),
            ),
            encoding="utf-8",
        )
        aws.chmod(0o755)
        self.bootstrap = self.tmp / "dummy-bootstrap.sh"
        self.bootstrap.write_text("echo ok\n", encoding="utf-8")
        self.output_root = self.tmp / "runs"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def _run(
        self, action: str, workers: int, extra: str = ""
    ) -> subprocess.CompletedProcess[str]:
        # -Command (not -File) so the array argument binds like an operator call.
        command = (
            f"& '{self.wrapper}' -Action {action} -AwsProfile stub -Label smoke "
            f"-InstanceType t3.small -Workers {workers} -Bootstrap '{self.bootstrap}' "
            "-EstimatedRuntimeHours 0.05, 0.2 -FailSafeHours 1 -CostBudgetUsd 1 "
            f"-OutputRoot '{self.output_root}' {extra}; exit $LASTEXITCODE"
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def _calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def test_preflight_writes_one_plan_without_billable_calls(self) -> None:
        result = self._run("Preflight", 1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: PREFLIGHT ONLY", result.stdout)
        plans = list(self.output_root.glob("*/plan.json"))
        self.assertEqual(len(plans), 1)
        plan = json.loads(plans[0].read_text(encoding="utf-8"))
        self.assertIsInstance(plan, dict)
        contract = plan["contract"]
        self.assertEqual((contract["vcpu"], contract["workers"]), (2, 1))
        self.assertEqual(contract["fail_safe_seconds"], 3600)
        request = contract["launch_request"]
        self.assertEqual(request["MetadataOptions"]["HttpTokens"], "required")
        self.assertEqual(request["InstanceInitiatedShutdownBehavior"], "terminate")
        self.assertEqual(
            request["BlockDeviceMappings"][0]["Ebs"],
            {"VolumeSize": 30, "VolumeType": "gp3", "Encrypted": True,
             "DeleteOnTermination": True},
        )  # fmt: skip
        self.assertEqual(request["NetworkInterfaces"][0]["Groups"], ["__RUN_SG__"])
        self.assertTrue(request["UserData"])
        self.assertEqual(
            sorted(contract["inputs"]), ["dummy-bootstrap.sh", "lisjong-ec2-runner.sh"]
        )
        self.assertLessEqual(plan["estimate"]["fail_safe_worst_case_usd"], 1)
        calls = self._calls()
        for call in calls:
            operation = [part for part in call if not part.startswith("-")]
            self.assertNotIn("create-bucket", operation)
            if "run-instances" in operation or "create-security-group" in operation:
                self.assertIn("--dry-run", call)
        self.assertTrue(any("run-instances" in call for call in calls))

    def test_workers_above_vcpu_fail_before_any_dry_run(self) -> None:
        result = self._run("Preflight", 3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exceeds the 2 vCPU", _message(result))
        self.assertFalse(any("--dry-run" in call for call in self._calls()))
        self.assertEqual(list(self.output_root.glob("*/plan.json")), [])

    def _assert_crlf_rejected_before_aws(self, script: Path) -> None:
        # A Windows checkout / editor writes CRLF; bash on EC2 then dies on line 1.
        script.write_bytes(script.read_bytes().replace(b"\n", b"\r\n"))
        result = self._run("Preflight", 1)
        self.assertNotEqual(result.returncode, 0)
        name = re.sub(r"\W+", " ", script.name)
        self.assertIn(
            f"{name} has CRLF CR line endings bash needs LF", _message(result)
        )
        self.assertEqual(self._calls(), [])
        self.assertEqual(list(self.output_root.glob("*/plan.json")), [])

    def test_crlf_bootstrap_is_rejected_before_any_aws_call(self) -> None:
        self._assert_crlf_rejected_before_aws(self.bootstrap)

    def test_crlf_runner_is_rejected_before_any_aws_call(self) -> None:
        self._assert_crlf_rejected_before_aws(self.wrapper.parent / _RUNNER.name)

    def _run_for_id(self, action: str, run_id: str) -> subprocess.CompletedProcess[str]:
        # An operator call inside a session, as operators run it: the caller sees the
        # script's $LASTEXITCODE, which must not leak the expected head-bucket 404 (254).
        command = (
            f"& '{self.wrapper}' -Action {action} -RunId {run_id} -AwsProfile stub "
            f"-OutputRoot '{self.output_root}'; exit $LASTEXITCODE"
        )
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            env=self.env, capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip

    def test_cleanup_deletes_a_retained_bucket_and_reports_no_residual(self) -> None:
        self.bucket_marker.touch()
        run_id = "smoke-20260925T162954Z-4e3061d2"
        result = self._run_for_id("Cleanup", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"No residual resources for {run_id}", result.stdout)
        self.assertFalse(self.bucket_marker.exists())
        removals = [call for call in self._calls() if call[4:6] == ["s3", "rb"]]
        self.assertEqual(
            removals[0][4:],
            ["s3", "rb", "s3://lisjong-ec2-123456789012-4e3061d2", "--force"],
        )
        self.assertEqual(len(removals), 1)

    def test_collect_accepts_an_instance_that_terminated_itself(self) -> None:
        run_id = "smoke-20260926T120000Z-4e3061d2"
        self.bucket_marker.touch()
        output = self.state / "output"
        (output / "arms").mkdir(parents=True)
        (output / "arms" / "result.json").write_bytes(b"{}\n")
        digest = hashlib.sha256(b"{}\n").hexdigest()
        (output / "sha256sums.txt").write_bytes(
            f"{digest}  arms/result.json\n".encode()
        )
        (output / "_completion.json").write_bytes(
            json.dumps({"run_id": run_id, "phase": "done", "exit_code": 0}).encode()
        )
        instance = {
            "InstanceId": "i-0123",
            "State": {"Name": "terminated"},
            "LaunchTime": "2026-09-26T12:00:00+00:00",
            "StateTransitionReason": "User initiated (2026-09-26 12:30:00 GMT)",
            "Tags": [
                {"Key": "lisjong-run-id", "Value": run_id},
                {"Key": "lisjong-command-id", "Value": "cmd-0123"},
                {"Key": "lisjong-hourly-usd", "Value": "1.0"},
            ],
        }
        (self.state / "instances.json").write_text(
            json.dumps({"Reservations": [{"Instances": [instance]}]}), encoding="utf-8"
        )
        result = self._run_for_id("Collect", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COMPLETE", result.stdout)
        operations = [call[4:6] for call in self._calls()]
        self.assertNotIn(["ec2", "terminate-instances"], operations)
        self.assertIn(["ssm", "get-command-invocation"], operations)
        self.assertFalse(self.bucket_marker.exists())
        run_dir = self.output_root / run_id
        collection = json.loads((run_dir / "collection.json").read_text("utf-8"))
        self.assertEqual(collection["result"], "COMPLETE")
        self.assertEqual(collection["billable_runtime_seconds"], 1800)
        self.assertEqual(collection["billable_end_source"], "state-transition")
        self.assertEqual(collection["residual"]["instances_not_terminated"], [])
        invocation = json.loads((run_dir / "ssm-invocation.json").read_text("utf-8"))
        self.assertEqual(invocation["Status"], "Success")
        self.assertTrue((run_dir / "evidence" / "arms" / "result.json").is_file())

    def _plan_path(self) -> Path:
        result = self._run("Preflight", 1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (plan,) = self.output_root.glob("*/plan.json")
        self.log.unlink()
        return plan

    def _billable_calls(self) -> list[list[str]]:
        return [call for call in self._calls() if "create-bucket" in call]

    def test_launch_with_the_reviewed_plan_reaches_the_first_billable_call(
        self,
    ) -> None:
        plan = self._plan_path()
        result = self._run("Launch", 1, f"-Plan '{plan}'")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("first billable call reached", _message(result))
        self.assertNotIn("differ from the reviewed plan", _message(result))
        self.assertEqual(len(self._billable_calls()), 1)

    def test_launch_with_changed_arguments_stops_before_billable_calls(self) -> None:
        plan = self._plan_path()
        result = self._run("Launch", 2, f"-Plan '{plan}'")
        self.assertIn("differ from the reviewed plan", _message(result))
        self.assertEqual(self._billable_calls(), [])


if __name__ == "__main__":
    unittest.main()
