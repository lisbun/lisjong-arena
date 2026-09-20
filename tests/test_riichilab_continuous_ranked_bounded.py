"""Issue #232 bounded continuous ranked / durable acquisition tests.

All one-game execution and durable acquisition boundaries are faked; these tests never
connect to live RiichiLab.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from lisjong_arena.riichilab.continuous_ranked import (
    ContinuousRunSummary,
    _run_cli,
    run_continuous_ranked,
)
from lisjong_arena.riichilab.errors import TransportError
from lisjong_arena.riichilab.profile import RuntimeProfile

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_TRACE_PATH_VAR = "RIICHILAB_TRACE_PATH"


class _FakePolicy:
    pass


def _make_profile(*, created: list[object] | None = None) -> RuntimeProfile:
    sink = created if created is not None else []

    def _factory() -> _FakePolicy:
        policy = _FakePolicy()
        sink.append(policy)
        return policy

    return RuntimeProfile(
        name="unit-test-profile",
        credential_env_var="UNIT_TEST_TOKEN",
        policy_factory=_factory,
        runtime_namespace="unit-test-profile",
    )


async def _no_sleep(_delay: float) -> None:
    return None


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class BoundedCompletedGamesTest(unittest.TestCase):
    def test_target_stops_after_exact_completed_count_without_extra_policy(
        self,
    ) -> None:
        calls: list[object] = []
        created: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            self.assertEqual(token, "token")
            calls.append(policy)

        profile = _make_profile(created=created)
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_completed_games=2,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(created), 2)
        self.assertEqual(summary.completed_games, 2)
        self.assertEqual(summary.failed_games, 0)
        self.assertEqual(summary.requested_completed_games, 2)
        self.assertFalse(summary.records_enabled)
        self.assertEqual(summary.stopped_reason, "target_completed_games_reached")

    def test_transport_failure_does_not_count_toward_target(self) -> None:
        outcomes: list[Exception | None] = [TransportError("temporary"), None, None]
        delays: list[float] = []
        call_count = 0

        async def _fake_run_ranked_game(policy, token, **kwargs):
            nonlocal call_count
            outcome = outcomes[call_count]
            call_count += 1
            if outcome is not None:
                raise outcome

        async def _sleep(delay: float) -> None:
            delays.append(delay)

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_completed_games=2,
                    sleep=_sleep,
                )
            )

        self.assertEqual(call_count, 3)
        self.assertEqual(delays, [5.0])
        self.assertEqual(summary.completed_games, 2)
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.stopped_reason, "target_completed_games_reached")

    def test_invalid_library_targets_fail_before_policy_creation(self) -> None:
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                created: list[object] = []
                profile = _make_profile(created=created)
                with self.assertRaises(ValueError):
                    asyncio.run(
                        run_continuous_ranked(
                            profile,
                            "token",
                            max_completed_games=invalid,  # type: ignore[arg-type]
                            sleep=_no_sleep,
                        )
                    )
                self.assertEqual(created, [])


class DurationBoundTest(unittest.TestCase):
    def test_cutoff_after_in_progress_game_stops_without_requeue(self) -> None:
        clock = _FakeClock()
        calls = 0
        created: list[object] = []

        async def _fake_run_ranked_game(policy, token, **kwargs):
            nonlocal calls
            calls += 1
            clock.advance(12.0)

        profile = _make_profile(created=created)
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_duration_seconds=10,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                )
            )

        self.assertEqual(calls, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.failed_games, 0)
        self.assertEqual(summary.requested_duration_seconds, 10)
        self.assertEqual(summary.stopped_reason, "duration_reached")

    def test_retry_backoff_is_capped_at_duration_and_does_not_retry(self) -> None:
        clock = _FakeClock()
        calls = 0

        async def _fake_run_ranked_game(policy, token, **kwargs):
            nonlocal calls
            calls += 1
            clock.advance(8.0)
            raise TransportError("temporary")

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_duration_seconds=10,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                    failure_budget=5,
                )
            )

        self.assertEqual(calls, 1)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(summary.completed_games, 0)
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.stopped_reason, "duration_reached")

    def test_transport_failure_after_cutoff_does_not_sleep_or_retry(self) -> None:
        clock = _FakeClock()
        calls = 0

        async def _fake_run_ranked_game(policy, token, **kwargs):
            nonlocal calls
            calls += 1
            clock.advance(11.0)
            raise TransportError("temporary")

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_duration_seconds=10,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                    failure_budget=5,
                )
            )

        self.assertEqual(calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.stopped_reason, "duration_reached")

    def test_completed_game_target_can_win_before_duration(self) -> None:
        clock = _FakeClock()
        calls = 0

        async def _fake_run_ranked_game(policy, token, **kwargs):
            nonlocal calls
            calls += 1
            clock.advance(1.0)

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked.run_ranked_game",
            _fake_run_ranked_game,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    max_completed_games=1,
                    max_duration_seconds=100,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                )
            )

        self.assertEqual(calls, 1)
        self.assertEqual(summary.completed_games, 1)
        self.assertEqual(summary.stopped_reason, "target_completed_games_reached")

    def test_invalid_library_durations_fail_before_policy_creation(self) -> None:
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                created: list[object] = []
                profile = _make_profile(created=created)
                with self.assertRaises(ValueError):
                    asyncio.run(
                        run_continuous_ranked(
                            profile,
                            "token",
                            max_duration_seconds=invalid,  # type: ignore[arg-type]
                            sleep=_no_sleep,
                        )
                    )
                self.assertEqual(created, [])

    def test_duration_stop_after_durable_finalization_counts_completed_game(
        self,
    ) -> None:
        clock = _FakeClock()
        attempts = 0

        async def _fake_acquire(policy, token, **kwargs):
            nonlocal attempts
            attempts += 1
            clock.advance(12.0)
            return object()

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked._acquire_ranked_record",
            _fake_acquire,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    record_dir="record-root",
                    max_duration_seconds=10,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                )
            )

        self.assertEqual(attempts, 1)
        self.assertEqual(summary.completed_games, 1)
        self.assertTrue(summary.records_enabled)
        self.assertEqual(summary.stopped_reason, "duration_reached")


class DurableAcquisitionTest(unittest.TestCase):
    def test_each_completed_game_uses_fresh_destination_and_policy(self) -> None:
        destinations: list[Path] = []
        policies: list[object] = []
        created: list[object] = []

        async def _fake_acquire(policy, token, **kwargs):
            self.assertEqual(token, "token")
            self.assertEqual(kwargs["profile_identity"], "unit-test-profile")
            self.assertEqual(kwargs["policy_identity"], "_FakePolicy")
            policies.append(policy)
            destinations.append(Path(kwargs["destination"]))
            return object()

        profile = _make_profile(created=created)
        with patch(
            "lisjong_arena.riichilab.continuous_ranked._acquire_ranked_record",
            _fake_acquire,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    record_dir="record-root",
                    max_completed_games=2,
                    sleep=_no_sleep,
                )
            )

        self.assertEqual(len(destinations), 2)
        self.assertEqual(len(set(destinations)), 2)
        self.assertTrue(
            all(path.parent == Path("record-root") for path in destinations)
        )
        self.assertEqual(policies, created)
        self.assertEqual(len(created), 2)
        self.assertEqual(summary.completed_games, 2)
        self.assertTrue(summary.records_enabled)
        self.assertEqual(summary.stopped_reason, "target_completed_games_reached")

    def test_transport_failure_during_acquisition_retries_without_counting(
        self,
    ) -> None:
        attempts = 0
        destinations: list[Path] = []
        delays: list[float] = []

        async def _fake_acquire(policy, token, **kwargs):
            nonlocal attempts
            attempts += 1
            destinations.append(Path(kwargs["destination"]))
            if attempts == 1:
                raise TransportError("temporary disconnect")
            return object()

        async def _sleep(delay: float) -> None:
            delays.append(delay)

        profile = _make_profile()
        with patch(
            "lisjong_arena.riichilab.continuous_ranked._acquire_ranked_record",
            _fake_acquire,
        ):
            summary = asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    record_dir="record-root",
                    max_completed_games=1,
                    sleep=_sleep,
                )
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(len(set(destinations)), 2)
        self.assertEqual(delays, [5.0])
        self.assertEqual(summary.failed_games, 1)
        self.assertEqual(summary.completed_games, 1)

    def test_record_finalization_failure_is_not_retried_or_counted(self) -> None:
        created: list[object] = []
        attempts = 0

        async def _fake_acquire(policy, token, **kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("strict record readback failed")

        profile = _make_profile(created=created)
        with patch(
            "lisjong_arena.riichilab.continuous_ranked._acquire_ranked_record",
            _fake_acquire,
        ):
            with self.assertRaisesRegex(RuntimeError, "strict record readback failed"):
                asyncio.run(
                    run_continuous_ranked(
                        profile,
                        "token",
                        record_dir="record-root",
                        max_completed_games=1,
                        sleep=_no_sleep,
                    )
                )

        self.assertEqual(attempts, 1)
        self.assertEqual(len(created), 1)

    def test_record_and_trace_conflict_fails_before_policy_creation(self) -> None:
        created: list[object] = []
        profile = _make_profile(created=created)

        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            asyncio.run(
                run_continuous_ranked(
                    profile,
                    "token",
                    trace_path="diagnostic.jsonl",
                    record_dir="record-root",
                    max_completed_games=1,
                )
            )

        self.assertEqual(created, [])


class BoundedCliTest(unittest.TestCase):
    def test_cli_forwards_games_and_record_dir(self) -> None:
        captured: dict[str, object] = {}

        async def _fake_continuous(profile, token, **kwargs):
            captured["profile"] = profile.name
            captured["token"] = token
            captured.update(kwargs)
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=2,
                failed_games=0,
                consecutive_failures=0,
                last_failure_type=None,
                stopped_reason="target_completed_games_reached",
                requested_completed_games=2,
                records_enabled=True,
            )

        stdout = io.StringIO()
        with patch.dict(os.environ, {_DEV_TOKEN_VAR: "secret-token"}, clear=True):
            with patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_continuous,
            ):
                with contextlib.redirect_stdout(stdout):
                    exit_code = _run_cli(
                        [
                            "--profile",
                            "lisjong-dev",
                            "--games",
                            "2",
                            "--record-dir",
                            "record-root",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["profile"], "lisjong-dev")
        self.assertEqual(captured["token"], "secret-token")
        self.assertEqual(captured["max_completed_games"], 2)
        self.assertEqual(captured["record_dir"], "record-root")
        self.assertIsNone(captured["trace_path"])
        self.assertIn("requested completed games: 2", stdout.getvalue())
        self.assertIn("records: on", stdout.getvalue())
        self.assertNotIn("secret-token", stdout.getvalue())


    def test_cli_forwards_duration_seconds(self) -> None:
        captured: dict[str, object] = {}

        async def _fake_continuous(profile, token, **kwargs):
            captured.update(kwargs)
            return ContinuousRunSummary(
                profile=profile.name,
                completed_games=1,
                failed_games=0,
                consecutive_failures=0,
                last_failure_type=None,
                stopped_reason="duration_reached",
                requested_duration_seconds=43200,
                records_enabled=True,
            )

        stdout = io.StringIO()
        with patch.dict(os.environ, {_DEV_TOKEN_VAR: "secret-token"}, clear=True):
            with patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_continuous,
            ):
                with contextlib.redirect_stdout(stdout):
                    exit_code = _run_cli(
                        [
                            "--profile",
                            "lisjong-dev",
                            "--duration-seconds",
                            "43200",
                            "--record-dir",
                            "record-root",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["max_duration_seconds"], 43200)
        self.assertIn("requested duration seconds: 43200", stdout.getvalue())
        self.assertNotIn("secret-token", stdout.getvalue())

    def test_cli_rejects_non_positive_duration(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                _run_cli(
                    [
                        "--profile",
                        "lisjong-dev",
                        "--duration-seconds",
                        "0",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--duration-seconds must be a positive integer",
            stderr.getvalue(),
        )

    def test_record_dir_conflicts_with_trace_environment_before_execution(self) -> None:
        called = False

        async def _fake_continuous(profile, token, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("runner must not start")

        stderr = io.StringIO()
        env = {
            _DEV_TOKEN_VAR: "secret-token",
            _TRACE_PATH_VAR: "diagnostic.jsonl",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "lisjong_arena.riichilab.continuous_ranked.run_continuous_ranked",
                _fake_continuous,
            ):
                with contextlib.redirect_stderr(stderr):
                    exit_code = _run_cli(
                        [
                            "--profile",
                            "lisjong-dev",
                            "--record-dir",
                            "record-root",
                        ]
                    )

        self.assertEqual(exit_code, 2)
        self.assertFalse(called)
        self.assertIn("RIICHILAB_TRACE_PATH", stderr.getvalue())
        self.assertNotIn("secret-token", stderr.getvalue())

    def test_cli_rejects_non_positive_games(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                _run_cli(["--profile", "lisjong-dev", "--games", "0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--games must be a positive integer", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
