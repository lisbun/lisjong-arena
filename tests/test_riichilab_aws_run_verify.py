from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong_arena.riichilab.aws_run_verify import (
    AwsRunVerificationError,
    verify_run,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT,
    RankedRecordProvenance,
)

_ARENA_REVISION = "2" * 40
_TOKEN = "test-runtime-token-value-that-must-never-be-persisted"


def _provenance() -> RankedRecordProvenance:
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
        profile_identity="lisjong-dev",
        policy_identity="MechanismRiichiDefenseYakuhaiCallPolicy",
    )


def _runner_log(
    *,
    stopped_reason: str = "duration_reached",
    duration: str = "43200",
    completed_games: int = 2,
) -> str:
    return "\n".join(
        [
            "profile: lisjong-dev",
            "mode: ranked-continuous",
            "trace: off",
            "records: on",
            "requested completed games: unbounded",
            f"requested duration seconds: {duration}",
            "stop file: on",
            "profile: lisjong-dev",
            "requested completed games: unbounded",
            f"requested duration seconds: {duration}",
            f"completed games: {completed_games}",
            "failed games: 1",
            "consecutive failures: 0",
            "last failure type: UnexpectedDisconnectError",
            "records: on",
            f"stopped reason: {stopped_reason}",
            "",
        ]
    )


class AwsRunVerifierTest(unittest.TestCase):
    def _layout(self, root: Path, *, log: str | None = None) -> tuple[Path, Path]:
        record_dir = root / "records"
        record_dir.mkdir()
        (record_dir / "record-a").mkdir()
        (record_dir / "record-b").mkdir()
        runner_log = root / "continuous.log"
        runner_log.write_text(log or _runner_log(), encoding="utf-8")
        return record_dir, runner_log

    def test_valid_run_returns_secret_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record_dir, runner_log = self._layout(Path(raw))
            records = {
                "record-a": SimpleNamespace(
                    record_identity="a" * 64,
                    provenance=_provenance(),
                ),
                "record-b": SimpleNamespace(
                    record_identity="b" * 64,
                    provenance=_provenance(),
                ),
            }

            with patch(
                "lisjong_arena.riichilab.aws_run_verify.load_ranked_game_record",
                side_effect=lambda path: records[path.name],
            ):
                summary = verify_run(
                    record_dir=record_dir,
                    runner_log=runner_log,
                    expected_arena_revision=_ARENA_REVISION,
                    expected_profile="lisjong-dev",
                    expected_policy="MechanismRiichiDefenseYakuhaiCallPolicy",
                    expected_duration_seconds=43200,
                    token=_TOKEN,
                    start_utc="2026-09-20T00:00:00Z",
                    cutoff_utc="2026-09-20T12:00:00Z",
                    stop_utc="2026-09-20T12:04:00Z",
                    elapsed_seconds=43440,
                )

        self.assertEqual("PASS", summary["status"])
        self.assertEqual(2, summary["record_count"])
        self.assertEqual(2, summary["strict_readback_pass_count"])
        self.assertEqual(0, summary["strict_readback_failure_count"])
        self.assertTrue(summary["retry_or_disconnect_occurred"])
        self.assertEqual("duration_reached", summary["stopped_reason"])
        self.assertNotIn(_TOKEN, str(summary))

    def test_non_duration_stop_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record_dir, runner_log = self._layout(
                Path(raw), log=_runner_log(stopped_reason="failure_budget_exhausted")
            )
            with self.assertRaisesRegex(AwsRunVerificationError, "duration"):
                verify_run(
                    record_dir=record_dir,
                    runner_log=runner_log,
                    expected_arena_revision=_ARENA_REVISION,
                    expected_profile="lisjong-dev",
                    expected_policy="MechanismRiichiDefenseYakuhaiCallPolicy",
                    expected_duration_seconds=43200,
                    token=_TOKEN,
                    start_utc="2026-09-20T00:00:00Z",
                    cutoff_utc="2026-09-20T12:00:00Z",
                    stop_utc="2026-09-20T12:04:00Z",
                    elapsed_seconds=43440,
                )

    def test_exact_runtime_token_in_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record_dir, runner_log = self._layout(Path(raw))
            (record_dir / "record-a" / "protocol.jsonl").write_text(
                f'{{"leak":"{_TOKEN}"}}\n',
                encoding="utf-8",
            )
            records = {
                "record-a": SimpleNamespace(
                    record_identity="a" * 64,
                    provenance=_provenance(),
                ),
                "record-b": SimpleNamespace(
                    record_identity="b" * 64,
                    provenance=_provenance(),
                ),
            }
            with (
                patch(
                    "lisjong_arena.riichilab.aws_run_verify.load_ranked_game_record",
                    side_effect=lambda path: records[path.name],
                ),
                self.assertRaisesRegex(AwsRunVerificationError, "token"),
            ):
                verify_run(
                    record_dir=record_dir,
                    runner_log=runner_log,
                    expected_arena_revision=_ARENA_REVISION,
                    expected_profile="lisjong-dev",
                    expected_policy="MechanismRiichiDefenseYakuhaiCallPolicy",
                    expected_duration_seconds=43200,
                    token=_TOKEN,
                    start_utc="2026-09-20T00:00:00Z",
                    cutoff_utc="2026-09-20T12:00:00Z",
                    stop_utc="2026-09-20T12:04:00Z",
                    elapsed_seconds=43440,
                )

    def test_inconsistent_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record_dir, runner_log = self._layout(Path(raw))
            other = replace(_provenance(), lisjong_engine_revision="4" * 40)
            records = {
                "record-a": SimpleNamespace(
                    record_identity="a" * 64,
                    provenance=_provenance(),
                ),
                "record-b": SimpleNamespace(
                    record_identity="b" * 64,
                    provenance=other,
                ),
            }
            with (
                patch(
                    "lisjong_arena.riichilab.aws_run_verify.load_ranked_game_record",
                    side_effect=lambda path: records[path.name],
                ),
                self.assertRaisesRegex(AwsRunVerificationError, "provenance"),
            ):
                verify_run(
                    record_dir=record_dir,
                    runner_log=runner_log,
                    expected_arena_revision=_ARENA_REVISION,
                    expected_profile="lisjong-dev",
                    expected_policy="MechanismRiichiDefenseYakuhaiCallPolicy",
                    expected_duration_seconds=43200,
                    token=_TOKEN,
                    start_utc="2026-09-20T00:00:00Z",
                    cutoff_utc="2026-09-20T12:00:00Z",
                    stop_utc="2026-09-20T12:04:00Z",
                    elapsed_seconds=43440,
                )


