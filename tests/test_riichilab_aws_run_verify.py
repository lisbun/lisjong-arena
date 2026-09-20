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


def _runner_log(*, stopped_reason: str = "duration_reached") -> str:
    return "\n".join(
        [
            "profile: lisjong-dev",
            "mode: ranked-continuous",
            "trace: off",
            "records: on",
            "requested completed games: unbounded",
            "requested duration seconds: 43200",
            "profile: lisjong-dev",
            "requested completed games: unbounded",
            "requested duration seconds: 43200",
            "completed games: 2",
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
