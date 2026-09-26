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
        # Issue #386: every bot's secret is simulated and must be allowed.
        self.assertIn('"--resource-arns") + $secretArns + @($denyProbeArn)', text)
        self.assertIn("ContainsKey($botEntry.secret_arn)", text)
        self.assertIn("ContainsKey($denyProbeArn)", text)
        self.assertLess(
            text.index("ResourceSpecificResults"),
            text.index('if ($decisions[$botEntry.secret_arn] -ne "allowed")'),
        )
        self.assertIn('if ($decisions[$denyProbeArn] -eq "allowed")', text)

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
        # Issue #386: tokens are never exported by the bootstrap itself; each
        # bot's subshell exports only its own profile's credential variable.
        self.assertIn('BOT_TOKENS[$profile]="$TOKEN"', text)
        self.assertIn("unset TOKEN", text)
        self.assertNotRegex(text, r"(?m)^export ")
        self.assertNotIn("LISJONG_DEV_BOT_TOKEN", text)
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
            text.index('BOT_TOKENS[$profile]="$TOKEN"'),
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
        start_bot = text[text.index("start_bot() {") :]
        start_bot = start_bot[: start_bot.index("\n}\n")]
        self.assertIn(
            'local args=(--profile "$profile" "${RUNNER_BOUND_ARGS[@]}" '
            '--record-dir "$directory/records")',
            start_bot,
        )
        spectate_run = start_bot[: start_bot.index("    else")]
        for argument in (
            'args+=(--port "${BOT_PORTS[$profile]}")',
            'exec "$PYTHON" -m lisjong_play.riichilab_html --continuous "${args[@]}"',
            ') >"$directory/continuous.log" 2>&1 &',
        ):
            self.assertIn(argument, spectate_run)
        self.assertNotIn("--host", text)
        self.assertNotIn("0.0.0.0", text)
        # Verification of the durable records is shared by both modes.
        self.assertEqual(
            1, text.count("-m lisjong_arena.riichilab.aws_instance_run verify")
        )

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
        self.assertIn(
            'VERIFY_BOUND_ARGS=("${BOT_CONFIG_ARGS[@]}" --stop-file "$STOP_FILE")',
            text,
        )
        self.assertIn("VERIFY_BOUND_ARGS+=(--until-stopped)", text)
        self.assertIn('grep -q -- "--stop-file" <<<"$CONTINUOUS_HELP"', text)
        # Every bot (both runner kinds) and the verifier use the bound arguments.
        self.assertEqual(1, text.count('"${RUNNER_BOUND_ARGS[@]}"'))
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
        self.assertIn("unset BOT_TOKENS", on_exit)
        # Issue #337: any failed exit arms it, not only until-stopped runs.
        self.assertNotIn("UNTIL_STOPPED", on_exit)
        self.assertIn("arm_normal_teardown", on_exit)
        # The supervisor drains every bot, then verification, then teardown.
        drained = text.index("while ((${#RUNNING_BOT_PIDS[@]})); do")
        verify = text.index("-m lisjong_arena.riichilab.aws_instance_run verify")
        success_arm = text.index("    arm_normal_teardown\n    echo")
        self.assertLess(drained, verify)
        self.assertLess(verify, success_arm)
        self.assertIn(
            'if [[ "$VERIFY_EXIT_CODE" -eq 0 ]]; then\n    arm_normal_teardown', text
        )
        self.assertLess(
            text.index("trap on_exit EXIT"),
            text.index('    start_bot "$profile"'),
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
        self.assertIn("(set -C; printf 'operator\\n' > $workRoot/stop-requested)", text)
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


class AwsRiichiLabFailureTeardownTest(unittest.TestCase):
    """Issue #337: a confirmed instance-side failure arms the short teardown.

    Bootstrap, secret/configuration, bot and verification failures arm the
    same five-minute teardown in duration-bound and until-stopped runs alike,
    after dropping runtime secrets. Nothing local (monitor detachment, AWS
    login expiry, unknown remote state) can reach this instance-side path.
    """

    def _teardown_functions(self) -> str:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        start = text.index("NORMAL_TEARDOWN_ARMED=0\n")
        end = text.index("trap on_exit EXIT\n") + len("trap on_exit EXIT\n")
        return text[start:end]

    def _run(self, body: str, *, systemd_run_code: int = 0):
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        # systemd-run is a shell function here, so no real timer is created.
        # It records whether any runtime secret was still set when called.
        harness = (
            "set -euo pipefail\n"
            'LOG="$1"\n'
            "systemd-run() {\n"
            "    local tokens=unset\n"
            "    if declare -p BOT_TOKENS >/dev/null 2>&1; then tokens=set; fi\n"
            '    echo "systemd-run $* tokens=$tokens'
            ' token=${TOKEN-unset} response=${SECRET_RESPONSE_JSON-unset}"'
            ' >>"$LOG"\n'
            f"    return {systemd_run_code}\n"
            "}\n"
            "declare -A BOT_TOKENS=([bot]=secret-value)\n"
            + self._teardown_functions()
            + body
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "harness.sh"
            script.write_text(harness, encoding="utf-8")
            log = root / "systemd-run.log"
            try:
                result = subprocess.run(
                    [bash, str(script), str(log)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except OSError as error:
                self.skipTest(f"bash cannot be executed: {error}")
            calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    def test_confirmed_failures_arm_short_teardown_after_secret_cleanup(
        self,
    ) -> None:
        cases = {
            # secret retrieval/validation failure with a token in flight
            "bootstrap_secret": (
                'TOKEN=secret-value\nSECRET_RESPONSE_JSON="{}"\nfalse\n',
                1,
            ),
            # duration-bound bot / verification FAIL: exit "$VERIFY_EXIT_CODE"
            "verification_fail": ("UNTIL_STOPPED=0\nexit 1\n", 1),
            # verification could not run
            "verification_error": ("UNTIL_STOPPED=0\nexit 3\n", 3),
            "until_stopped_fail": ("UNTIL_STOPPED=1\nexit 1\n", 1),
        }
        for name, (body, expected_code) in cases.items():
            with self.subTest(case=name):
                result, calls = self._run(body)
                self.assertEqual(expected_code, result.returncode, result.stderr)
                self.assertEqual(1, len(calls), calls)
                self.assertIn("--unit=lisjong-normal-teardown", calls[0])
                self.assertIn("--on-active=5min", calls[0])
                self.assertIn("/usr/bin/systemctl poweroff", calls[0])
                self.assertIn("tokens=unset token=unset response=unset", calls[0])
                self.assertIn(
                    f"run: failed exit_code={expected_code} "
                    "failure_teardown_armed=5min",
                    result.stderr,
                )
                self.assertNotIn("secret-value", result.stdout + result.stderr)
                self.assertNotIn("PASS", result.stdout + result.stderr)

    def test_success_keeps_the_single_post_pass_teardown(self) -> None:
        result, calls = self._run("arm_normal_teardown\nexit 0\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(calls), calls)
        self.assertNotIn("run: failed", result.stderr)
        result, calls = self._run("exit 0\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], calls)

    def test_teardown_arm_failure_keeps_exit_code_and_long_fail_safe(self) -> None:
        result, calls = self._run("exit 1\n", systemd_run_code=1)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(1, len(calls), calls)
        self.assertIn("failure_teardown_not_armed", result.stderr)
        self.assertIn("long cost fail-safe remains armed", result.stderr)

    def test_trap_covers_bootstrap_but_not_foreign_instances(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        trap = text.index("trap on_exit EXIT")
        # Armed only on this run's own AL2023 instance with a fresh work root...
        for guard in (
            'echo "bootstrap must run as root under SSM"',
            'echo "expected Amazon Linux 2023"',
            'echo "expected x86_64 architecture"',
            'echo "work root is not fresh"',
        ):
            self.assertLess(text.index(guard), trap, guard)
        # ...and before every bootstrap, secret, bot and verification step.
        for step in (
            "static AWS credential file is present",
            "dnf -q install",
            "git clone",
            "lisjong_arena.environment_verify",
            "aws_instance_run check-config",
            "secretsmanager get-secret-value",
            'start_bot "$profile"',
            "aws_instance_run verify",
        ):
            self.assertLess(trap, text.index(step), step)
        self.assertEqual(1, text.count("\ntrap "))

    def test_long_fail_safe_and_local_detachment_paths_are_unchanged(self) -> None:
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        collector = _COLLECTOR.read_text(encoding="utf-8")
        monitor = _MONITOR.read_text(encoding="utf-8")
        # The long fail-safe is a separate launcher-armed unit the bootstrap
        # never touches.
        self.assertIn(
            "--unit=lisjong-cost-failsafe --on-active=$($FailSafeHours)h", launcher
        )
        self.assertNotIn("lisjong-cost-failsafe", bootstrap)
        # Local monitor detachment / unknown status never terminates (#333).
        self.assertNotIn("terminate-instances", monitor)
        self.assertIn(
            "It is not being force-terminated from this catch path.", launcher
        )
        self.assertIn("No termination or teardown action was taken.", collector)
        # A self-terminated failed instance is accepted as already terminated,
        # and collection never resubmits the workload.
        ensure = collector[collector.index("function Ensure-InstanceTerminated") :]
        self.assertLess(
            ensure.index('if ($instanceState -eq "terminated")'),
            ensure.index('"ec2", "terminate-instances"'),
        )
        self.assertNotIn("send-command", collector)


class AwsRiichiLabBotSupervisorTest(unittest.TestCase):
    """Issue #383: every bot of a run is supervised by one bootstrap.

    When any bot exits, the shared stop file is written so that the other bots
    finish their hanchan and stop; the bootstrap continues only after every bot
    has exited.
    """

    def _supervisor_script(self) -> str:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        request_stop = text[text.index("request_stop() {") :]
        request_stop = request_stop[: request_stop.index("\n}\n") + 3]
        loop = text[text.index('RUNNING_BOT_PIDS=("${!BOT_NAMES[@]}")') :]
        end = loop.index("\ndone\n") + len("\ndone\n")
        return request_stop + loop[:end]

    def test_bootstrap_backgrounds_every_bot_under_the_supervisor(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertEqual(2, text.count(') >"$directory/continuous.log" 2>&1 &'))
        self.assertIn('BOT_NAMES[$!]="$profile"', text)
        self.assertIn('for profile in "${BOT_PROFILES[@]}"; do\n    start_bot', text)
        self.assertIn('wait -n -p EXITED_BOT_PID "${RUNNING_BOT_PIDS[@]}"', text)
        self.assertIn('request_stop "bot-exited:$EXITED_BOT"', text)
        self.assertIn("bash 5.1 or newer is required", text)
        # Verification runs only after the supervisor loop has drained.
        self.assertLess(
            text.index("while ((${#RUNNING_BOT_PIDS[@]})); do"),
            text.index("-m lisjong_arena.riichilab.aws_instance_run verify"),
        )

    def test_one_bot_exit_stops_the_others_after_their_hanchan(self) -> None:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        harness = (
            "set -euo pipefail\n"
            'STOP_FILE="$1"\n'
            "bot() {\n"
            "    # $1 name, $2 exit code when it exits on its own, $3 own ticks\n"
            "    local i=0\n"
            "    while ((i < 100)); do\n"
            '        if [[ -e "$STOP_FILE" ]]; then\n'
            "            sleep 0.2  # finish the hanchan in progress\n"
            '            echo "stopped:$1" >>"$STOP_FILE.log"\n'
            "            exit 0\n"
            "        fi\n"
            "        sleep 0.05\n"
            "        i=$((i + 1))\n"
            '        if ((i == $3)); then exit "$2"; fi\n'
            "    done\n"
            "    exit 99\n"
            "}\n"
            "declare -A BOT_NAMES=()\n"
            'BOTS_DIR="$(dirname "$STOP_FILE")/bots"\n'
            'mkdir -p "$BOTS_DIR/failing" "$BOTS_DIR/steady" "$BOTS_DIR/other" '
            '"$BOTS_DIR/only"\n'
            'bot failing 3 4 & BOT_NAMES[$!]="failing"\n'
            'bot steady 0 1000 & BOT_NAMES[$!]="steady"\n'
            'bot other 0 1000 & BOT_NAMES[$!]="other"\n'
            + self._supervisor_script()
            + 'echo "supervisor=drained"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "harness.sh"
            script.write_text(harness, encoding="utf-8")
            stop_file = root / "stop-requested"
            try:
                result = subprocess.run(
                    [bash, str(script), str(stop_file)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except OSError as error:
                self.skipTest(f"bash cannot be executed: {error}")
            self.assertEqual(0, result.returncode, result.stderr)
            # The first writer wins: the failing bot's exit is the recorded reason.
            self.assertEqual(
                "bot-exited:failing\n", stop_file.read_text(encoding="utf-8")
            )
            stopped = sorted(
                (root / "stop-requested.log").read_text(encoding="utf-8").split()
            )
            # Per-bot exit evidence for the instance verifier.
            exit_codes = {
                name: (root / "bots" / name / "exit_code").read_text().strip()
                for name in ("failing", "steady", "other")
            }
            for name in ("failing", "steady", "other"):
                self.assertRegex(
                    (root / "bots" / name / "stop_utc").read_text(),
                    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n$",
                )
        self.assertEqual(["stopped:other", "stopped:steady"], stopped)
        self.assertEqual({"failing": "3", "steady": "0", "other": "0"}, exit_codes)
        self.assertIn("supervisor=drained", result.stdout)
        self.assertEqual(3, result.stdout.count("run: bot_exited"))

    def test_an_existing_operator_stop_reason_is_kept(self) -> None:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        harness = (
            "set -euo pipefail\n"
            'STOP_FILE="$1"\n'
            "printf 'operator\\n' >\"$STOP_FILE\"\n"
            "declare -A BOT_NAMES=()\n"
            'BOTS_DIR="$(dirname "$STOP_FILE")/bots"\n'
            'mkdir -p "$BOTS_DIR/failing" "$BOTS_DIR/steady" "$BOTS_DIR/other" '
            '"$BOTS_DIR/only"\n'
            'true & BOT_NAMES[$!]="only"\n'
            + self._supervisor_script()
            + 'echo "supervisor=drained"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "harness.sh"
            script.write_text(harness, encoding="utf-8")
            stop_file = root / "stop-requested"
            try:
                result = subprocess.run(
                    [bash, str(script), str(stop_file)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except OSError as error:
                self.skipTest(f"bash cannot be executed: {error}")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("operator\n", stop_file.read_text(encoding="utf-8"))
            self.assertEqual(
                "0", (root / "bots" / "only" / "exit_code").read_text().strip()
            )
        self.assertIn("supervisor=drained", result.stdout)


class AwsRiichiLabMultiBotScriptTest(unittest.TestCase):
    """Issue #386: several explicitly configured bots on one instance."""

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

    def test_bootstrap_rejects_unsafe_bot_lists_before_any_work(self) -> None:
        dev = "lisjong-dev=lisjong/riichilab/dev"
        baseline = "lisjong-baseline=lisjong/riichilab/baseline"
        cases = {
            "duplicate profile": ("--bot", dev, "--bot", "lisjong-dev=other"),
            "duplicate secret": (
                "--bot",
                dev,
                "--bot",
                "lisjong-baseline=lisjong/riichilab/dev",
            ),
            "missing secret": ("--bot", "lisjong-dev"),
            "quote in secret": ("--bot", "lisjong-dev=a'b"),
            "too many bots": (
                "--bot",
                dev,
                "--bot",
                baseline,
                "--bot",
                "lisjong=c",
                "--bot",
                "x=d",
                "--bot",
                "y=e",
            ),
            "bot with secret id": ("--bot", dev, "--secret-id", "other"),
            "port overflow": (
                "--bot",
                dev,
                "--bot",
                baseline,
                "--spectate-port",
                "65535",
                "--play-revision",
                _SHA,
            ),
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                result = self._run_bootstrap(*extra)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("--", result.stderr)

    def test_single_bot_operation_stays_the_default(self) -> None:
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        launcher = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('BOT_SPECS=("lisjong-dev=$SECRET_ID")', bootstrap)
        self.assertIn('SECRET_ID="lisjong/riichilab/lisjong-dev-token"', bootstrap)
        self.assertIn('$Bot = @("lisjong-dev=$SecretId")', launcher)
        self.assertIn(
            '[string]$SecretId = "lisjong/riichilab/lisjong-dev-token"', launcher
        )

    def test_bootstrap_resolves_every_credential_before_the_first_bot(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        check_config = text.index("aws_instance_run check-config")
        token_fetch = text.index("secretsmanager get-secret-value")
        resolved = text.index('echo "preflight: credentials_resolved=')
        first_start = text.index('    start_bot "$profile"')
        self.assertLess(check_config, token_fetch)
        self.assertLess(token_fetch, resolved)
        self.assertLess(resolved, first_start)
        self.assertIn('--secret-id "${BOT_SECRET_IDS[$index]}"', text)
        self.assertIn("resolve the same runtime token", text)
        self.assertIn(
            'echo "runtime secret could not be resolved for bot $profile"', text
        )

    def test_each_bot_process_receives_only_its_own_credential(self) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        start_bot = text[text.index("start_bot() {") :]
        start_bot = start_bot[: start_bot.index("\n}\n")]
        own = 'export "${BOT_ENV_VARS[$profile]}=${BOT_TOKENS[$profile]}"'
        self.assertEqual(2, start_bot.count(own))
        self.assertEqual(2, start_bot.count("unset BOT_TOKENS"))
        self.assertNotIn('"${BOT_TOKENS[@]}"', text)
        # Only the verifier's subshell receives every bot's variable.
        verifier = text[text.index('SUMMARY_JSON="$(') :]
        verifier = verifier[: verifier.index(')"\n')]
        self.assertIn('for profile in "${BOT_PROFILES[@]}"; do', verifier)
        self.assertIn(own, verifier)

    def test_bootstrap_uses_per_bot_evidence_and_returns_failed_summaries(
        self,
    ) -> None:
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('BOTS_DIR="$WORK_ROOT/bots"', text)
        self.assertIn('mkdir -p "$BOTS_DIR/$profile/records"', text)
        self.assertIn('>"$BOTS_DIR/$EXITED_BOT/exit_code"', text)
        self.assertIn('>"$BOTS_DIR/$EXITED_BOT/stop_utc"', text)
        self.assertIn("several bots cannot stop together", text)
        # The summary is returned for PASS and FAIL; exit code follows it.
        tail = text[text.index('SUMMARY_B64="$(') :]
        self.assertIn("printf 'LISJONG_COMPLETION_JSON_B64=%s\\n'", tail)
        self.assertIn('exit "$VERIFY_EXIT_CODE"', tail)
        self.assertLess(
            tail.index("LISJONG_COMPLETION_JSON_B64"),
            tail.index('exit "$VERIFY_EXIT_CODE"'),
        )

    def test_launcher_bot_list_mirrors_the_instance_contract(self) -> None:
        from lisjong_arena.riichilab.aws_instance_run import (
            _SECRET_ID_RE,
            EXPECTED_POLICY_BY_PROFILE,
            MAX_BOTS,
        )

        text = _LAUNCHER.read_text(encoding="utf-8")
        bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("[string[]]$Bot = @()", text)
        supported = ", ".join(f'"{name}"' for name in EXPECTED_POLICY_BY_PROFILE)
        self.assertIn(f"$supportedBotProfiles = @({supported})", text)
        self.assertIn(f"$maxBots = {MAX_BOTS}", text)
        self.assertIn(f"MAX_BOTS={MAX_BOTS}", bootstrap)
        secret = _SECRET_ID_RE.pattern
        self.assertIn(f"'^([a-z0-9-]+)=({secret})$'", text)
        self.assertIn(f"^([a-z0-9-]+)=({secret})$", bootstrap)
        self.assertIn('$PSBoundParameters.ContainsKey("SecretId")', text)
        self.assertIn(
            "$remoteCommand += \" --bot '$($botEntry.profile)=$($botEntry.secret_id)'\"",
            text,
        )
        self.assertNotIn("--secret-id '$SecretId'", text)
        self.assertIn(
            "spectate_port = $(if ($spectate) { $SpectatePort + $index }", text
        )
        self.assertEqual(
            2, text.count("bots = @($bots | ForEach-Object {\n            [ordered]@{")
        )

    def test_collector_keeps_per_bot_attribution_for_pass_and_fail(self) -> None:
        text = _COLLECTOR.read_text(encoding="utf-8")
        self.assertIn('"lisjong-arena-aws-riichilab-instance-run-summary"', text)
        self.assertIn("legacy_single_bot", text)
        failure = text[text.index('if ($status -ne "Success") {') :]
        failure = failure[: failure.index("throw ")]
        self.assertLess(
            failure.index("Write-JsonFile -Value $failedSummary"),
            failure.index("Ensure-InstanceTerminated"),
        )
        self.assertIn('if ([string]$summary.status -ne "PASS")', text)
        self.assertIn("$SecretIds | ForEach-Object", text)

    def test_watcher_selects_the_bot_port(self) -> None:
        text = _WATCHER.read_text(encoding="utf-8")
        self.assertIn('[string]$Bot = ""', text)
        self.assertIn('$state.PSObject.Properties["bots"]', text)
        self.assertIn("This run has several bots; pass -Bot", text)
        self.assertIn('$state.PSObject.Properties["spectate_port"]', text)


if __name__ == "__main__":
    unittest.main()
