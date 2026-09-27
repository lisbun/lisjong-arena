"""Issue #386: several RiichiLab bots on one AWS instance.

Configuration checks and per-bot verification aggregation.  Durable record
loading is faked; no AWS or RiichiLab access happens here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong_arena.riichilab import continuous_ranked
from lisjong_arena.riichilab.aws_instance_run import (
    EXPECTED_POLICY_BY_PROFILE,
    INSTANCE_SUMMARY_SCHEMA_ID,
    INSTANCE_SUMMARY_SCHEMA_VERSION,
    MAX_BOTS,
    MAX_RUNNER_FACT_EVENTS,
    AwsInstanceRunConfigError,
    _run_cli,
    bot_directory,
    check_runtime_policies,
    parse_bot_configs,
    read_runner_facts,
    verify_instance_run,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT,
    RankedRecordProvenance,
)
from lisjong_arena.riichilab.errors import (
    ProtocolError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.profile import PROFILE_NAMES

_ARENA_REVISION = "2" * 40
_DEV = "lisjong-dev=lisjong/riichilab/dev-token"
_BASELINE = "lisjong-baseline=lisjong/riichilab/baseline-token"
_TOKENS = {
    "lisjong-dev": "dev-runtime-token-that-must-never-be-persisted",
    "lisjong-baseline": "baseline-runtime-token-that-must-never-be-persisted",
}
_START = "2026-09-20T00:00:00Z"


def _provenance(profile: str) -> RankedRecordProvenance:
    return RankedRecordProvenance(
        execution_environment=RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT,
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=_ARENA_REVISION,
        lisjong_version="0.1.0",
        lisjong_revision="1" * 40,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision="3" * 40,
        riichienv_version="0.4.10",
        python_version="3.14.7",
        python_implementation="CPython",
        profile_identity=profile,
        policy_identity=EXPECTED_POLICY_BY_PROFILE[profile],
    )


def _runner_log(profile: str, *, stopped_reason: str, games: int) -> str:
    return "\n".join(
        [
            f"profile: {profile}",
            "requested completed games: unbounded",
            "requested duration seconds: unbounded",
            f"completed games: {games}",
            "failed games: 0",
            "consecutive failures: 0",
            "last failure type: none",
            "records: on",
            f"stopped reason: {stopped_reason}",
            "",
        ]
    )


def _real_runner_log(profile: str, outcomes: list[object]) -> tuple[int, str]:
    """Run the real continuous CLI against faked games; return (exit, log).

    stdout and stderr are combined as the bootstrap redirects both into
    ``continuous.log``.  Backoff sleeps are skipped.
    """

    games = iter(outcomes)
    original = continuous_ranked.run_continuous_ranked

    async def _fake_game(policy, token, **kwargs):
        outcome = next(games)
        if isinstance(outcome, BaseException):
            raise outcome

    async def _no_sleep(_delay: float) -> None:
        return None

    async def _runner(runtime_profile, token, **kwargs):
        return await original(runtime_profile, token, sleep=_no_sleep, **kwargs)

    env_var = "LISJONG_DEV_BOT_TOKEN"
    if profile == "lisjong-baseline":
        env_var = "LISJONG_BASELINE_BOT_TOKEN"
    output = io.StringIO()
    with (
        patch.dict(os.environ, {env_var: _TOKENS[profile]}),
        patch.object(continuous_ranked, "run_ranked_game", _fake_game),
        patch.object(continuous_ranked, "run_continuous_ranked", _runner),
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(output),
    ):
        code = continuous_ranked._run_cli(["--profile", profile])
    return code, output.getvalue()


class ParseBotConfigsTest(unittest.TestCase):
    def test_bots_keep_launch_order_and_get_deterministic_ports(self) -> None:
        bots = parse_bot_configs([_DEV, _BASELINE], spectate_base_port=8765)
        self.assertEqual(["lisjong-dev", "lisjong-baseline"], [b.profile for b in bots])
        self.assertEqual([8765, 8766], [b.spectate_port for b in bots])
        self.assertEqual(
            ["LISJONG_DEV_BOT_TOKEN", "LISJONG_BASELINE_BOT_TOKEN"],
            [b.credential_env_var for b in bots],
        )
        without = parse_bot_configs([_DEV], spectate_base_port=None)
        self.assertIsNone(without[0].spectate_port)

    def test_expected_policies_cover_exactly_the_runtime_profiles(self) -> None:
        self.assertEqual(set(PROFILE_NAMES), set(EXPECTED_POLICY_BY_PROFILE))
        check_runtime_policies(
            parse_bot_configs(
                [f"{name}=secret/{name}" for name in PROFILE_NAMES],
                spectate_base_port=None,
            )
        )

    def test_runtime_policy_mismatch_fails_closed(self) -> None:
        bots = parse_bot_configs([_DEV], spectate_base_port=None)
        with patch.dict(
            "lisjong_arena.riichilab.aws_instance_run.EXPECTED_POLICY_BY_PROFILE",
            {"lisjong-dev": "MinimalPolicy"},
        ):
            with self.assertRaisesRegex(AwsInstanceRunConfigError, "does not build"):
                check_runtime_policies(bots)

    def test_unsafe_configurations_are_rejected(self) -> None:
        cases = (
            ("at least one", [], None),
            (f"at most {MAX_BOTS}", [f"x{i}=s{i}" for i in range(MAX_BOTS + 1)], None),
            ("PROFILE=SECRET_ID", ["lisjong-dev"], None),
            ("unsupported profile", ["other=secret"], None),
            ("invalid secret id", ["lisjong-dev=a'b"], None),
            ("duplicate bot profile", [_DEV, "lisjong-dev=other"], None),
            (
                "duplicate bot secret id",
                [_DEV, "lisjong-baseline=lisjong/riichilab/dev-token"],
                None,
            ),
            ("spectate ports", [_DEV, _BASELINE], 65535),
            ("spectate ports", [_DEV], 80),
        )
        for message, specs, port in cases:
            with self.subTest(message=message, port=port):
                with self.assertRaisesRegex(AwsInstanceRunConfigError, message):
                    parse_bot_configs(specs, spectate_base_port=port)


class VerifyInstanceRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bots = parse_bot_configs([_DEV, _BASELINE], spectate_base_port=8765)
        self.stop_file = self.root / "stop-requested"
        self.records: dict[str, SimpleNamespace] = {}

    def _bot(
        self,
        profile: str,
        *,
        exit_code: int = 0,
        stop_utc: str = "2026-09-20T03:00:00Z",
        stopped_reason: str = "stop_requested",
        log_extra: str = "",
        log: str | None = None,
    ) -> None:
        directory = bot_directory(self.root, profile)
        (directory / "records" / f"{profile}-game").mkdir(parents=True)
        self.records[f"{profile}-game"] = SimpleNamespace(
            record_identity=profile.ljust(64, "0"),
            provenance=_provenance(profile),
        )
        if log is None:
            log = _runner_log(profile, stopped_reason=stopped_reason, games=1)
        (directory / "continuous.log").write_text(log + log_extra, encoding="utf-8")
        (directory / "exit_code").write_text(f"{exit_code}\n", encoding="ascii")
        (directory / "stop_utc").write_text(f"{stop_utc}\n", encoding="ascii")

    def _verify(self, **overrides) -> dict:
        kwargs = {
            "work_root": self.root,
            "bots": self.bots,
            "tokens_by_profile": dict(_TOKENS),
            "expected_arena_revision": _ARENA_REVISION,
            "expected_duration_seconds": None,
            "start_utc": _START,
            "cutoff_utc": None,
            "stop_file": self.stop_file,
        }
        kwargs.update(overrides)
        with patch(
            "lisjong_arena.riichilab.aws_run_verify.load_ranked_game_record",
            side_effect=lambda path: self.records[path.name],
        ):
            return verify_instance_run(**kwargs)

    def test_every_bot_passes_and_is_attributed(self) -> None:
        self.stop_file.write_text("operator\n", encoding="ascii")
        self._bot("lisjong-dev", stop_utc="2026-09-20T03:00:00Z")
        self._bot("lisjong-baseline", stop_utc="2026-09-20T03:20:00Z")

        summary = self._verify()

        self.assertEqual(INSTANCE_SUMMARY_SCHEMA_ID, summary["schema_id"])
        self.assertEqual(2, INSTANCE_SUMMARY_SCHEMA_VERSION)
        self.assertEqual(INSTANCE_SUMMARY_SCHEMA_VERSION, summary["schema_version"])
        self.assertEqual("PASS", summary["status"])
        self.assertEqual(
            ["lisjong-dev", "lisjong-baseline"], summary["configured_bots"]
        )
        self.assertEqual(2, summary["passed_bot_count"])
        self.assertEqual("operator", summary["stop_request_source"])
        self.assertEqual("2026-09-20T03:20:00Z", summary["stop_utc"])
        self.assertTrue(summary["until_stopped"])
        by_profile = {entry["profile"]: entry for entry in summary["bots"]}
        self.assertEqual(8766, by_profile["lisjong-baseline"]["spectate_port"])
        self.assertEqual(
            "lisjong/riichilab/baseline-token",
            by_profile["lisjong-baseline"]["secret_id"],
        )
        self.assertEqual(
            "lisjong-baseline",
            by_profile["lisjong-baseline"]["verification"]["provenance"][
                "profile_identity"
            ],
        )
        self.assertEqual(
            10800, by_profile["lisjong-dev"]["verification"]["actual_elapsed_seconds"]
        )
        facts = by_profile["lisjong-dev"]["runner_facts"]
        self.assertEqual("summary", facts["source"])
        self.assertEqual(1, facts["completed_games"])
        self.assertEqual("stop_requested", facts["stopped_reason"])
        rendered = json.dumps(summary)
        for token in _TOKENS.values():
            self.assertNotIn(token, rendered)

    def test_budget_exhausted_bot_keeps_its_facts_while_another_drains(
        self,
    ) -> None:
        """The #404 run shape: baseline fails, dev finishes its hanchan and stops."""

        code, log = _real_runner_log(
            "lisjong-baseline",
            [None, None] + [UnexpectedDisconnectError("dc")] * 5,
        )
        self.assertEqual(1, code)
        self.stop_file.write_text("bot-exited:lisjong-baseline\n", encoding="ascii")
        self._bot("lisjong-dev")
        self._bot("lisjong-baseline", exit_code=1, log=log)

        summary = self._verify()

        self.assertEqual("FAIL", summary["status"])
        self.assertEqual(1, summary["passed_bot_count"])
        dev, baseline = summary["bots"]
        self.assertEqual("PASS", dev["status"])
        self.assertEqual("FAIL", baseline["status"])
        self.assertEqual("bot runner exited with code 1", baseline["failure_reason"])
        facts = baseline["runner_facts"]
        self.assertEqual("summary", facts["source"])
        self.assertEqual(2, facts["completed_games"])
        self.assertEqual(5, facts["failed_games"])
        self.assertEqual(5, facts["final_consecutive_failures"])
        self.assertEqual("UnexpectedDisconnectError", facts["last_failure_type"])
        self.assertEqual("failure_budget_exhausted", facts["stopped_reason"])
        self.assertIsNone(facts["terminal_exception_type"])
        self.assertEqual(5, facts["transport_failure_event_count"])
        self.assertEqual(
            [
                (1, 5.0, "retry"),
                (2, 10.0, "retry"),
                (3, 20.0, "retry"),
                (4, 40.0, "retry"),
                (5, None, "failure_budget_exhausted"),
            ],
            [
                (e["consecutive_failures"], e["backoff_seconds"], e["outcome"])
                for e in facts["transport_failure_events"]
            ],
        )
        rendered = json.dumps(summary)
        for token in _TOKENS.values():
            self.assertNotIn(token, rendered)

    def test_client_error_before_a_normal_summary_keeps_terminal_facts(
        self,
    ) -> None:
        code, log = _real_runner_log(
            "lisjong-baseline",
            [None, TransportError("boom"), ProtocolError("raw protocol payload")],
        )
        self.assertEqual(1, code)
        self.stop_file.write_text("bot-exited:lisjong-baseline\n", encoding="ascii")
        self._bot("lisjong-dev")
        self._bot("lisjong-baseline", exit_code=1, log=log)

        summary = self._verify()

        facts = summary["bots"][1]["runner_facts"]
        self.assertEqual("terminal", facts["source"])
        self.assertEqual(1, facts["completed_games"])
        self.assertEqual(1, facts["failed_games"])
        self.assertEqual(1, facts["final_consecutive_failures"])
        self.assertEqual("TransportError", facts["last_failure_type"])
        self.assertEqual("runner_exception", facts["stopped_reason"])
        self.assertEqual("ProtocolError", facts["terminal_exception_type"])
        self.assertEqual("riichilab_client", facts["terminal_exception_category"])
        self.assertEqual(1, facts["transport_failure_event_count"])
        self.assertNotIn("raw protocol payload", json.dumps(summary))

    def test_one_failing_bot_is_not_hidden_by_a_passing_one(self) -> None:
        self.stop_file.write_text("bot-exited:lisjong-dev\n", encoding="ascii")
        self._bot("lisjong-dev", exit_code=1, stopped_reason="failure_budget_exhausted")
        self._bot("lisjong-baseline")

        summary = self._verify()

        self.assertEqual("FAIL", summary["status"])
        self.assertEqual(1, summary["passed_bot_count"])
        self.assertEqual("bot-exited:lisjong-dev", summary["stop_request_source"])
        dev, baseline = summary["bots"]
        self.assertEqual("FAIL", dev["status"])
        self.assertEqual(1, dev["exit_code"])
        self.assertEqual("bot runner exited with code 1", dev["failure_reason"])
        self.assertEqual("PASS", baseline["status"])

    def test_another_bots_token_in_a_bots_evidence_fails_that_bot(self) -> None:
        self.stop_file.write_text("operator\n", encoding="ascii")
        self._bot("lisjong-dev", log_extra=_TOKENS["lisjong-baseline"])
        self._bot("lisjong-baseline")

        summary = self._verify()

        self.assertEqual("FAIL", summary["status"])
        dev = summary["bots"][0]
        self.assertEqual("runtime token bytes were persisted", dev["failure_reason"])
        self.assertNotIn(_TOKENS["lisjong-baseline"], json.dumps(summary))

    def test_missing_bot_evidence_fails_that_bot(self) -> None:
        self.stop_file.write_text("operator\n", encoding="ascii")
        self._bot("lisjong-dev")
        (bot_directory(self.root, "lisjong-baseline") / "records").mkdir(parents=True)

        summary = self._verify()

        self.assertEqual("FAIL", summary["status"])
        self.assertEqual(
            "bot exit evidence is missing or unreadable",
            summary["bots"][1]["failure_reason"],
        )

    def test_unrecognized_stop_file_fails_the_instance(self) -> None:
        self.stop_file.write_text("someone\n", encoding="ascii")
        self._bot("lisjong-dev", stopped_reason="duration_reached")
        self._bot("lisjong-baseline", stopped_reason="duration_reached")

        summary = self._verify(
            expected_duration_seconds=10800, cutoff_utc="2026-09-20T03:00:00Z"
        )

        self.assertEqual("FAIL", summary["status"])
        self.assertEqual(
            "stop file content is not recognized", summary["instance_failure_reason"]
        )
        self.assertIsNone(summary["stop_request_source"])

    def test_tokens_must_be_present_and_distinct(self) -> None:
        cases = {
            "missing": {"lisjong-dev": _TOKENS["lisjong-dev"]},
            "empty": {**_TOKENS, "lisjong-baseline": ""},
            "shared": {"lisjong-dev": "same", "lisjong-baseline": "same"},
        }
        for name, tokens in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(AwsInstanceRunConfigError):
                    self._verify(tokens_by_profile=tokens)


class ReadRunnerFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = Path(self.enterContext(tempfile.TemporaryDirectory())) / "log"

    def test_missing_log_has_no_facts(self) -> None:
        self.assertIsNone(read_runner_facts(self.log))

    def test_killed_runner_falls_back_to_the_latest_event(self) -> None:
        self.log.write_text(
            "profile: lisjong-dev\n"
            "continuous-event: kind=game_completed elapsed_seconds=900.0 "
            "profile=lisjong-dev completed_games=1 failed_games=0 "
            "consecutive_failures=0\n"
            "continuous-event: kind=transport_failure elapsed_seconds=1000.5 "
            "profile=lisjong-dev completed_games=1 failed_games=1 "
            "consecutive_failures=1 exception=UnexpectedDisconnectError "
            "backoff_seconds=5 outcome=retry\n",
            encoding="utf-8",
        )

        facts = read_runner_facts(self.log)

        self.assertEqual("events", facts["source"])
        self.assertEqual(1, facts["completed_games"])
        self.assertEqual(1, facts["failed_games"])
        self.assertEqual("UnexpectedDisconnectError", facts["last_failure_type"])
        self.assertIsNone(facts["stopped_reason"])
        self.assertEqual(
            [
                {
                    "elapsed_seconds": 1000.5,
                    "exception_type": "UnexpectedDisconnectError",
                    "consecutive_failures": 1,
                    "backoff_seconds": 5.0,
                    "outcome": "retry",
                }
            ],
            facts["transport_failure_events"],
        )

    def test_values_that_are_not_strict_identifiers_are_dropped(self) -> None:
        self.log.write_text(
            "terminal completed games: 1 extra\n"
            "terminal exception type: Bearer secret-value\n"
            "terminal exception category: riichilab_client\n"
            "terminal stopped reason: runner_exception\n"
            "continuous-event: kind=transport_failure elapsed_seconds=x "
            "exception=Authorization:abc backoff_seconds=5 outcome=retry\n",
            encoding="utf-8",
        )

        facts = read_runner_facts(self.log)

        self.assertIsNone(facts["completed_games"])
        self.assertIsNone(facts["terminal_exception_type"])
        self.assertEqual("riichilab_client", facts["terminal_exception_category"])
        (event,) = facts["transport_failure_events"]
        self.assertIsNone(event["elapsed_seconds"])
        self.assertIsNone(event["exception_type"])
        rendered = json.dumps(facts)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_only_the_latest_events_are_kept(self) -> None:
        line = (
            "continuous-event: kind=transport_failure elapsed_seconds={n}.0 "
            "profile=lisjong-dev completed_games=0 failed_games={n} "
            "consecutive_failures=1 exception=TransportError backoff_seconds=5 "
            "outcome=retry\n"
        )
        total = MAX_RUNNER_FACT_EVENTS + 3
        self.log.write_text(
            "".join(line.format(n=n) for n in range(1, total + 1)), encoding="utf-8"
        )

        facts = read_runner_facts(self.log)

        self.assertEqual(total, facts["transport_failure_event_count"])
        events = facts["transport_failure_events"]
        self.assertEqual(MAX_RUNNER_FACT_EVENTS, len(events))
        self.assertEqual(float(total), events[-1]["elapsed_seconds"])


