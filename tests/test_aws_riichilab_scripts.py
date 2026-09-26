from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _REPOSITORY_ROOT / "scripts" / "aws" / "bootstrap-riichilab-12h.sh"
_LAUNCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "start-riichilab-12h.ps1"
_COLLECTOR = _REPOSITORY_ROOT / "scripts" / "aws" / "collect-riichilab-12h.ps1"
_MONITOR = _REPOSITORY_ROOT / "scripts" / "aws" / "ssm-monitor.ps1"
_WATCHER = _REPOSITORY_ROOT / "scripts" / "aws" / "watch-riichilab.ps1"
_STOPPER = _REPOSITORY_ROOT / "scripts" / "aws" / "stop-riichilab.ps1"
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _bash() -> str | None:
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    return str(git_bash) if git_bash.is_file() else shutil.which("bash")


class AwsRiichiLabAutomationScriptTest(unittest.TestCase):
    def test_bootstrap_has_valid_bash_syntax(self) -> None:
        bash = _bash()
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

    def test_powershell_scripts_have_valid_syntax_when_pwsh_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        for script in (_LAUNCHER, _COLLECTOR, _MONITOR, _WATCHER, _STOPPER):
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
        self.assertIn("lisjong-scientific-command-id", text)
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
        self.assertLess(
            text.index("if ($SubmitOnly)"),
            text.index("Wait-SsmLongRunningInvocation"),
        )
        self.assertIn("collect-riichilab-12h.ps1", text)

    def test_launcher_uses_resource_specific_iam_simulator_results(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("ResourceSpecificResults", text)
        self.assertIn("EvalResourceDecision", text)
        self.assertIn("ContainsKey($secretArn)", text)
        self.assertIn("ContainsKey($denyProbeArn)", text)
        self.assertLess(
            text.index("ResourceSpecificResults"),
            text.index('if ($decisions[$secretArn] -ne "allowed")'),
        )
        self.assertNotIn(
            "$decisions[[string]$entry.EvalResourceName] = [string]$entry.EvalDecision\n"
            'if ($decisions[$secretArn] -ne "allowed")',
            text,
        )

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

    def test_bootstrap_validates_secret_shape_through_output_json_seam(self) -> None:
        """Issue #336: `--output text`のcommand substitutionはtrailing
        newlineを正規化してしまうため、`--output json`でSecretStringの
        CR/LF evidenceを保ったまま`secret_contract`検証へ渡していることを
        固定する。
        """
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("--output json", text)
        self.assertNotIn("--query SecretString", text)
        self.assertIn("lisjong_arena.riichilab.secret_contract", text)
        self.assertLess(
            text.index("SECRET_RESPONSE_JSON"),
            text.index("lisjong_arena.riichilab.secret_contract"),
        )
        self.assertLess(
            text.index("lisjong_arena.riichilab.secret_contract"),
            text.index('export LISJONG_DEV_BOT_TOKEN="$TOKEN"'),
        )
        self.assertNotIn('echo "$SECRET_RESPONSE_JSON"', text)
        self.assertNotIn('printf "$SECRET_RESPONSE_JSON"', text)


class AwsRiichiLabSpectateScriptTest(unittest.TestCase):
    """Issue #381: opt-in live spectating through SSM port forwarding only."""

    def test_bootstrap_rejects_invalid_spectate_arguments_before_any_work(
        self,
    ) -> None:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        cases = (
            ["--spectate-port", "8765"],
            ["--play-revision", _SHA],
            ["--spectate-port", "80", "--play-revision", _SHA],
            ["--spectate-port", "70000", "--play-revision", _SHA],
            ["--spectate-port", "08765", "--play-revision", _SHA],
            ["--spectate-port", "x", "--play-revision", _SHA],
            ["--spectate-port", "8765", "--play-revision", "main"],
        )
        for extra in cases:
            with self.subTest(extra=extra):
                try:
                    result = subprocess.run(
                        [bash, str(_BOOTSTRAP), "--arena-revision", _SHA, *extra],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                except OSError as error:
                    self.skipTest(f"bash cannot be executed: {error}")
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("--", result.stderr)

    def test_bootstrap_pins_play_to_the_arena_revision_before_the_live_run(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn(
            'PLAY_REPOSITORY_URL="https://github.com/lisbun/lisjong-play.git"', text
        )
        self.assertIn('checkout -q --detach "$PLAY_REVISION"', text)
        self.assertIn('if [[ "$PLAY_PIN_CHECK" != "match" ]]; then', text)
        # Arena must stay the editable clean checkout so that durable record
        # provenance resolves lisjong_arena_revision from Git.
        self.assertIn('--no-deps -e "$PLAY_DIR"', text)
        self.assertLess(
            text.index('if [[ "$PLAY_PIN_CHECK" != "match" ]]; then'),
            text.index('--no-deps -e "$PLAY_DIR"'),
        )
        # Everything is verified before the token is fetched.
        token_fetch = text.index("secretsmanager get-secret-value")
        for marker in (
            'if [[ "$PLAY_PIN_CHECK" != "match" ]]; then',
            "-m lisjong_arena.environment_verify --project pyproject.toml",
            "preflight: spectate=on",
        ):
            self.assertLess(text.index(marker), token_fetch, marker)

    def test_bootstrap_play_pin_check_accepts_only_matching_internal_pins(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        start = text.index('PLAY_PIN_CHECK="$(')
        script = text[text.index("<<'PY'\n", start) + len("<<'PY'\n") :]
        script = script[: script.index("\nPY\n")]
        arena = _SHA
        other = "f" * 40
        arena_pyproject = (
            "[project]\nname = 'lisjong-arena'\ndependencies = [\n"
            f"  'lisjong @ git+https://github.com/lisbun/lisjong.git@{'1' * 40}',\n"
            "  'lisjong-engine @ git+https://github.com/lisbun/lisjong-engine.git"
            f"@{'2' * 40}',\n"
            "  'riichienv==0.4.10',\n]\n"
        )

        def play(*dependencies: str) -> str:
            listed = "".join(f"  '{item}',\n" for item in dependencies)
            return f"[project]\nname = 'lisjong-play'\ndependencies = [\n{listed}]\n"

        lisjong = f"lisjong @ git+https://github.com/lisbun/lisjong.git@{'1' * 40}"
        engine = (
            "lisjong-engine @ git+https://github.com/lisbun/lisjong-engine.git"
            f"@{'2' * 40}"
        )
        arena_pin = (
            f"lisjong-arena @ git+https://github.com/lisbun/lisjong-arena.git@{arena}"
        )
        cases = {
            "match": play(lisjong, engine, arena_pin),
            "mismatch": play(lisjong, engine, arena_pin.replace(arena, other)),
        }
        cases_mismatch = (
            play(lisjong.replace("1" * 40, other), engine, arena_pin),
            play(lisjong, engine, arena_pin, "requests==2.32.0"),
            play(lisjong, arena_pin),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "arena.toml").write_text(arena_pyproject, encoding="utf-8")
            (root / "check.py").write_text(script, encoding="utf-8")
            inputs = [(expected, body) for expected, body in cases.items()]
            inputs += [("mismatch", body) for body in cases_mismatch]
            for index, (expected, body) in enumerate(inputs):
                with self.subTest(index=index):
                    play_path = root / f"play{index}.toml"
                    play_path.write_text(body, encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(root / "check.py"),
                            str(play_path),
                            str(root / "arena.toml"),
                            arena,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(expected, result.stdout.strip())

    def test_bootstrap_runs_the_viewer_with_the_same_runner_contract(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        spectate_run = text[
            text.index("-m lisjong_play.riichilab_html         --continuous") :
        ]
        spectate_run = spectate_run[: spectate_run.index("else")]
        for argument in (
            '--profile "$PROFILE"',
            '"${RUNNER_BOUND_ARGS[@]}"',
            '--record-dir "$RECORD_DIR"',
            '--port "$SPECTATE_PORT"',
            '>"$RUNNER_LOG" 2>&1',
        ):
            self.assertIn(argument, spectate_run)
        self.assertNotIn("--host", text)
        self.assertNotIn("0.0.0.0", text)
        # Verification of the durable records is shared by both modes.
        self.assertEqual(1, text.count("-m lisjong_arena.riichilab.aws_run_verify"))

    def test_launcher_checks_play_pin_and_forwards_spectate_arguments(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[int]$SpectatePort = 0", text)
        self.assertIn('throw "PlayRevision requires SpectatePort."', text)
        self.assertIn(
            "raw.githubusercontent.com/lisbun/lisjong-play/$PlayRevision/pyproject.toml",
            text,
        )
        self.assertIn("$playArenaPins[0] -ne $ArenaRevision", text)
        self.assertLess(
            text.index("$playArenaPins[0] -ne $ArenaRevision"),
            text.index("if ($PreflightOnly)"),
        )
        self.assertIn(
            "--spectate-port '$SpectatePort' --play-revision '$PlayRevision'", text
        )
        self.assertIn("watch-riichilab.ps1", text)
        self.assertNotIn("authorize-security-group-ingress", text.lower())

    def test_watcher_uses_ssm_port_forwarding_with_matching_ports(self) -> None:
        text = _WATCHER.read_text(encoding="utf-8")
        self.assertIn('"AWS-StartPortForwardingSession"', text)
        self.assertIn('"portNumber=$port,localPortNumber=$port"', text)
        self.assertIn("session-manager-plugin", text)
        self.assertIn('$state.PSObject.Properties["spectate_port"]', text)
        lowered = text.lower()
        for forbidden in (
            "authorize-security-group-ingress",
            "terminate-instances",
            "send-command",
            "secretsmanager",
        ):
            self.assertNotIn(forbidden, lowered)


class AwsRiichiLabStopRequestScriptTest(unittest.TestCase):
    """Issue #383: operator stop request and until-stopped participation."""

    def _run_bootstrap(self, *extra: str) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        try:
            return subprocess.run(
                [bash, str(_BOOTSTRAP), "--arena-revision", _SHA, *extra],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            self.skipTest(f"bash cannot be executed: {error}")

    def test_bootstrap_rejects_until_stopped_with_duration(self) -> None:
        for extra in (
            ("--until-stopped", "--duration-seconds", "60"),
            ("--duration-seconds", "60", "--until-stopped"),
        ):
            with self.subTest(extra=extra):
                result = self._run_bootstrap(*extra)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("--until-stopped", result.stderr)

    def test_bootstrap_always_passes_stop_file_to_runner_and_verifier(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('STOP_FILE="$WORK_ROOT/stop-requested"', text)
        self.assertIn('RUNNER_BOUND_ARGS+=(--stop-file "$STOP_FILE")', text)
        self.assertIn('VERIFY_BOUND_ARGS+=(--stop-file "$STOP_FILE")', text)
        self.assertIn("VERIFY_BOUND_ARGS+=(--until-stopped)", text)
        self.assertIn('grep -q -- "--stop-file" <<<"$CONTINUOUS_HELP"', text)
        # Both runner invocations and the verifier use the bound arguments.
        self.assertEqual(2, text.count('"${RUNNER_BOUND_ARGS[@]}"'))
        self.assertEqual(1, text.count('"${VERIFY_BOUND_ARGS[@]}"'))
        self.assertNotIn('--duration-seconds "$DURATION_SECONDS"         ', text)

    def test_bootstrap_fails_closed_for_until_stopped_viewer_without_stop_file(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn(
            'lisjong_play.riichilab_html --help | grep -q -- "--stop-file"', text
        )
        self.assertIn(
            "--until-stopped cannot stop normally",
            text,
        )
        # Decided before the token is fetched.
        self.assertLess(
            text.index("--until-stopped cannot stop normally"),
            text.index("secretsmanager get-secret-value"),
        )

    def test_bootstrap_arms_teardown_after_verification_and_on_until_stopped_exit(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("--unit=lisjong-normal-teardown"))
        self.assertIn("trap on_exit EXIT", text)
        on_exit = text[text.index("on_exit() {") :]
        on_exit = on_exit[: on_exit.index("\n}\n")]
        self.assertIn("unset LISJONG_DEV_BOT_TOKEN", on_exit)
        self.assertIn('"$UNTIL_STOPPED" == "1" && "$RUNNER_STARTED" == "1"', on_exit)
        self.assertIn("arm_normal_teardown", on_exit)
        verify = text.index("-m lisjong_arena.riichilab.aws_run_verify")
        success_arm = text.rindex("\narm_normal_teardown\n")
        self.assertLess(verify, success_arm)
        self.assertLess(
            text.index("RUNNER_STARTED=1\nset +e"),
            text.index(
                "-m lisjong_arena.riichilab.continuous_ranked         --profile"
            ),
        )

    def test_launcher_until_stopped_requires_explicit_fail_safe(self) -> None:
        text = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("[switch]$UntilStopped", text)
        self.assertIn('$PSBoundParameters.ContainsKey("FailSafeHours")', text)
        self.assertIn('$PSBoundParameters.ContainsKey("DurationSeconds")', text)
        self.assertIn("if ($FailSafeHours -gt 47)", text)
        self.assertIn('$remoteCommand += " --until-stopped"', text)
        self.assertIn("until_stopped = [bool]$UntilStopped", text)
        self.assertIn("stop-riichilab.ps1", text)

    def test_stopper_only_creates_the_stop_file_of_an_active_run(self) -> None:
        text = _STOPPER.read_text(encoding="utf-8")
        self.assertIn('"ssm", "get-command-invocation"', text)
        self.assertIn('$runStatus -notin @("Pending", "InProgress", "Delayed")', text)
        self.assertLess(
            text.index('$runStatus -notin @("Pending", "InProgress", "Delayed")'),
            text.index('"ssm", "send-command"'),
        )
        self.assertIn('"touch $workRoot/stop-requested"', text)
        self.assertIn('$workRoot = "/var/lib/lisjong-riichilab-313"', text)
        self.assertIn(
            'WORK_ROOT="/var/lib/lisjong-riichilab-313"',
            _BOOTSTRAP.read_text(encoding="utf-8"),
        )
        self.assertIn("stop_requested_at_utc", text)
        lowered = text.lower()
        for forbidden in (
            "terminate-instances",
            "stop-instances",
            "poweroff",
            "kill",
            "secretsmanager",
        ):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
