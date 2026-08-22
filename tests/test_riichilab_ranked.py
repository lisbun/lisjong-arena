"""Arena-owned RiichiLab ranked one-game orchestration / CLI tests (Issue #17).

`RankedGameResult` / `run_ranked_game()` のcanonical implementationはArenaが所有する。
Session / transport / trace / profile等のlower-level semanticsはpin済みlisjong側の
coverageを再実装せず、fake / monkeypatchでArena orchestration boundaryだけを検証する。
live RiichiLabへは接続しない。
"""

import asyncio
import contextlib
import io
import os
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, fields
from types import SimpleNamespace
from unittest.mock import patch

import lisjong_arena
from lisjong.policies import MinimalPolicy, TwoStepUkeirePolicy
from lisjong.policy_contract.seat import Seat
from lisjong.riichilab_client import DEFAULT_RANKED_URL
from lisjong.riichilab_client.errors import ProtocolError, RiichiLabClientError

from lisjong_arena.riichilab.ranked import (
    RankedGameResult,
    _run_cli,
    run_ranked_game,
)

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_BASELINE_TOKEN_VAR = "LISJONG_BASELINE_BOT_TOKEN"
_PROD_TOKEN_VAR = "LISJONG_BOT_TOKEN"
_TRACE_PATH_VAR = "RIICHILAB_TRACE_PATH"
_ALL_TOKEN_VARS = (_DEV_TOKEN_VAR, _BASELINE_TOKEN_VAR, _PROD_TOKEN_VAR)


def _fake_result(**overrides: object) -> RankedGameResult:
    values: dict[str, object] = {
        "end_game_received": True,
        "seat": Seat.SEAT_2,
        "requests_received": 3,
        "responses_sent": 3,
        "ack_history": {},
        "scores": None,
    }
    values.update(overrides)
    return RankedGameResult(**values)


def _async_returning(result: RankedGameResult):
    async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
        return result

    return _fake_run_ranked_game