class InstanceRunCliTest(unittest.TestCase):
    def test_check_config_prints_one_line_per_bot(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = _run_cli(
                [
                    "check-config",
                    "--bot",
                    _DEV,
                    "--bot",
                    _BASELINE,
                    "--spectate-base-port",
                    "9000",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual(
            [
                "lisjong-dev\tLISJONG_DEV_BOT_TOKEN\t"
                "PlacementAwareSpeedCallPolicy\t9000",
                "lisjong-baseline\tLISJONG_BASELINE_BOT_TOKEN\t"
                "PlacementAwareSpeedCallPolicy\t9001",
            ],
            stdout.getvalue().splitlines(),
        )

    def test_rejected_configuration_exits_2_without_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = _run_cli(["check-config", "--bot", _DEV, "--bot", _DEV])
        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("duplicate bot profile", stderr.getvalue())

    def test_verify_reads_each_bots_token_from_its_own_variable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "stop-requested").write_text("operator\n", encoding="ascii")
            argv = [
                "verify",
                "--bot",
                _DEV,
                "--bot",
                _BASELINE,
                "--work-root",
                raw,
                "--expected-arena-revision",
                _ARENA_REVISION,
                "--until-stopped",
                "--start-utc",
                _START,
                "--stop-file",
                str(root / "stop-requested"),
            ]
            env = {
                "LISJONG_DEV_BOT_TOKEN": _TOKENS["lisjong-dev"],
                "LISJONG_BASELINE_BOT_TOKEN": _TOKENS["lisjong-baseline"],
            }
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, env, clear=True),
                contextlib.redirect_stdout(stdout),
            ):
                code = _run_cli(argv)
            # No bot evidence exists, so both bots FAIL; the summary still prints.
            self.assertEqual(1, code)
            summary = json.loads(stdout.getvalue())
            self.assertEqual("FAIL", summary["status"])
            self.assertEqual(0, summary["passed_bot_count"])

            with (
                patch.dict(os.environ, {"LISJONG_DEV_BOT_TOKEN": "x"}, clear=True),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, _run_cli(argv))


if __name__ == "__main__":
    unittest.main()
