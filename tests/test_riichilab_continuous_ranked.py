"""RiichiLab ranked resilient / continuous participation runner tests (Issue #47)。

`run_ranked_game()`をfake/monkeypatchしたone-game boundaryだけを検証し、
live RiichiLabへは接続しない。実時間sleepも行わず、`asyncio.sleep`相当は
すべてfakeへ差し替える。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import unittest
from unittest.mock import patch

from lisjong_arena.riichilab.continuous_ranked import (
    ContinuousRunSummary,
    _backoff_seconds,
    _run_cli,
    format_continuous_summary,
    run_continuous_ranked,
)
from lisjong_arena.riichilab.errors import (
    ProtocolError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.profile import RuntimeProfile
from lisjong_arena.riichilab.trace import ProtocolTraceError

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"


class _FakePolicy:
    """gameごとの identity 比較用の使い捨て object。"""


def _make_profile(*, created_policies: list[object] | None = None) -> RuntimeProfile:
    sink = created_policies if created_policies is not None else []

    def _factory() -> _FakePolicy:
        policy = _FakePolicy()
        sink.append(policy)
        return policy

    return RuntimeProfile(
        name="unit-test-profile",
        credential_env_var=_DEV_TOKEN_VAR,
        policy_factory=_factory,
        runtime_namespace="unit-test-profile",
    )


async def _no_sleep(_delay: float) -> None:
    return None


def _recording_sleep(recorded: list[float]):
    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    return _sleep


def _stop_after(n_calls: int):
    """`run_ranked_game`呼び出し回数が`n_calls`へ到達したらstopを要求する。"""
    state = {"calls": 0}

    def _stop_requested() -> bool:
        return state["calls"] >= n_calls

    def _on_call() -> None:
        state["calls"] += 1

    return _stop_requested, _on_call


class BackoffFormulaTest(unittest.TestCase):
    def test_backoff_sequence_matches_baseline(self) -> None:
        self.assertEqual(_backoff_seconds(1), 5.0)
        self.assertEqual(_backoff_seconds(2), 10.0)
        self.assertEqual(_backoff_seconds(3), 20.0)
        self.assertEqual(_backoff_seconds(4), 40.0)

    def test_backoff_is_capped_at_sixty_seconds(self) -> None:
        self.assertEqual(_backoff_seconds(5), 60.0)
        self.assertEqual(_backoff_seconds(6), 60.0)
        self.assertEqual(_backoff_seconds(20), 60.0)


class SuccessLoopTest(unittest.TestCase):
    def test_success_proceeds_to_next_game(self) -> None:
        calls: list[tuple[object, str]] = []
        stop_requested, on_call = _stop_after(2)

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            calls.append((policy, token))
            return None

        created: list[object] = []
        profile = _make_profile(created_policies=created)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(summary.completed_games, 2)
        self.assertEqual(summary.failed_games, 0)
        self.assertEqual(summary.consecutive_failures, 0)
        self.assertEqual(summary.stopped_reason, "stop_requested")

    def test_each_game_receives_a_distinct_fresh_policy_instance(self) -> None:
        stop_requested, on_call = _stop_after(3)
        seen_policies: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            seen_policies.append(policy)
            return None

        created: list[object] = []
        profile = _make_profile(created_policies=created)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(len(seen_policies), 3)
        self.assertEqual(len(created), 3)
        self.assertEqual(len(set(id(p) for p in seen_policies)), 3)
        self.assertIs(seen_policies[0], created[0])
        self.assertIs(seen_policies[1], created[1])
        self.assertIs(seen_policies[2], created[2])

    def test_same_resolved_token_reaches_every_game(self) -> None:
        stop_requested, on_call = _stop_after(3)
        tokens_seen: list[str] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            tokens_seen.append(token)
            return None

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "resolved-token-value",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(tokens_seen, ["resolved-token-value"] * 3)

    def test_trace_path_is_forwarded_unchanged_to_every_game(self) -> None:
        stop_requested, on_call = _stop_after(2)
        trace_paths_seen: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            trace_paths_seen.append(kwargs.get("trace_path"))
            return None

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    trace_path="trace/continuous-unit-test.jsonl",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(
            trace_paths_seen,
            ["trace/continuous-unit-test.jsonl", "trace/continuous-unit-test.jsonl"],
        )


class RetryTest(unittest.TestCase):
    def test_transport_error_backs_off_and_retries(self) -> None:
        outcomes = iter([TransportError("boom"), None])
        stop_requested, on_call = _stop_after(2)
        delays: list[float] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_recording_sleep(delays),
                )
            )

        self.assertEqual(delays, [5.0])
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.consecutive_failures, 0)
        self.assertEqual(summary.last_failure_type, "TransportError")

    def test_unexpected_disconnect_error_backs_off_and_retries(self) -> None:
        outcomes = iter([UnexpectedDisconnectError("dc"), None])
        stop_requested, on_call = _stop_after(2)
        delays: list[float] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_recording_sleep(delays),
                )
            )

        self.assertEqual(delays, [5.0])
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.last_failure_type, "UnexpectedDisconnectError")

    def test_failed_game_is_not_counted_as_completed(self) -> None:
        stop_requested, on_call = _stop_after(1)

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            raise TransportError("boom")

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                    failure_budget=100,
                )
            )

        self.assertEqual(summary.completed_games, 0)
        self.assertEqual(summary.failed_games, 1)

    def test_expected_backoff_sequence_and_cap(self) -> None:
        delays: list[float] = []
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            raise TransportError("boom")

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    sleep=_recording_sleep(delays),
                    failure_budget=7,
                )
            )

        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])
        self.assertEqual(call_count["n"], 7)
        self.assertEqual(summary.failed_games, 7)
        self.assertEqual(summary.stopped_reason, "failure_budget_exhausted")

    def test_does_not_busy_loop_between_failures(self) -> None:
        delays: list[float] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            raise TransportError("boom")

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    sleep=_recording_sleep(delays),
                    failure_budget=3,
                )
            )

        self.assertEqual(len(delays), 2)
        self.assertTrue(all(delay > 0 for delay in delays))

    def test_failure_budget_stops_the_loop(self) -> None:
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            raise TransportError("boom")

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    sleep=_no_sleep,
                    failure_budget=5,
                )
            )

        self.assertEqual(call_count["n"], 5)
        self.assertEqual(summary.consecutive_failures, 5)
        self.assertEqual(summary.stopped_reason, "failure_budget_exhausted")

    def test_budget_reached_skips_extra_sleep_and_requeue(self) -> None:
        delays: list[float] = []
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            raise TransportError("boom")

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    sleep=_recording_sleep(delays),
                    failure_budget=5,
                )
            )

        # 5 failures -> only 4 backoff sleeps (none after the budget-exhausting failure).
        self.assertEqual(len(delays), 4)
        self.assertEqual(call_count["n"], 5)


class FailureResetTest(unittest.TestCase):
    def test_consecutive_failure_count_resets_after_success(self) -> None:
        outcomes = iter(
            [
                TransportError("boom-1"),
                TransportError("boom-2"),
                None,
                TransportError("boom-3"),
            ]
        )
        stop_requested, on_call = _stop_after(4)
        delays: list[float] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_recording_sleep(delays),
                )
            )

        # failure streak after success resets: backoff restarts at 5s, not 20s.
        self.assertEqual(delays, [5.0, 10.0, 5.0])
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.failed_games, 3)
        self.assertEqual(summary.consecutive_failures, 1)


class FailClosedTest(unittest.TestCase):
    def _assert_not_retried(self, error: Exception) -> None:
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            raise error

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            with self.assertRaises(type(error)):
                asyncio.run(
                    run_continuous_ranked(
                        profile,
                        "unit-test-token",
                        sleep=_no_sleep,
                    )
                )

        self.assertEqual(call_count["n"], 1)

    def test_protocol_error_is_not_retried(self) -> None:
        self._assert_not_retried(ProtocolError("bad protocol"))

    def test_protocol_trace_error_is_not_retried(self) -> None:
        self._assert_not_retried(ProtocolTraceError("trace failed"))

    def test_arbitrary_runtime_error_is_not_retried(self) -> None:
        self._assert_not_retried(RuntimeError("policy/adapter exploded"))

    def test_arbitrary_value_error_is_not_retried(self) -> None:
        self._assert_not_retried(ValueError("invariant violated"))


class ShutdownTest(unittest.TestCase):
    def test_stop_request_before_first_game_runs_zero_games(self) -> None:
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            return None

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=lambda: True,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(call_count["n"], 0)
        self.assertEqual(summary.completed_games, 0)
        self.assertEqual(summary.stopped_reason, "stop_requested")

    def test_stop_request_after_success_does_not_start_a_new_game(self) -> None:
        stop_requested, on_call = _stop_after(1)
        created: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            return None

        profile = _make_profile(created_policies=created)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.stopped_reason, "stop_requested")

    def test_cancellation_is_not_retried_and_stops_the_loop(self) -> None:
        call_count = {"n": 0}

        async def _fake_run_ranked_game(policy, token, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None
            raise asyncio.CancelledError()

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "unit-test-token",
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(call_count["n"], 2)
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.stopped_reason, "cancelled")

    def test_shutdown_summary_output_is_secret_safe(self) -> None:
        dummy_token = "unit-test-dummy-token-should-not-leak"

        async def _fake_run_ranked_game(policy, token, **kwargs):
            raise asyncio.CancelledError()

        profile = _make_profile()

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    dummy_token,
                    sleep=_no_sleep,
                )
            )

        output = format_continuous_summary(summary)
        self.assertNotIn(dummy_token, output)


class SummaryFormattingTest(unittest.TestCase):
    def test_format_contains_no_secret_like_fields(self) -> None:
        summary = ContinuousRunSummary(
            profile="lisjong-dev",
            completed_games=3,
            failed_games=1,
            consecutive_failures=0,
            last_failure_type="TransportError",
            stopped_reason="stop_requested",
        )
        output = format_continuous_summary(summary)
        for forbidden in ("token", "authorization", "bearer", "credential"):
            self.assertNotIn(forbidden, output.lower())
        self.assertIn("completed games: 3", output)
        self.assertIn("failed games: 1", output)

    def test_last_failure_type_none_renders_as_none(self) -> None:
        summary = ContinuousRunSummary(
            profile="lisjong-dev",
            completed_games=0,
            failed_games=0,
            consecutive_failures=0,
            last_failure_type=None,
            stopped_reason="stop_requested",
        )
        self.assertIn("last failure type: none", format_continuous_summary(summary))


class CliRegressionTest(unittest.TestCase):
    def test_missing_credential_exits_2(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_DEV_TOKEN_VAR, None)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return_code = _run_cli(["--profile", "lisjong-dev"])
        self.assertEqual(return_code, 2)
        self.assertIn(_DEV_TOKEN_VAR, stderr.getvalue())

    def test_cli_reports_summary_and_is_secret_safe(self) -> None:
        dummy_token = "unit-test-dummy-token-should-not-leak"

        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            self.assertEqual(token, dummy_token)
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=2,
                failed_games=1,
                consecutive_failures=0,
                last_failure_type="TransportError",
                stopped_reason="stop_requested",
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: dummy_token}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_run_continuous_ranked,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 0)
        self.assertIn("completed games: 2", stdout.getvalue())
        self.assertNotIn(dummy_token, stdout.getvalue())
        self.assertNotIn(dummy_token, stderr.getvalue())

    def test_cli_exits_non_zero_when_failure_budget_exhausted(self) -> None:
        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=0,
                failed_games=5,
                consecutive_failures=5,
                last_failure_type="TransportError",
                stopped_reason="failure_budget_exhausted",
            )

        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_run_continuous_ranked,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 1)

    def test_protocol_error_from_loop_exits_1_and_is_secret_safe(self) -> None:
        dummy_token = "unit-test-dummy-token-should-not-leak"

        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            raise ProtocolError("bad protocol")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: dummy_token}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_run_continuous_ranked,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 1)
        self.assertIn("ProtocolError", stderr.getvalue())
        self.assertNotIn(dummy_token, stdout.getvalue())
        self.assertNotIn(dummy_token, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