def _status(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "end_game_received": True,
        "seat": Seat.SEAT_2,
        "requests_received": 4,
        "responses_sent": 3,
        "ack_history": {7: ("accepted",)},
        "scores": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RankedGameResultContractTest(unittest.TestCase):
    def test_exact_six_fields_are_preserved(self) -> None:
        self.assertEqual(
            [field.name for field in fields(RankedGameResult)],
            [
                "end_game_received",
                "seat",
                "requests_received",
                "responses_sent",
                "ack_history",
                "scores",
            ],
        )

    def test_frozen_contract_rejects_field_reassignment(self) -> None:
        result = _fake_result()
        with self.assertRaises(FrozenInstanceError):
            result.scores = (25000, 25000, 25000, 25000)

    def test_scores_none_and_present_are_preserved(self) -> None:
        self.assertIsNone(_fake_result().scores)
        scores = (30000, 25000, 20000, 25000)
        self.assertEqual(_fake_result(scores=scores).scores, scores)

    def test_contract_contains_no_secret_fields(self) -> None:
        names = {field.name.lower() for field in fields(RankedGameResult)}
        for forbidden in ("token", "authorization", "credential", "observation"):
            self.assertNotIn(forbidden, names)

    def test_canonical_symbols_are_arena_local(self) -> None:
        self.assertEqual(RankedGameResult.__module__, "lisjong_arena.riichilab.ranked")
        self.assertEqual(run_ranked_game.__module__, "lisjong_arena.riichilab.ranked")
        self.assertFalse(hasattr(lisjong_arena, "RankedGameResult"))
        self.assertFalse(hasattr(lisjong_arena, "run_ranked_game"))


class RunRankedGameTest(unittest.TestCase):
    def _run_with_status(
        self,
        status: SimpleNamespace,
        *,
        url: str = "wss://example.invalid/ranked",
        trace_path: str | None = None,
        drive_error: Exception | None = None,
    ) -> tuple[RankedGameResult | None, dict[str, object]]:
        captured: dict[str, object] = {}

        class _FakeSession:
            def __init__(self, policy: object) -> None:
                captured["policy"] = policy

            def status(self) -> SimpleNamespace:
                return status

        @asynccontextmanager
        async def _fake_connect(captured_url: str, token: str):
            captured["url"] = captured_url
            captured["token"] = token
            transport = object()
            captured["transport"] = transport
            yield transport

        async def _fake_drive(session: object, transport: object, *, trace=None):
            captured["drive_session"] = session
            captured["drive_transport"] = transport
            captured["drive_trace"] = trace
            if drive_error is not None:
                raise drive_error

        class _FakeWriter:
            def __init__(self, path: object) -> None:
                captured["trace_path"] = path
                captured["writer"] = self
                self.closed = False

            def close(self) -> None:
                self.closed = True

        with (
            patch("lisjong_arena.riichilab.ranked.RankedSession", _FakeSession),
            patch(
                "lisjong_arena.riichilab.ranked.connect_ranked_transport",
                _fake_connect,
            ),
            patch("lisjong_arena.riichilab.ranked.drive_ranked_session", _fake_drive),
            patch(
                "lisjong_arena.riichilab.ranked.JsonlProtocolTraceWriter",
                _FakeWriter,
            ),
        ):
            if drive_error is not None:
                with self.assertRaises(type(drive_error)):
                    asyncio.run(
                        run_ranked_game(
                            MinimalPolicy(),
                            "unit-test-token",
                            url=url,
                            trace_path=trace_path,
                        )
                    )
                return None, captured

            result = asyncio.run(
                run_ranked_game(
                    MinimalPolicy(),
                    "unit-test-token",
                    url=url,
                    trace_path=trace_path,
                )
            )
            return result, captured

    def test_policy_token_url_and_status_are_forwarded(self) -> None:
        scores = (30000, 25000, 20000, 25000)
        result, captured = self._run_with_status(_status(scores=scores))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsInstance(captured["policy"], MinimalPolicy)
        self.assertEqual(captured["token"], "unit-test-token")
        self.assertEqual(captured["url"], "wss://example.invalid/ranked")
        self.assertIs(captured["drive_transport"], captured["transport"])
        self.assertTrue(result.end_game_received)
        self.assertEqual(result.seat, Seat.SEAT_2)
        self.assertEqual(result.requests_received, 4)
        self.assertEqual(result.responses_sent, 3)
        self.assertEqual(result.ack_history, {7: ("accepted",)})
        self.assertEqual(result.scores, scores)

    def test_default_url_uses_pinned_lisjong_public_constant(self) -> None:
        captured: dict[str, object] = {}

        class _FakeSession:
            def __init__(self, policy: object) -> None:
                pass

            def status(self) -> SimpleNamespace:
                return _status()

        @asynccontextmanager
        async def _fake_connect(url: str, token: str):
            captured["url"] = url
            yield object()

        async def _fake_drive(session: object, transport: object, *, trace=None):
            return None

        with (
            patch("lisjong_arena.riichilab.ranked.RankedSession", _FakeSession),
            patch(
                "lisjong_arena.riichilab.ranked.connect_ranked_transport",
                _fake_connect,
            ),
            patch("lisjong_arena.riichilab.ranked.drive_ranked_session", _fake_drive),
        ):
            asyncio.run(run_ranked_game(MinimalPolicy(), "unit-test-token"))

        self.assertEqual(captured["url"], DEFAULT_RANKED_URL)

    def test_trace_disabled_does_not_create_writer(self) -> None:
        result, captured = self._run_with_status(_status(), trace_path=None)
        self.assertIsNotNone(result)
        self.assertNotIn("writer", captured)
        self.assertIsNone(captured["drive_trace"])

    def test_trace_enabled_creates_and_closes_writer_on_success(self) -> None:
        result, captured = self._run_with_status(
            _status(), trace_path="trace/unit-test.jsonl"
        )
        self.assertIsNotNone(result)
        self.assertEqual(captured["trace_path"], "trace/unit-test.jsonl")
        self.assertIs(captured["drive_trace"], captured["writer"])
        self.assertTrue(captured["writer"].closed)

    def test_trace_writer_closes_when_lower_level_runtime_fails(self) -> None:
        result, captured = self._run_with_status(
            _status(),
            trace_path="trace/unit-test.jsonl",
            drive_error=RiichiLabClientError("boom"),
        )
        self.assertIsNone(result)
        self.assertTrue(captured["writer"].closed)

    def test_scores_none_is_preserved(self) -> None:
        result, _ = self._run_with_status(_status(scores=None))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result.scores)

    def test_missing_bound_seat_fails_closed(self) -> None:
        with self.assertRaises(ProtocolError):
            self._run_with_status(_status(seat=None))

    def test_non_string_and_empty_token_are_rejected_before_session_creation(
        self,
    ) -> None:
        for token in (None, b"token", True, 0, ""):
            with self.subTest(token=token):
                with patch(
                    "lisjong_arena.riichilab.ranked.RankedSession",
                    side_effect=AssertionError("session must not be created"),
                ):
                    with self.assertRaises(ValueError):
                        asyncio.run(run_ranked_game(MinimalPolicy(), token))


