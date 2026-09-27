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

from lisjong.policy_contract import Seat

from lisjong_arena.riichilab.continuous_ranked import (
    ContinuousRunEvent,
    ContinuousRunSummary,
    _backoff_seconds,
    _run_cli,
    format_continuous_event,
    format_continuous_summary,
    run_continuous_ranked,
    run_continuous_ranked_cli,
)
from lisjong_arena.riichilab.errors import (
    ProtocolError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.live_presentation import (
    BoundedRankedPresentationBuffer,
    ContinuousRankedPresentationFeed,
    RankedCompletionPresentation,
)
from lisjong_arena.riichilab.profile import RuntimeProfile
from lisjong_arena.riichilab.trace import ProtocolTraceError
from lisjong_arena.riichilab.transport_diagnostics import TransportDiagnostics

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

    def test_cancellation_is_not_retried_and_propagates(self) -> None:
        """`CancelledError`はTransportErrorとしてretryされず、catchもされずに
        呼び出し元へそのまま伝播する(標準のasyncio cancellation semantics)。
        """
        call_count = {"n": 0}
        delays: list[float] = []

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
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    run_continuous_ranked(
                        profile,
                        "unit-test-token",
                        sleep=_recording_sleep(delays),
                    )
                )

        self.assertEqual(call_count["n"], 2)
        self.assertEqual(delays, [])

    def test_cancellation_does_not_call_a_new_policy_factory(self) -> None:
        created: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            raise asyncio.CancelledError()

        profile = _make_profile(created_policies=created)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    run_continuous_ranked(
                        profile,
                        "unit-test-token",
                        sleep=_no_sleep,
                    )
                )

        self.assertEqual(len(created), 1)


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


class ContinuousPresentationTest(unittest.TestCase):
    """Issue #381: opt-in per-game live presentation thread-through。"""

    def test_without_presentation_no_presentation_kwarg_is_passed(self) -> None:
        seen: list[dict[str, object]] = []
        stop_requested, on_call = _stop_after(1)

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            seen.append(kwargs)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "token",
                    stop_requested=stop_requested,
                    sleep=_no_sleep,
                )
            )
        self.assertEqual(len(seen), 1)
        self.assertNotIn("presentation", seen[0])

    def test_each_attempt_gets_its_own_buffer_including_retries(self) -> None:
        feed = ContinuousRankedPresentationFeed()
        buffers: list[BoundedRankedPresentationBuffer] = []
        ordinals: list[int] = []
        outcomes = [None, TransportError("drop"), None]
        stop_requested, on_call = _stop_after(len(outcomes))

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            buffer = kwargs["presentation"]
            buffers.append(buffer)
            ordinals.append(feed.current().game_ordinal)
            self.assertIs(feed.current().buffer, buffer)
            outcome = outcomes[len(buffers) - 1]
            if outcome is not None:
                raise outcome

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "token",
                    stop_requested=stop_requested,
                    presentation=feed,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(summary.completed_games, 2)
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(ordinals, [1, 2, 3])
        self.assertEqual(len({id(buffer) for buffer in buffers}), 3)

    def test_previous_game_terminal_is_published_before_next_game_opens(
        self,
    ) -> None:
        feed = ContinuousRankedPresentationFeed()
        handles = []
        stop_requested, on_call = _stop_after(2)

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            handles.append(feed.current())
            kwargs["presentation"].publish_completion(
                RankedCompletionPresentation(self_seat=Seat.SEAT_1, scores=None)
            )

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "token",
                    stop_requested=stop_requested,
                    presentation=feed,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(feed.current().game_ordinal, 2)
        first_batch = handles[0].buffer.drain()
        self.assertEqual(first_batch.completion.self_seat, Seat.SEAT_1)

    def test_record_acquisition_receives_the_game_buffer(self) -> None:
        feed = ContinuousRankedPresentationFeed()
        seen: list[object] = []

        async def _fake_acquire(policy, token, **kwargs):
            seen.append(kwargs["presentation"])
            return object()

        with (
            patch(
                "lisjong_arena.riichilab.continuous_ranked._acquire_ranked_record",
                _fake_acquire,
            ),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.resolve_ranked_record_path",
                lambda record_dir: f"{record_dir}/game",
            ),
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "token",
                    record_dir="record-root",
                    max_completed_games=2,
                    presentation=feed,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(summary.completed_games, 2)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])
        self.assertIs(seen[1], feed.current().buffer)

    def test_detached_feed_does_not_stop_the_run(self) -> None:
        feed = ContinuousRankedPresentationFeed()
        feed.detach()
        stop_requested, on_call = _stop_after(2)

        async def _fake_run_ranked_game(policy, token, **kwargs):
            on_call()
            self.assertFalse(kwargs["presentation"].is_attached)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "token",
                    stop_requested=stop_requested,
                    presentation=feed,
                    sleep=_no_sleep,
                )
            )
        self.assertEqual(summary.completed_games, 2)

    def test_invalid_presentation_fails_closed_before_any_game(self) -> None:
        created: list[object] = []
        with self.assertRaises(TypeError):
            asyncio.run(
                run_continuous_ranked(
                    _make_profile(created_policies=created),
                    "token",
                    presentation=BoundedRankedPresentationBuffer(),  # type: ignore[arg-type]
                    sleep=_no_sleep,
                )
            )
        self.assertEqual(created, [])


