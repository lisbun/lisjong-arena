"""Issue #416: timing scalars on the event line, the bounded transport evidence
file, and the scalars reaching the AWS instance summary.

The ranked game itself is faked; no WebSocket connection is made.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisjong_arena.riichilab.aws_instance_run import (
    TRANSPORT_EVIDENCE_FILENAME,
    read_runner_facts,
)
from lisjong_arena.riichilab.continuous_ranked import (
    MAX_TRANSPORT_EVIDENCE_ENTRIES,
    TIMING_EVENT_FIELDS,
    TRANSPORT_EVIDENCE_SCHEMA_ID,
    ContinuousRunEvent,
    _run_cli,
    format_continuous_event,
    run_continuous_ranked,
    transport_evidence_record,
)
from lisjong_arena.riichilab.errors import TransportError, UnexpectedDisconnectError
from lisjong_arena.riichilab.transport_diagnostics import TransportDiagnostics
from lisjong_arena.riichilab.transport_timing import ConnectionTiming

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_TOKEN = "unit-test-evidence-token-0123456789abcdef"


def _timing_evidence():
    timing = ConnectionTiming(clock=lambda: 0.0)
    entry = timing.record_decision(
        request_id=64,
        time_budget={"grace_ms": 2000, "bank_ms": 10000},
        recv_at=100.0,
        start_at=100.01,
        end_at=122.5,
    )
    timing.record_send_attempt(entry, 122.51)
    timing.record_ack(64, "defaulted")
    timing.record_probe_tick(
        expected_at=99.0, actual_at=99.0, latency=0.08, state="OPEN"
    )
    timing.record_probe_tick(
        expected_at=100.5, actual_at=122.6, latency=0.08, state="CLOSING"
    )
    return timing.evidence()


def _diagnostics(timing=None, **fields) -> TransportDiagnostics:
    values = {
        "phase": "in_game",
        "operation": "send",
        "http_status": None,
        "close_code_received": None,
        "close_code_sent": 1011,
        "server_reason_class": "other",
        "server_reason_excerpt": "some server text",
        "local_close_reason_class": "keepalive_timeout",
        "requests_received": 65,
        "last_decision_elapsed_seconds": 8.507,
        "timing": timing,
    }
    values.update(fields)
    return TransportDiagnostics(**values)


def _event(diagnostics) -> ContinuousRunEvent:
    return ContinuousRunEvent(
        kind="transport_failure",
        elapsed_seconds=1135.94,
        profile="lisjong-dev",
        completed_games=3,
        failed_games=1,
        consecutive_failures=1,
        exception_type="TransportError",
        backoff_seconds=5.0,
        outcome="retry",
        transport=diagnostics,
    )


class EventLineTest(unittest.TestCase):
    def test_timing_scalars_follow_the_diagnostics_as_single_tokens(self) -> None:
        line = format_continuous_event(_event(_diagnostics(_timing_evidence())))

        self.assertTrue(
            line.endswith(
                "last_decision_elapsed_seconds=8.507 "
                "max_event_loop_lag_seconds=22.100 "
                "lag_at_close_seconds=22.100 "
                "lag_spans_keepalive_deadline=true "
                "max_lag_overlapping_decision_seconds=22.000 "
                "max_lag_outside_decision_seconds=0.100 "
                "max_recent_decision_seconds=22.490 "
                "max_recent_keepalive_latency_seconds=0.080 "
                "defaulted_acks=1 stale_acks=0"
            ),
            line,
        )

    def test_no_timing_fields_without_timing_evidence(self) -> None:
        line = format_continuous_event(_event(_diagnostics(None)))
        for name in TIMING_EVENT_FIELDS:
            self.assertNotIn(f"{name}=", line)


class EvidenceRecordTest(unittest.TestCase):
    def test_record_holds_no_server_text(self) -> None:
        record = transport_evidence_record(_event(_diagnostics(_timing_evidence())), 1)

        self.assertEqual(TRANSPORT_EVIDENCE_SCHEMA_ID, record["schema_id"])
        self.assertEqual(1, record["sequence"])
        transport = record["transport"]
        self.assertNotIn("server_reason_excerpt", transport)
        self.assertEqual("keepalive_timeout", transport["local_close_reason_class"])
        self.assertEqual(64, transport["timing"]["decisions"][0]["request_id"])
        self.assertNotIn("some server text", json.dumps(record))

    def test_record_without_diagnostics(self) -> None:
        event = _event(None)
        self.assertIsNone(transport_evidence_record(event, 2)["transport"])


class EvidenceCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / TRANSPORT_EVIDENCE_FILENAME

    def _cli(self, outcomes, *extra: str) -> tuple[int, str, str]:
        outcomes = iter(outcomes)
        original = run_continuous_ranked

        async def _fake_run_ranked_game(policy, token, **kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome

        async def _no_sleep(_delay: float) -> None:
            return None

        async def _runner(profile, token, **kwargs):
            return await original(profile, token, sleep=_no_sleep, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: _TOKEN}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _runner,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = _run_cli(["--profile", "lisjong-dev", *extra])
        self.assertNotIn(_TOKEN, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _failure(timing=True) -> TransportError:
        error = UnexpectedDisconnectError("dc")
        error.diagnostics = _diagnostics(_timing_evidence() if timing else None)
        return error

    def test_each_transport_failure_writes_one_bounded_line(self) -> None:
        outcomes = [self._failure(), None, TransportError("bare")] + [None] * 2
        code, stdout, _ = self._cli(
            outcomes, "--games", "3", "--transport-evidence", str(self.path)
        )

        self.assertEqual(0, code)
        self.assertIn("transport evidence: on", stdout)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        first, second = (json.loads(line) for line in lines)
        self.assertEqual([1, 2], [first["sequence"], second["sequence"]])
        self.assertTrue(first["transport"]["timing"]["lag_spans_keepalive_deadline"])
        self.assertIsNone(second["transport"])
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn(_TOKEN, content)
        self.assertNotIn("uthorization", content)

    def test_entries_are_capped(self) -> None:
        failures = MAX_TRANSPORT_EVIDENCE_ENTRIES + 3
        # Success in between keeps the consecutive failure budget from ending.
        outcomes: list = []
        for _ in range(failures):
            outcomes += [self._failure(), None]
        code, _, _ = self._cli(
            outcomes,
            "--games",
            str(failures),
            "--transport-evidence",
            str(self.path),
        )

        self.assertEqual(0, code)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(MAX_TRANSPORT_EVIDENCE_ENTRIES, len(lines))
        self.assertEqual(1, json.loads(lines[0])["sequence"])

    def test_existing_file_is_refused_before_any_game(self) -> None:
        self.path.write_text("keep\n", encoding="utf-8")
        code, stdout, stderr = self._cli([], "--transport-evidence", str(self.path))

        self.assertEqual(2, code)
        self.assertIn("--transport-evidence could not be created", stderr)
        self.assertNotIn("stopped reason", stdout)
        self.assertEqual("keep\n", self.path.read_text(encoding="utf-8"))

    def test_write_failure_is_reported_once_and_the_run_continues(self) -> None:
        outcomes = [self._failure(), self._failure(), None]

        def _remove_and_block(event_path=self.path):
            event_path.unlink()
            event_path.mkdir()  # appending now fails with an OSError

        original_format = format_continuous_event
        calls = {"n": 0}

        def _format(event):
            calls["n"] += 1
            if calls["n"] == 1:
                _remove_and_block()
            return original_format(event)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.format_continuous_event",
            _format,
        ):
            code, stdout, stderr = self._cli(
                outcomes, "--games", "1", "--transport-evidence", str(self.path)
            )

        self.assertEqual(0, code)
        self.assertIn("stopped reason: target_completed_games_reached", stdout)
        self.assertEqual(1, stderr.count("transport evidence write failed:"))

    def test_without_the_flag_nothing_is_written(self) -> None:
        code, stdout, _ = self._cli([self._failure(), None], "--games", "1")
        self.assertEqual(0, code)
        self.assertIn("transport evidence: off", stdout)
        self.assertEqual([], list(self.root.iterdir()))


class RunnerFactsTimingTest(unittest.TestCase):
    def test_timing_scalars_reach_runner_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "continuous.log"
            line = format_continuous_event(_event(_diagnostics(_timing_evidence())))
            log.write_text(line + "\n", encoding="utf-8")

            facts = read_runner_facts(log, (_TOKEN,))

        timing = facts["first_transport_failure_event"]["timing"]
        self.assertEqual(
            {
                "max_event_loop_lag_seconds": 22.1,
                "lag_at_close_seconds": 22.1,
                "lag_spans_keepalive_deadline": True,
                "max_lag_overlapping_decision_seconds": 22.0,
                "max_lag_outside_decision_seconds": 0.1,
                "max_recent_decision_seconds": 22.49,
                "max_recent_keepalive_latency_seconds": 0.08,
                "defaulted_acks": 1,
                "stale_acks": 0,
            },
            timing,
        )

    def test_malformed_timing_values_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "continuous.log"
            log.write_text(
                "continuous-event: kind=transport_failure elapsed_seconds=1.0 "
                "exception=TransportError max_event_loop_lag_seconds=-1 "
                "lag_spans_keepalive_deadline=maybe defaulted_acks=x "
                f"lag_at_close_seconds={_TOKEN}\n",
                encoding="utf-8",
            )
            (event,) = read_runner_facts(log, (_TOKEN,))["transport_failure_events"]

        timing = event["timing"]
        self.assertIsNone(timing["max_event_loop_lag_seconds"])
        self.assertIsNone(timing["lag_spans_keepalive_deadline"])
        self.assertIsNone(timing["defaulted_acks"])
        self.assertIsNone(timing["lag_at_close_seconds"])
        self.assertNotIn(_TOKEN, json.dumps(event))


if __name__ == "__main__":
    unittest.main()
