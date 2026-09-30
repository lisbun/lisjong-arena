"""#434: R5 wiring, same-process runner, evidence and bootstrap failure gates."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong_arena.riichilab import aws_backend as backend
from lisjong_arena.shanten_backend_verification.backend import (
    EXPECTED_LISJONG_REVISION,
    EXPECTED_NATIVE_API_VERSION,
    EXPECTED_WHEEL_SHA256,
    ShantenBackendVerificationError,
)

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts/aws/bootstrap-riichilab-12h.sh"


def _record(name="rust"):
    return {
        "schema_version": 1,
        "backend": name,
        "lisjong_revision": EXPECTED_LISJONG_REVISION,
        "native_api_version": EXPECTED_NATIVE_API_VERSION if name == "rust" else None,
        "wheel_sha256": EXPECTED_WHEEL_SHA256 if name == "rust" else None,
        "r5_probe_calls": 1 if name == "rust" else 0,
        "pid": os.getpid(),
    }


class BackendReceiptTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "backend.json"

    def test_python_and_rust_evidence_are_validated(self):
        for name in ("python", "rust"):
            value = _record(name)
            self.path.write_text(json.dumps(value))
            self.assertEqual(value, backend.read_backend_evidence(self.path, name))

    def test_missing_corrupt_and_mismatched_evidence_is_rejected(self):
        with self.assertRaises(ShantenBackendVerificationError):
            backend.read_backend_evidence(self.path, "rust")
        for data in ("{", "[]", "x" * 2049):
            self.path.write_text(data)
            with self.assertRaises(ShantenBackendVerificationError):
                backend.read_backend_evidence(self.path, "rust")
        for field, value in (
            ("schema_version", True),
            ("backend", "python"),
            ("lisjong_revision", "0" * 40),
            ("native_api_version", 2),
            ("wheel_sha256", "0" * 64),
            ("r5_probe_calls", 0),
            ("r5_probe_calls", True),
            ("pid", -1),
            ("unexpected", "secret"),
        ):
            with self.subTest(field=field, value=value):
                self.path.write_text(json.dumps({**_record(), field: value}))
                with self.assertRaises(ShantenBackendVerificationError):
                    backend.read_backend_evidence(self.path, "rust")

    def test_runner_uses_same_interpreter_and_verified_evidence(self):
        for runner, module in (
            ("continuous", "lisjong_arena.riichilab.continuous_ranked"),
            ("spectate", "lisjong_play.riichilab_html"),
        ):
            self.path.unlink(missing_ok=True)

            def enter(name, **kwargs):
                self.assertEqual(module, name)
                self.assertEqual(os.getpid(), json.loads(self.path.read_text())["pid"])
                self.assertEqual(
                    [module, "--profile", "lisjong-dev", "--stop-file", "stop"],
                    sys.argv,
                )

            with (
                patch.object(backend, "probe_backend", return_value=_record()),
                patch.object(backend.runpy, "run_module", side_effect=enter) as run,
                patch.object(sys, "argv", ["test"]),
            ):
                backend.main(
                    [
                        "--backend",
                        "rust",
                        "--wheel",
                        "wheel",
                        "--evidence",
                        str(self.path),
                        "--profile",
                        "lisjong-dev",
                        "--runner",
                        runner,
                        "--",
                        "--stop-file",
                        "stop",
                    ]
                )
            run.assert_called_once()

    def test_failed_probe_never_enters_runner_or_writes_receipt(self):
        with (
            patch.object(
                backend,
                "probe_backend",
                side_effect=ShantenBackendVerificationError("bad wheel"),
            ),
            patch.object(backend.runpy, "run_module") as run,
        ):
            with self.assertRaises(ShantenBackendVerificationError):
                backend.main(
                    [
                        "--backend",
                        "rust",
                        "--wheel",
                        "wheel",
                        "--evidence",
                        str(self.path),
                        "--profile",
                        "lisjong-dev",
                        "--runner",
                        "continuous",
                    ]
                )
            run.assert_not_called()
            self.assertFalse(self.path.exists())


class R5ProbeTest(unittest.TestCase):
    def test_wheel_required_exactly_for_rust(self):
        for name, wheel in (("rust", None), ("python", Path("wheel"))):
            with self.assertRaises(ShantenBackendVerificationError):
                backend.probe_backend(name, wheel)

    def test_real_factory_must_increment_native_counter(self):
        from lisjong.policies import (
            terminal_shanten_progression_mechanism_riichi_defense as r5,
        )

        for calls in ((0, 1), (0, 0)):
            native = SimpleNamespace(
                progression_evaluation_call_count=unittest.mock.Mock(side_effect=calls)
            )
            with (
                patch.object(
                    backend,
                    "require_shanten_backend",
                    return_value={
                        "lisjong_revision": EXPECTED_LISJONG_REVISION,
                        "native": {"api_version": 3},
                    },
                ),
                patch.object(
                    backend,
                    "verify_installed_native",
                    return_value={"sha256": EXPECTED_WHEEL_SHA256},
                ),
                patch.dict(sys.modules, {"_lisjong_native": native}),
                patch.object(r5, "_new_progression_evaluator") as factory,
            ):
                if calls == (0, 0):
                    with self.assertRaisesRegex(
                        ShantenBackendVerificationError, "R5 factory"
                    ):
                        backend.probe_backend("rust", Path("wheel"))
                else:
                    value = backend.probe_backend("rust", Path("wheel"))
                    self.assertEqual(1, value["r5_probe_calls"])
                factory.return_value.evaluate_roots.assert_called_once()


@unittest.skipUnless(shutil.which("bash"), "bash required")
class BootstrapBackendTest(unittest.TestCase):
    def test_two_bot_processes_receive_backend_and_isolated_credentials(self):
        text = _BOOTSTRAP.read_text()
        start = text.index("start_bot() {")
        function = text[
            start : text.index('\nfor profile in "${BOT_PROFILES[@]}"', start)
        ]
        for spectate in ("0", "1"):
            with self.subTest(spectate=spectate), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for profile in ("dev", "baseline"):
                    (root / profile).mkdir()
                fake = root / "python"
                fake.write_text(
                    "#!/usr/bin/env python3\nimport json,os,sys\n"
                    'print(json.dumps({"args":sys.argv[1:], "backend":os.environ.get("LISJONG_SHANTEN_BACKEND"), '
                    '"dev":os.environ.get("FAKE_DEV_TOKEN"), "baseline":os.environ.get("FAKE_BASELINE_TOKEN")}))\n'
                )
                fake.chmod(0o755)
                script = (
                    """set -eu