class PublicCliTest(unittest.TestCase):
    def test_presentation_and_stop_requested_are_forwarded(self) -> None:
        feed = ContinuousRankedPresentationFeed()
        captured: dict[str, object] = {}

        def _stop() -> bool:
            return True

        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            captured.update(kwargs)
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=0,
                failed_games=0,
                consecutive_failures=0,
                last_failure_type=None,
                stopped_reason="stop_requested",
            )

        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_run_continuous_ranked,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            return_code = run_continuous_ranked_cli(
                ["--profile", "lisjong-dev"],
                presentation=feed,
                stop_requested=_stop,
            )

        self.assertEqual(return_code, 0)
        self.assertIs(captured["presentation"], feed)
        self.assertIs(captured["stop_requested"], _stop)
        self.assertIn("stopped reason: stop_requested", stdout.getvalue())
        self.assertNotIn("unit-test-dummy-token", stdout.getvalue())

    def test_module_entry_point_passes_no_optional_kwargs(self) -> None:
        captured: dict[str, object] = {}

        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            captured.update(kwargs)
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=0,
                failed_games=0,
                consecutive_failures=0,
                last_failure_type=None,
                stopped_reason="stop_requested",
            )

        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_run_continuous_ranked,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _run_cli(["--profile", "lisjong-dev"])

        self.assertNotIn("presentation", captured)
        self.assertNotIn("stop_requested", captured)


class CliRegressionTest(unittest.TestCase):
    def test_missing_credential_exits_2(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_DEV_TOKEN_VAR, None)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return_code = _run_cli(["--profile", "lisjong-dev"])
        self.assertEqual(return_code, 2)
        self.assertIn(_DEV_TOKEN_VAR, stderr.getvalue())

    def test_structured_secret_exits_2_and_does_not_invoke_the_runner(self) -> None:
        """Issue #336: SecretStringがJSON objectだった場合、
        `run_continuous_ranked()`(ranked runner/retry loop)へ到達する前に
        fail closedすることを固定する。
        """
        structured_secret = '{"LISJONG_DEV_BOT_TOKEN": "should-not-be-used"}'

        async def _runner_must_not_be_called(profile, token, **kwargs):
            raise AssertionError(
                "run_continuous_ranked must not be invoked after a secret "
                "format failure"
            )

        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: structured_secret}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _runner_must_not_be_called,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(["--profile", "lisjong-dev"])
        self.assertEqual(return_code, 2)
        self.assertNotIn("should-not-be-used", stderr.getvalue())
        self.assertIn("RIICHILAB SECRET FORMAT BLOCKER", stderr.getvalue())

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

    def test_keyboard_interrupt_from_asyncio_run_exits_0_and_is_secret_safe(
        self,
    ) -> None:
        """`asyncio.run()`はCtrl-Cによるtask cancellationを`KeyboardInterrupt`
        として呼び出し元へ再送出する。`_run_cli()`はこのboundaryだけで
        Ctrl-Cを正常終了として扱う(`run_continuous_ranked()`自体は
        `CancelledError`をcatchしない)。
        """
        dummy_token = "unit-test-dummy-token-should-not-leak"

        async def _fake_run_continuous_ranked(profile, token, **kwargs):
            raise KeyboardInterrupt()

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
        self.assertNotIn(dummy_token, stdout.getvalue())
        self.assertNotIn(dummy_token, stderr.getvalue())