if __name__ == "__main__":
    unittest.main()


class AwsRunOperatorStopTest(unittest.TestCase):
    """Issue #383: operator stop requests and until-stopped runs."""

    def _records(self) -> dict[str, SimpleNamespace]:
        return {
            "record-a": SimpleNamespace(
                record_identity="a" * 64, provenance=_provenance()
            ),
            "record-b": SimpleNamespace(
                record_identity="b" * 64, provenance=_provenance()
            ),
        }

    def _verify(
        self,
        root: Path,
        *,
        log: str,
        record_names: tuple[str, ...] = ("record-a", "record-b"),
        create_stop_file: bool = True,
        stop_file_content: str = "operator\n",
        expected_duration_seconds: int | None = None,
        cutoff_utc: str | None = None,
        pass_stop_file: bool = True,
    ) -> dict:
        record_dir = root / "records"
        record_dir.mkdir()
        for name in record_names:
            (record_dir / name).mkdir()
        runner_log = root / "continuous.log"
        runner_log.write_text(log, encoding="utf-8")
        stop_file = root / "stop-requested"
        if create_stop_file:
            stop_file.write_text(stop_file_content, encoding="ascii")
        records = self._records()
        with patch(
            "lisjong_arena.riichilab.aws_run_verify.load_ranked_game_record",
            side_effect=lambda path: records[path.name],
        ):
            return verify_run(
                record_dir=record_dir,
                runner_log=runner_log,
                expected_arena_revision=_ARENA_REVISION,
                expected_profile="lisjong-dev",
                expected_policy="MechanismRiichiDefenseYakuhaiCallPolicy",
                expected_duration_seconds=expected_duration_seconds,
                token=_TOKEN,
                start_utc="2026-09-20T00:00:00Z",
                cutoff_utc=cutoff_utc,
                stop_utc="2026-09-20T03:00:00Z",
                elapsed_seconds=10800,
                stop_file=stop_file if pass_stop_file else None,
            )

    def test_until_stopped_run_with_operator_stop_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            summary = self._verify(
                Path(raw),
                log=_runner_log(stopped_reason="stop_requested", duration="unbounded"),
            )
        self.assertEqual("PASS", summary["status"])
        self.assertEqual(2, summary["schema_version"])
        self.assertEqual("stop_requested", summary["stopped_reason"])
        self.assertTrue(summary["operator_stop_requested"])
        self.assertEqual("operator", summary["stop_request_source"])
        self.assertIsNone(summary["requested_duration_seconds"])
        self.assertIsNone(summary["cutoff_utc"])
        self.assertEqual(2, summary["record_count"])

    def test_stop_after_another_bot_exited_passes_and_names_the_bot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            summary = self._verify(
                Path(raw),
                log=_runner_log(stopped_reason="stop_requested", duration="unbounded"),
                stop_file_content="bot-exited:lisjong-baseline\n",
            )
        self.assertEqual("bot-exited:lisjong-baseline", summary["stop_request_source"])
        self.assertFalse(summary["operator_stop_requested"])

    def test_duration_stop_records_the_supervisor_stop_request(self) -> None:
        # The supervisor writes the stop file after the bot itself exits.
        with tempfile.TemporaryDirectory() as raw:
            summary = self._verify(
                Path(raw),
                log=_runner_log(duration="10800"),
                stop_file_content="bot-exited:lisjong-dev\n",
                expected_duration_seconds=10800,
                cutoff_utc="2026-09-20T03:00:00Z",
            )
        self.assertEqual("duration_reached", summary["stopped_reason"])
        self.assertFalse(summary["operator_stop_requested"])

    def test_unrecognized_stop_file_content_is_rejected(self) -> None:
        for content in ("", "operator please\n", "bot-exited:\n", "bot-exited:a b"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as raw:
                with self.assertRaisesRegex(AwsRunVerificationError, "stop file"):
                    self._verify(
                        Path(raw),
                        log=_runner_log(
                            stopped_reason="stop_requested", duration="unbounded"
                        ),
                        stop_file_content=content,
                    )

    def test_stop_requested_without_stop_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(AwsRunVerificationError, "stop file"):
                self._verify(
                    Path(raw),
                    log=_runner_log(
                        stopped_reason="stop_requested", duration="unbounded"
                    ),
                    create_stop_file=False,
                )

    def test_until_stopped_run_requires_a_stop_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(AwsRunVerificationError, "stop file"):
                self._verify(
                    Path(raw),
                    log=_runner_log(
                        stopped_reason="stop_requested", duration="unbounded"
                    ),
                    pass_stop_file=False,
                )

    def test_until_stopped_run_rejects_duration_or_failure_stop(self) -> None:
        for reason in ("duration_reached", "failure_budget_exhausted"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as raw:
                with self.assertRaises(AwsRunVerificationError):
                    self._verify(
                        Path(raw),
                        log=_runner_log(stopped_reason=reason, duration="unbounded"),
                    )

    def test_until_stopped_run_rejects_bounded_runner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(AwsRunVerificationError, "duration"):
                self._verify(
                    Path(raw),
                    log=_runner_log(stopped_reason="stop_requested"),
                )

    def test_operator_stop_before_duration_passes_without_elapsed_bound(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            summary = self._verify(
                Path(raw),
                log=_runner_log(stopped_reason="stop_requested"),
                expected_duration_seconds=43200,
                cutoff_utc="2026-09-20T12:00:00Z",
            )
        self.assertEqual("stop_requested", summary["stopped_reason"])
        self.assertEqual(43200, summary["requested_duration_seconds"])

    def test_operator_stop_before_first_hanchan_passes_with_zero_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            summary = self._verify(
                Path(raw),
                log=_runner_log(
                    stopped_reason="stop_requested",
                    duration="unbounded",
                    completed_games=0,
                ),
                record_names=(),
            )
        self.assertEqual(0, summary["record_count"])
        self.assertIsNone(summary["provenance"])

    def test_cutoff_must_match_duration_presence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(AwsRunVerificationError, "cutoff"):
                self._verify(
                    Path(raw),
                    log=_runner_log(
                        stopped_reason="stop_requested", duration="unbounded"
                    ),
                    cutoff_utc="2026-09-20T12:00:00Z",
                )
