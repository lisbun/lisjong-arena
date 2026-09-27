"""Original-game wait after a mid-game disconnect (Issue #419).

After an in-game transport failure, an explicit `same_bot_already_active`
reconnect rejection means the original RiichiLab game still owns the bot. Such
rejections must not spend the ordinary failure budget nor count as failed
games; the wait has its own finite bound. The one-game boundary is faked and
no real sleep happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import unittest
from unittest.mock import patch

from lisjong_arena.riichilab.continuous_ranked import (
    ContinuousRunEvent,
    _run_cli,
    format_continuous_event,
    format_continuous_summary,
    run_continuous_ranked,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    DurableRankedGameRecordError,
)
from lisjong_arena.riichilab.errors import (
    ProtocolError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.profile import RuntimeProfile
from lisjong_arena.riichilab.transport_diagnostics import TransportDiagnostics

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_TOKEN = "unit-test-dummy-token-should-not-leak"
#: Seconds the fake clock advances per one-game attempt.
_GAME_SECONDS = 30.0


def _profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="unit-test-profile",
        credential_env_var=_DEV_TOKEN_VAR,
        policy_factory=object,
        runtime_namespace="unit-test-profile",
    )


def _diagnosed(error: TransportError, **fields: object) -> TransportError:
    values: dict[str, object] = {
        "phase": "before_start_game",
        "operation": "recv",
        "http_status": None,
        "close_code_received": None,
        "close_code_sent": None,
        "server_reason_class": "none",
        "server_reason_excerpt": None,
        "local_close_reason_class": "none",
        "requests_received": 0,
        "last_decision_elapsed_seconds": None,
        **fields,
    }
    error.diagnostics = TransportDiagnostics(**values)
    return error


def _in_game() -> TransportError:
    return _diagnosed(
        UnexpectedDisconnectError("dc"),
        phase="in_game",
        close_code_received=1011,
        server_reason_class="keepalive_timeout",
        requests_received=40,
    )


def _same_bot() -> TransportError:
    return _diagnosed(
        TransportError("rejected"),
        server_reason_class="same_bot_already_active",
        server_reason_excerpt="This bot is already connected to a game",
    )


def _connect_failure() -> TransportError:
    return _diagnosed(
        TransportError("connect"),
        phase="connect",
        operation="connect",
        server_reason_class="other",
    )


def _token_rejected() -> TransportError:
    return _diagnosed(
        TransportError("rejected"),
        phase="connect",
        operation="connect",
        http_status=401,
        server_reason_class="token_or_bot_rejected",
    )


class _Harness:
    """Runs the runner on scripted outcomes with a fake clock and sleep."""

    def __init__(self, outcomes, *, stop_after_calls: int | None = None) -> None:
        self.outcomes = iter(outcomes)
        self.clock = 0.0
        self.calls = 0
        self.delays: list[float] = []
        self.events: list[ContinuousRunEvent] = []
        self.stop_after_calls = stop_after_calls

    async def _game(self, policy, token, **kwargs) -> None:
        self.calls += 1
        self.clock += _GAME_SECONDS
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome

    async def _sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock += delay

    def _stop_requested(self) -> bool:
        return self.stop_after_calls is not None and self.calls >= self.stop_after_calls

    def run(self, **kwargs):
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game", self._game
        ):
            return asyncio.run(
                run_continuous_ranked(
                    _profile(),
                    _TOKEN,
                    sleep=self._sleep,
                    monotonic=lambda: self.clock,
                    stop_requested=self._stop_requested,
                    on_event=self.events.append,
                    **kwargs,
                )
            )


class OriginalGameWaitTest(unittest.TestCase):
    def test_in_game_failure_enters_wait_and_rejections_spend_no_budget(
        self,
    ) -> None:
        harness = _Harness([_in_game(), *[_same_bot() for _ in range(12)], None])

        summary = harness.run(max_completed_games=1, failure_budget=5)

        self.assertEqual("target_completed_games_reached", summary.stopped_reason)
        self.assertEqual(1, summary.completed_games)
        self.assertEqual(1, summary.failed_games)
        self.assertEqual(0, summary.consecutive_failures)
        self.assertEqual(12, summary.same_bot_rejections)
        self.assertFalse(summary.awaiting_original_game)
        first, *rejections, completed = harness.events
        self.assertEqual(("retry", True, 1, 1), _state(first))
        self.assertEqual(
            [("awaiting_original_game", True, 1, 1)] * 12,
            [_state(e) for e in rejections],
        )
        self.assertEqual(
            list(range(1, 13)), [e.same_bot_rejections for e in rejections]
        )
        self.assertEqual("game_completed", completed.kind)
        self.assertEqual("recovered_from_original_game_wait", completed.outcome)
        self.assertFalse(completed.awaiting_original_game)
        # Bounded backoff keyed on the rejection count: never zero, capped.
        self.assertEqual([5.0, 5.0, 10.0, 20.0, 40.0] + [60.0] * 8, harness.delays)

    def test_repeated_rejections_never_reach_failure_budget_exhausted(
        self,
    ) -> None:
        harness = _Harness(
            [_in_game(), *[_same_bot() for _ in range(30)]], stop_after_calls=31
        )

        summary = harness.run(failure_budget=2)

        self.assertEqual("stop_requested", summary.stopped_reason)
        self.assertEqual(1, summary.failed_games)
        self.assertEqual(1, summary.consecutive_failures)
        self.assertEqual(30, summary.same_bot_rejections)
        self.assertTrue(summary.awaiting_original_game)
        self.assertNotIn(
            "failure_budget_exhausted", [e.outcome for e in harness.events]
        )

    def test_wait_bound_terminates_a_stuck_same_bot_condition(self) -> None:
        harness = _Harness([_in_game(), *[_same_bot() for _ in range(1000)]])

        summary = harness.run(original_game_wait_seconds=600.0)

        self.assertEqual("original_game_wait_exhausted", summary.stopped_reason)
        self.assertEqual(1, summary.failed_games)
        self.assertEqual(1, summary.consecutive_failures)
        self.assertTrue(summary.awaiting_original_game)
        last = harness.events[-1]
        self.assertEqual("original_game_wait_exhausted", last.outcome)
        self.assertIsNone(last.backoff_seconds)
        # The wait started at the in-game failure (t=30); every earlier
        # rejection was within the bound and the terminating one was not.
        self.assertGreaterEqual(last.elapsed_seconds - _GAME_SECONDS, 600.0)
        for event in harness.events[1:-1]:
            self.assertLess(event.elapsed_seconds - _GAME_SECONDS, 600.0)
        self.assertEqual(len(harness.events) - 1, summary.same_bot_rejections)

    def test_default_wait_bound_is_finite_and_exceeds_the_failure_budget(
        self,
    ) -> None:
        harness = _Harness([_in_game(), *[_same_bot() for _ in range(1000)]])

        summary = harness.run()

        self.assertEqual("original_game_wait_exhausted", summary.stopped_reason)
        self.assertGreater(summary.same_bot_rejections, 5)
        self.assertGreaterEqual(harness.events[-1].elapsed_seconds, 3600.0)

    def test_duration_cutoff_during_wait_stops_retries(self) -> None:
        harness = _Harness([_in_game(), *[_same_bot() for _ in range(1000)]])

        summary = harness.run(max_duration_seconds=490)

        self.assertEqual("duration_reached", summary.stopped_reason)
        self.assertTrue(summary.awaiting_original_game)
        last = harness.events[-1]
        self.assertEqual("duration_reached", last.outcome)
        self.assertTrue(last.awaiting_original_game)
        self.assertEqual(490.0, harness.clock)
        self.assertEqual(len(harness.events), harness.calls)

    def test_stop_request_during_wait_stops_retries(self) -> None:
        harness = _Harness(
            [_in_game(), *[_same_bot() for _ in range(10)]], stop_after_calls=3
        )

        summary = harness.run()

        self.assertEqual("stop_requested", summary.stopped_reason)
        self.assertEqual(3, harness.calls)
        self.assertTrue(summary.awaiting_original_game)
        self.assertEqual(2, summary.same_bot_rejections)

    def test_new_game_clears_wait_and_restores_normal_counting(self) -> None:
        harness = _Harness(
            [
                _in_game(),
                _same_bot(),
                None,
                # A same-bot rejection after recovery is ordinary again.
                _same_bot(),
                _same_bot(),
            ]
        )

        summary = harness.run(failure_budget=2)

        self.assertEqual("failure_budget_exhausted", summary.stopped_reason)
        self.assertEqual(1, summary.completed_games)
        self.assertEqual(3, summary.failed_games)
        self.assertEqual(2, summary.consecutive_failures)
        self.assertEqual(1, summary.same_bot_rejections)
        self.assertFalse(summary.awaiting_original_game)
        self.assertEqual(
            ["retry", "failure_budget_exhausted"],
            [e.outcome for e in harness.events[-2:]],
        )

    def test_same_bot_without_prior_in_game_failure_is_ordinary(self) -> None:
        harness = _Harness([_same_bot() for _ in range(5)])

        summary = harness.run(failure_budget=5)

        self.assertEqual("failure_budget_exhausted", summary.stopped_reason)
        self.assertEqual(5, summary.failed_games)
        self.assertEqual(0, summary.same_bot_rejections)
        self.assertFalse(any(e.awaiting_original_game for e in harness.events))

    def test_same_bot_after_pre_game_failure_is_ordinary(self) -> None:
        harness = _Harness([_connect_failure(), _same_bot(), _same_bot()])

        summary = harness.run(failure_budget=3)

        self.assertEqual("failure_budget_exhausted", summary.stopped_reason)
        self.assertEqual(3, summary.failed_games)
        self.assertEqual(0, summary.same_bot_rejections)

    def test_failure_without_diagnostics_does_not_enter_wait(self) -> None:
        harness = _Harness([UnexpectedDisconnectError("dc"), _same_bot()])

        summary = harness.run(failure_budget=2)

        self.assertEqual("failure_budget_exhausted", summary.stopped_reason)
        self.assertEqual(2, summary.failed_games)
        self.assertEqual(0, summary.same_bot_rejections)

    def test_unrelated_transport_failure_while_waiting_is_ordinary(self) -> None:
        harness = _Harness(
            [_in_game(), _same_bot(), _connect_failure(), _same_bot(), None],
        )

        summary = harness.run(max_completed_games=1, failure_budget=5)

        self.assertEqual(1, summary.completed_games)
        self.assertEqual(2, summary.failed_games)
        self.assertEqual(2, summary.same_bot_rejections)
        connect_event = harness.events[2]
        self.assertEqual("retry", connect_event.outcome)
        self.assertEqual(2, connect_event.consecutive_failures)
        self.assertEqual(10.0, connect_event.backoff_seconds)
        self.assertTrue(connect_event.awaiting_original_game)

    def test_unrelated_failures_while_waiting_still_exhaust_the_budget(
        self,
    ) -> None:
        harness = _Harness(
            [_in_game(), _same_bot(), _token_rejected(), _connect_failure()]
        )

        summary = harness.run(failure_budget=3)

        self.assertEqual("failure_budget_exhausted", summary.stopped_reason)
        self.assertEqual(3, summary.failed_games)
        self.assertEqual(1, summary.same_bot_rejections)

    def test_new_in_game_failure_restarts_the_wait(self) -> None:
        # A second in-game failure means a new game had started; it counts as
        # a failed game and the wait restarts from that failure.
        harness = _Harness(
            [_in_game(), _same_bot(), _in_game(), _same_bot(), _same_bot()]
        )

        summary = harness.run(failure_budget=5, original_game_wait_seconds=60.0)

        self.assertEqual(2, summary.failed_games)
        self.assertEqual(2, summary.consecutive_failures)
        self.assertEqual(3, summary.same_bot_rejections)
        self.assertEqual("original_game_wait_exhausted", summary.stopped_reason)
        # The per-wait rejection count restarts, so does its backoff.
        self.assertEqual([5.0, 5.0, 10.0, 5.0], harness.delays)

    def test_non_transport_errors_while_waiting_still_propagate(self) -> None:
        for error in (
            ProtocolError("raw payload"),
            DurableRankedGameRecordError("record"),
            RuntimeError("policy exploded"),
        ):
            with self.subTest(type(error).__name__):
                harness = _Harness([_in_game(), _same_bot(), error])
                with self.assertRaises(type(error)):
                    harness.run()
                self.assertEqual(3, harness.calls)

    def test_invalid_wait_bound_fails_closed(self) -> None:
        for value in (0, -1.0, True, float("inf"), float("nan"), "60"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _Harness([]).run(original_game_wait_seconds=value)


def _state(event: ContinuousRunEvent) -> tuple[object, ...]:
    return (
        event.outcome,
        event.awaiting_original_game,
        event.failed_games,
        event.consecutive_failures,
    )


class OriginalGameWaitFactsTest(unittest.TestCase):
    def test_event_line_marks_wait_with_fixed_vocabulary(self) -> None:
        line = format_continuous_event(
            ContinuousRunEvent(
                kind="transport_failure",
                elapsed_seconds=90.0,
                profile="lisjong-dev",
                completed_games=0,
                failed_games=1,
                consecutive_failures=1,
                exception_type="TransportError",
                backoff_seconds=5.0,
                outcome="awaiting_original_game",
                awaiting_original_game=True,
                same_bot_rejections=1,
            )
        )
        self.assertEqual(
            "continuous-event: kind=transport_failure elapsed_seconds=90.0 "
            "profile=lisjong-dev completed_games=0 failed_games=1 "
            "consecutive_failures=1 awaiting_original_game=true "
            "same_bot_rejections=1 exception=TransportError backoff_seconds=5 "
            "outcome=awaiting_original_game",
            line,
        )

    def test_recovered_game_line_carries_its_outcome(self) -> None:
        line = format_continuous_event(
            ContinuousRunEvent(
                kind="game_completed",
                elapsed_seconds=900.0,
                profile="lisjong-dev",
                completed_games=1,
                failed_games=1,
                consecutive_failures=0,
                outcome="recovered_from_original_game_wait",
                same_bot_rejections=3,
            )
        )
        self.assertEqual(
            "continuous-event: kind=game_completed elapsed_seconds=900.0 "
            "profile=lisjong-dev completed_games=1 failed_games=1 "
            "consecutive_failures=0 outcome=recovered_from_original_game_wait "
            "same_bot_rejections=3",
            line,
        )

    def test_summary_reports_wait_state(self) -> None:
        harness = _Harness([_in_game(), *[_same_bot() for _ in range(1000)]])
        text = format_continuous_summary(harness.run(original_game_wait_seconds=60))
        self.assertIn("awaiting original game: yes", text)
        self.assertIn("same-bot rejections: 2", text)
        self.assertIn("stopped reason: original_game_wait_exhausted", text)

    def _cli(self, outcomes, *, wait_seconds: float) -> tuple[int, str, str]:
        outcomes = iter(outcomes)
        original = run_continuous_ranked

        async def _game(policy, token, **kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome

        async def _no_sleep(_delay: float) -> None:
            return None

        async def _runner(profile, token, **kwargs):
            return await original(
                profile,
                token,
                sleep=_no_sleep,
                original_game_wait_seconds=wait_seconds,
                **kwargs,
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: _TOKEN}),
            patch("lisjong_arena.riichilab.continuous_ranked.run_ranked_game", _game),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _runner,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = _run_cli(["--profile", "lisjong-dev"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_wait_exhaustion_exits_non_zero_without_secrets(self) -> None:
        code, stdout, stderr = self._cli([_in_game(), _same_bot()], wait_seconds=1e-9)

        self.assertEqual(1, code)
        self.assertIn("stopped reason: original_game_wait_exhausted", stdout)
        self.assertIn("same-bot rejections: 1", stdout)
        self.assertIn("outcome=original_game_wait_exhausted", stderr)
        for text in (stdout, stderr):
            self.assertNotIn(_TOKEN, text)
            self.assertNotIn("rejected", text.replace("token_or_bot_rejected", ""))

    def test_cli_terminal_facts_report_wait_state(self) -> None:
        code, stdout, stderr = self._cli(
            [_in_game(), _same_bot(), ProtocolError("raw payload")],
            wait_seconds=3600.0,
        )

        self.assertEqual(1, code)
        facts = [line for line in stderr.splitlines() if line.startswith("terminal ")]
        self.assertIn("terminal failed games: 1", facts)
        self.assertIn("terminal awaiting original game: yes", facts)
        self.assertIn("terminal same-bot rejections: 1", facts)


if __name__ == "__main__":
    unittest.main()