class RunEventTest(unittest.TestCase):
    """Issue #404: secret-safe evidence for every retryable transport failure."""

    def _run(self, outcomes, **kwargs) -> tuple[list[ContinuousRunEvent], object]:
        outcomes = iter(outcomes)
        events: list[ContinuousRunEvent] = []
        clock = {"now": 0.0}

        async def _fake_run_ranked_game(policy, token, **_kwargs):
            clock["now"] += 30.0
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome

        async def _sleep(delay: float) -> None:
            clock["now"] += delay

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "unit-test-token",
                    sleep=_sleep,
                    monotonic=lambda: clock["now"],
                    on_event=events.append,
                    **kwargs,
                )
            )
        return events, summary

    def test_every_transport_failure_emits_backoff_and_outcome(self) -> None:
        events, summary = self._run(
            [None, UnexpectedDisconnectError("dc")] + [TransportError("boom")] * 4,
            failure_budget=5,
        )

        self.assertEqual(summary.stopped_reason, "failure_budget_exhausted")
        failures = [e for e in events if e.kind == "transport_failure"]
        self.assertEqual(len(failures), summary.failed_games)
        self.assertEqual(
            [(e.consecutive_failures, e.backoff_seconds, e.outcome) for e in failures],
            [
                (1, 5.0, "retry"),
                (2, 10.0, "retry"),
                (3, 20.0, "retry"),
                (4, 40.0, "retry"),
                (5, None, "failure_budget_exhausted"),
            ],
        )
        self.assertEqual(
            [e.exception_type for e in failures],
            ["UnexpectedDisconnectError"] + ["TransportError"] * 4,
        )
        completed = events[0]
        self.assertEqual(completed.kind, "game_completed")
        self.assertEqual(completed.completed_games, 1)
        self.assertEqual(completed.elapsed_seconds, 30.0)
        # The first failure is reported before its backoff sleep.
        self.assertEqual(failures[0].elapsed_seconds, 60.0)
        self.assertEqual(failures[1].elapsed_seconds, 95.0)
        self.assertTrue(all(e.profile == "unit-test-profile" for e in events))

    def test_deadline_capped_backoff_reports_duration_reached(self) -> None:
        events, summary = self._run([TransportError("boom")], max_duration_seconds=33)

        self.assertEqual(summary.stopped_reason, "duration_reached")
        (failure,) = events
        self.assertEqual(failure.outcome, "duration_reached")
        self.assertEqual(failure.backoff_seconds, 3.0)

    def test_formatted_event_is_one_secret_free_line(self) -> None:
        line = format_continuous_event(
            ContinuousRunEvent(
                kind="transport_failure",
                elapsed_seconds=61.25,
                profile="lisjong-dev",
                completed_games=2,
                failed_games=3,
                consecutive_failures=1,
                exception_type="UnexpectedDisconnectError",
                backoff_seconds=5.0,
                outcome="retry",
            )
        )
        self.assertEqual(
            line,
            "continuous-event: kind=transport_failure elapsed_seconds=61.2 "
            "profile=lisjong-dev completed_games=2 failed_games=3 "
            "consecutive_failures=1 exception=UnexpectedDisconnectError "
            "backoff_seconds=5 outcome=retry",
        )