class CliArgumentFailClosedTest(unittest.TestCase):
    def test_missing_profile_exits_2(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--profile", stderr.getvalue())

    def test_unknown_profile_exits_2(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            _run_cli(["--profile", "lisjong-production"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_missing_credential_exits_2_and_names_only_its_own_env_var(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for var in _ALL_TOKEN_VARS:
                os.environ.pop(var, None)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return_code = _run_cli(["--profile", "lisjong-dev"])

        message = stderr.getvalue()
        self.assertEqual(return_code, 2)
        self.assertIn(_DEV_TOKEN_VAR, message)
        self.assertNotIn(_BASELINE_TOKEN_VAR, message)
        self.assertNotIn(_PROD_TOKEN_VAR, message)


class ModuleCliRuntimeWarningTest(unittest.TestCase):
    def test_module_execution_does_not_emit_runtime_warning(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "lisjong_arena.riichilab.ranked"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--profile", completed.stderr)
        self.assertNotIn("RuntimeWarning", completed.stderr)


class CompositionForwardingTest(unittest.TestCase):
    def test_resolved_policy_and_token_reach_arena_local_run_ranked_game(self) -> None:
        captured: dict[str, object] = {}

        async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            captured["policy"] = policy
            captured["token"] = token
            captured.update(kwargs)
            return _fake_result()

        with (
            patch.dict(
                os.environ,
                {
                    _PROD_TOKEN_VAR: "unit-test-dummy-token",
                    _TRACE_PATH_VAR: "",
                },
            ),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(["--profile", "lisjong"])

        self.assertEqual(return_code, 0)
        self.assertIsInstance(captured["policy"], MinimalPolicy)
        self.assertEqual(captured["token"], "unit-test-dummy-token")
        self.assertIsNone(captured.get("trace_path"))


class ProfilePolicyFactoryReuseTest(unittest.TestCase):
    def test_lisjong_dev_profile_uses_its_existing_policy_factory_as_is(self) -> None:
        captured_policies: list[object] = []

        async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            captured_policies.append(policy)
            return _fake_result()

        with (
            patch.dict(os.environ, {_DEV_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 0)
        self.assertEqual(len(captured_policies), 1)
        self.assertIsInstance(captured_policies[0], TwoStepUkeirePolicy)


class TraceForwardingTest(unittest.TestCase):
    def _run_capturing_trace_path(
        self, argv: list[str], *, env_overrides: dict[str, str] | None = None
    ) -> dict[str, object]:
        captured: dict[str, object] = {}

        async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            captured.update(kwargs)
            return _fake_result()

        env = {
            _PROD_TOKEN_VAR: "unit-test-dummy-token",
            _TRACE_PATH_VAR: "",
        }
        if env_overrides:
            env.update(env_overrides)

        with (
            patch.dict(os.environ, env),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = _run_cli(argv)

        self.assertEqual(return_code, 0)
        return captured

    def test_default_trace_path_is_none(self) -> None:
        captured = self._run_capturing_trace_path(["--profile", "lisjong"])
        self.assertIsNone(captured.get("trace_path"))

    def test_explicit_trace_path_argument_is_forwarded(self) -> None:
        captured = self._run_capturing_trace_path(
            ["--profile", "lisjong", "--trace-path", "explicit-trace.jsonl"]
        )
        self.assertEqual(captured.get("trace_path"), "explicit-trace.jsonl")

    def test_trace_path_env_var_is_forwarded(self) -> None:
        captured = self._run_capturing_trace_path(
            ["--profile", "lisjong"],
            env_overrides={_TRACE_PATH_VAR: "env-trace.jsonl"},
        )
        self.assertEqual(captured.get("trace_path"), "env-trace.jsonl")


class ResultRenderingTest(unittest.TestCase):
    def test_success_output_reports_seat_requests_responses_and_end_game(self) -> None:
        result = _fake_result(
            seat=Seat.SEAT_1, requests_received=7, responses_sent=7, scores=None
        )
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {_PROD_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _async_returning(result),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            return_code = _run_cli(["--profile", "lisjong"])

        output = stdout.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("seat: 1", output)
        self.assertIn("requests: 7", output)
        self.assertIn("responses: 7", output)
        self.assertIn("end_game: yes", output)
        self.assertIn("scores: unavailable", output)

    def test_success_output_reports_scores_when_available(self) -> None:
        result = _fake_result(scores=(30000, 25000, 20000, 25000))
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {_PROD_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _async_returning(result),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            return_code = _run_cli(["--profile", "lisjong"])

        self.assertEqual(return_code, 0)
        self.assertIn("scores: 30000, 25000, 20000, 25000", stdout.getvalue())


class ErrorHandlingTest(unittest.TestCase):
    def test_riichilab_client_error_returns_1(self) -> None:
        async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            raise RiichiLabClientError("boom")

        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_PROD_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(["--profile", "lisjong"])

        self.assertEqual(return_code, 1)
        self.assertIn("RiichiLabClientError", stderr.getvalue())

    def test_unexpected_exception_propagates_instead_of_being_swallowed(self) -> None:
        async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            raise ValueError("unexpected failure")

        with (
            patch.dict(os.environ, {_PROD_TOKEN_VAR: "unit-test-dummy-token"}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(ValueError):
                _run_cli(["--profile", "lisjong"])


class SecretSafetyTest(unittest.TestCase):
    def test_dummy_token_never_appears_in_stdout_or_stderr(self) -> None:
        dummy_token = "unit-test-dummy-token-should-not-leak"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {_PROD_TOKEN_VAR: dummy_token}),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                _async_returning(_fake_result()),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = _run_cli(["--profile", "lisjong"])

        self.assertEqual(return_code, 0)
        self.assertNotIn(dummy_token, stdout.getvalue())
        self.assertNotIn(dummy_token, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
