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

from lisjong_arena.riichilab.aws_instance_run import (
    EXPECTED_POLICY_BY_PROFILE,
    INSTANCE_SUMMARY_SCHEMA_ID,
    MAX_BOTS,
    AwsInstanceRunConfigError,
    _run_cli,
    bot_directory,
    check_runtime_policies,
    parse_bot_configs,
    verify_instance_run,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT,
    RankedRecordProvenance,
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
    ) -> None:
        directory = bot_directory(self.root, profile)
        (directory / "records" / f"{profile}-game").mkdir(parents=True)
        self.records[f"{profile}-game"] = SimpleNamespace(
            record_identity=profile.ljust(64, "0"),
            provenance=_provenance(profile),
        )
        (directory / "continuous.log").write_text(
            _runner_log(profile, stopped_reason=stopped_reason, games=1) + log_extra,
            encoding="utf-8",
        )
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
        self.assertEqual(1, summary["schema_version"])
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
        rendered = json.dumps(summary)
        for token in _TOKENS.values():
            self.assertNotIn(token, rendered)

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
                "MechanismRiichiDefenseYakuhaiCallPolicy\t9000",
                "lisjong-baseline\tLISJONG_BASELINE_BOT_TOKEN\tMinimalPolicy\t9001",
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