class TransportDiagnosticsEventTest(unittest.TestCase):
    """Issue #411: a failure's diagnostics reach its event, retries unchanged."""

    _DIAGNOSTICS = TransportDiagnostics(
        phase="before_start_game",
        operation="recv",
        http_status=None,
        close_code_received=4000,
        close_code_sent=None,
        server_reason_class="same_bot_already_active",
        server_reason_excerpt="Bot already in game; token [redacted]",
        local_close_reason_class="none",
        requests_received=0,
        last_decision_elapsed_seconds=1.25,
    )

    def test_diagnostics_are_attached_and_retry_semantics_are_unchanged(
        self,
    ) -> None:
        def _failure() -> UnexpectedDisconnectError:
            error = UnexpectedDisconnectError("dc")
            error.diagnostics = self._DIAGNOSTICS
            return error

        outcomes = iter([_failure(), TransportError("no diagnostics")] * 3)
        events: list[ContinuousRunEvent] = []
        delays: list[float] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            raise next(outcomes)

        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    _make_profile(),
                    "unit-test-token",
                    sleep=_recording_sleep(delays),
                    on_event=events.append,
                    failure_budget=5,
                )
            )

        self.assertEqual(summary.stopped_reason, "failure_budget_exhausted")
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0])
        self.assertEqual(
            [event.transport for event in events],
            [self._DIAGNOSTICS, None, self._DIAGNOSTICS, None, self._DIAGNOSTICS],
        )

    def test_formatted_diagnostics_are_single_tokens(self) -> None:
        line = format_continuous_event(
            ContinuousRunEvent(
                kind="transport_failure",
                elapsed_seconds=343.2,
                profile="lisjong-dev",
                completed_games=0,
                failed_games=2,
                consecutive_failures=2,
                exception_type="UnexpectedDisconnectError",
                backoff_seconds=10.0,
                outcome="retry",
                transport=self._DIAGNOSTICS,
            )
        )
        self.assertTrue(
            line.endswith(
                "outcome=retry phase=before_start_game operation=recv "
                "http_status=none close_code_received=4000 close_code_sent=none "
                "server_reason_class=same_bot_already_active "
                "server_reason_excerpt=Bot%20already%20in%20game%3B%20token%20"
                "%5Bredacted%5D local_close_reason_class=none requests_received=0 "
                "last_decision_elapsed_seconds=1.250"
            ),
            line,
        )


class CliTerminalFactsTest(unittest.TestCase):
    """Issue #404: a fail-closed exit keeps the runner state it had reached."""

    _TOKEN = "unit-test-dummy-token-should-not-leak"

    def _cli(self, outcomes) -> tuple[int | None, str, str, BaseException | None]:
        outcomes = iter(outcomes)
        original = run_continuous_ranked

        async def _fake_run_ranked_game(policy, token, **kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome

        async def _no_sleep_runner(profile, token, **kwargs):
            return await original(profile, token, sleep=_no_sleep, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        code: int | None = None
        raised: BaseException | None = None
        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: self._TOKEN}),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _no_sleep_runner,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = _run_cli(["--profile", "lisjong-dev"])
            except RuntimeError as error:
                raised = error
        for text in (stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn(self._TOKEN, text)
        return code, stdout.getvalue(), stderr.getvalue(), raised

    def test_retry_events_are_written_to_stderr(self) -> None:
        code, stdout, stderr, _ = self._cli([TransportError("boom")] * 5)

        self.assertEqual(code, 1)
        self.assertIn("stopped reason: failure_budget_exhausted", stdout)
        self.assertEqual(stderr.count("continuous-event: kind=transport_failure"), 5)
        self.assertIn("outcome=failure_budget_exhausted", stderr)
        self.assertNotIn("terminal ", stderr)

    def test_client_error_keeps_counts_and_exception_class_only(self) -> None:
        code, stdout, stderr, _ = self._cli(
            [None, UnexpectedDisconnectError("dc"), ProtocolError("raw payload")]
        )

        self.assertEqual(code, 1)
        self.assertNotIn("stopped reason:", stdout)
        facts = "\n".join(
            line for line in stderr.splitlines() if line.startswith("terminal ")
        )
        self.assertIn("terminal completed games: 1", facts)
        self.assertIn("terminal failed games: 1", facts)
        self.assertIn("terminal consecutive failures: 1", facts)
        self.assertIn("terminal last failure type: UnexpectedDisconnectError", facts)
        self.assertIn("terminal exception type: ProtocolError", facts)
        self.assertIn("terminal exception category: riichilab_client", facts)
        self.assertIn("terminal stopped reason: runner_exception", facts)
        self.assertNotIn("raw payload", facts)

    def test_unexpected_exception_keeps_facts_and_still_propagates(self) -> None:
        _, _, stderr, raised = self._cli([RuntimeError("policy bug")])

        self.assertIsInstance(raised, RuntimeError)
        self.assertIn("terminal completed games: 0", stderr)
        self.assertIn("terminal exception type: RuntimeError", stderr)
        self.assertIn("terminal exception category: unexpected", stderr)


if __name__ == "__main__":
    unittest.main()