unset FAKE_DEV_TOKEN FAKE_BASELINE_TOKEN
declare -A BOT_NAMES=() BOT_ENV_VARS=([dev]=FAKE_DEV_TOKEN [baseline]=FAKE_BASELINE_TOKEN)
declare -A BOT_TOKENS=([dev]=fake-dev [baseline]=fake-baseline) BOT_PORTS=([dev]=8765 [baseline]=8766)
BACKEND_ARGS=(--backend rust --wheel /fixed/wheel.whl)
RUNNER_BOUND_ARGS=(--duration-seconds 1800)
"""
                    + function
                    + "\nstart_bot dev\nstart_bot baseline\nwait\n"
                )
                result = subprocess.run(
                    ["bash", "-c", script],
                    env={
                        **os.environ,
                        "BOTS_DIR": td,
                        "PYTHON": str(fake),
                        "SPECTATE": spectate,
                        "SHANTEN_BACKEND": "rust",
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                for profile, port in (("dev", "8765"), ("baseline", "8766")):
                    receipt = json.loads(
                        (root / profile / "continuous.log").read_text()
                    )
                    self.assertEqual("rust", receipt["backend"])
                    self.assertEqual("fake-" + profile, receipt[profile])
                    self.assertIsNone(
                        receipt["baseline" if profile == "dev" else "dev"]
                    )
                    args = receipt["args"]
                    self.assertEqual("lisjong_arena.riichilab.aws_backend", args[1])
                    self.assertEqual(profile, args[args.index("--profile") + 1])
                    self.assertEqual(
                        str(root / profile / "backend.json"),
                        args[args.index("--evidence") + 1],
                    )
                    self.assertEqual(
                        "spectate" if spectate == "1" else "continuous",
                        args[args.index("--runner") + 1],
                    )
                    self.assertEqual(spectate == "1", "--continuous" in args)
                    if spectate == "1":
                        self.assertEqual(port, args[args.index("--port") + 1])

    def test_invalid_rust_inputs_exit_before_any_work(self):
        cases = (
            ["--shanten-backend", "rust"],
            ["--shanten-backend", "other"],
            ["--native-wheel-s3-uri", "s3://bucket/wheel.whl"],
            [
                "--shanten-backend",
                "rust",
                "--native-wheel-s3-uri",
                "https://bucket/wheel.whl",
            ],
            [
                "--shanten-backend",
                "rust",
                "--native-wheel-s3-uri",
                "s3://bucket/x'$(touch pwn).whl",
            ],
        )
        for args in cases:
            with self.subTest(args=args):
                result = subprocess.run(
                    ["bash", str(_BOOTSTRAP), "--arena-revision", "1" * 40, *args],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(2, result.returncode)
                self.assertNotIn("dnf", result.stdout)

    def test_install_and_probe_fail_before_progressing(self):
        text = _BOOTSTRAP.read_text()
        block = text[
            text.index("# Resolve the pinned native wheel") : text.index(
                'cd "$REPO_DIR"\n"$PYTHON" -m lisjong_arena.environment_verify'
            )
        ]
        self.assertLess(
            text.index("# Resolve the pinned native wheel"),
            text.index("secretsmanager get-secret-value"),
        )
        for failure in ("verify-wheel", "install", "aws_backend", ""):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                fake = root / "python"
                fake.write_text(
                    '#!/usr/bin/env python3\nimport sys,os\nfrom pathlib import Path\na=sys.argv[1:]\nwith open(os.environ["CALL_LOG"],"a") as f:f.write(" ".join(a)+"\\n")\nif a[0]=="-c":print("wheel.whl")\nif os.environ["FAIL"] and any(os.environ["FAIL"] in x for x in a):sys.exit(7)\n'
                )
                fake.chmod(0o755)
                script = "set -eu\naws() { :; }\n" + block + "\necho FINISHED\n"
                env = {
                    **os.environ,
                    "PYTHON": str(fake),
                    "WORK_ROOT": td,
                    "BOOTSTRAP_LOG": str(root / "log"),
                    "CALL_LOG": str(root / "calls"),
                    "FAIL": failure,
                    "SHANTEN_BACKEND": "rust",
                    "NATIVE_WHEEL_S3_URI": "s3://bucket/wheel.whl",
                    "REGION": "test",
                }
                result = subprocess.run(
                    ["bash", "-c", script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(7 if failure else 0, result.returncode, result.stderr)
                self.assertEqual(not failure, "FINISHED" in result.stdout)
                calls = (root / "calls").read_text()
                if failure == "verify-wheel":
                    self.assertNotIn("pip install", calls)
                if failure == "install":
                    self.assertNotIn("aws_backend", calls)


class AckTimingTest(unittest.TestCase):
    def test_missing_fields_are_not_zero_and_late_ack_is_counted_as_event(self):
        from lisjong_arena.riichilab.aws_run_verify import _ack_timing

        def event(status, **fields):
            return SimpleNamespace(
                direction="recv",
                payload={"type": "action_ack", "status": status, **fields},
            )

        rows = [
            event("accepted", elapsed_ms=4000, bank_ms=14000, bank_consumed_ms=1000),
            event("defaulted", elapsed_ms=17000, bank_ms=0, bank_consumed_ms=14000),
            event("stale", elapsed_ms=True),
            event("accepted"),
        ]
        result = _ack_timing([SimpleNamespace(protocol_entries=rows)])
        self.assertEqual(4, result["ack_count"])
        self.assertEqual(
            {"accepted": 2, "defaulted": 1, "stale": 1}, result["status_counts"]
        )
        self.assertEqual(2, result["measured_ack_counts"]["elapsed_ms"])
        self.assertEqual(17000, result["max_elapsed_ms"])
        self.assertEqual(0, result["min_bank_ms"])
        self.assertIsNone(_ack_timing([])["max_elapsed_ms"])


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell required")
class LauncherPreflightTest(unittest.TestCase):
    def test_short_run_and_wheel_arguments_are_checked_before_aws(self):
        text = (_ROOT / "scripts/aws/start-riichilab-12h.ps1").read_text()
        # Execute the real argument checks, stopping before any AWS call.
        prefix = text[: text.index("function Invoke-AwsText {")]
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "preflight.ps1"
            script.write_text(prefix + '\nWrite-Output "VALIDATED"\n')
            cases = (
                (["-DurationSeconds", "1800", "-FailSafeHours", "2"], True),
                (["-DurationSeconds", "7200", "-FailSafeHours", "2"], False),
                (["-ShantenBackend", "rust"], False),
                (
                    [
                        "-ShantenBackend",
                        "rust",
                        "-NativeWheelS3Uri",
                        "s3://bucket/wheel.whl",
                    ],
                    True,
                ),
                (["-NativeWheelS3Uri", "s3://bucket/wheel.whl"], False),
                (
                    [
                        "-ShantenBackend",
                        "rust",
                        "-NativeWheelS3Uri",
                        "s3://bucket/a'$(exit).whl",
                    ],
                    False,
                ),
            )
            for args, valid in cases:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [
                            "pwsh",
                            "-NoProfile",
                            "-File",
                            str(script),
                            "-AwsProfile",
                            "fake",
                            "-OutputRoot",
                            td,
                            *args,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(valid, result.returncode == 0, result.stderr)
                    self.assertEqual(valid, "VALIDATED" in result.stdout)

    def test_exact_object_iam_denial_stops_preflight(self):
        text = (_ROOT / "scripts/aws/start-riichilab-12h.ps1").read_text()
        start = text.index(
            'if ($ShantenBackend -eq "rust") {', text.index("$roleArn =")
        )
        block = text[
            start : text.index(
                "if ([string]::IsNullOrWhiteSpace($InstanceProfileName))", start
            )
        ]
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                setup = """
$ErrorActionPreference = 'Stop'
$ShantenBackend = 'rust'; $wheelBucket = 'bucket'; $wheelKey = 'fixed/wheel.whl'; $roleArn = 'role'
function Invoke-AwsJson { param($Arguments)
    if ($Arguments[0] -eq 's3api') { return @{ ContentLength = 123 } }
    if (($Arguments -join ' ') -notmatch 's3:GetObject --resource-arns arn:aws:s3:::bucket/fixed/wheel.whl') { throw 'wrong scope' }
    return @{ EvaluationResults = @(@{ EvalDecision = 'DECISION' }) }
}
""".replace("DECISION", "allowed" if allowed else "implicitDeny")
                result = subprocess.run(
                    [
                        "pwsh",
                        "-NoProfile",
                        "-Command",
                        setup + block + '\nWrite-Output "PASSED"',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(allowed, result.returncode == 0, result.stderr)
                self.assertEqual(allowed, "PASSED" in result.stdout)


if __name__ == "__main__":
    unittest.main()
