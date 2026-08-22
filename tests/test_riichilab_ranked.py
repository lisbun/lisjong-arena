"""RiichiLab ranked first-party entry pointのcomposition test(Issue #15)。

`lisjong_arena.riichilab.ranked`は既存`lisjong.riichilab_client`のpublic
helpers/primitivesを組み合わせるだけの薄いcomposition layerである。profile
定義、credential解決、trace path優先順位、transport自体はlisjong側で既に
固定されているためここでは再検証せず、wrapperが解決済みのPolicy・token・
trace_pathを`run_ranked_game()`へ正しく橋渡しすることと、fail closed /
secret-safeな挙動を重点的に確認する。live RiichiLabへは接続しない。
"""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from lisjong.policies import MinimalPolicy, TwoStepUkeirePolicy
from lisjong.policy_contract.seat import Seat
from lisjong.riichilab_client.errors import RiichiLabClientError
from lisjong.riichilab_client.ranked import RankedGameResult

from lisjong_arena.riichilab.ranked import _run_cli

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_BASELINE_TOKEN_VAR = "LISJONG_BASELINE_BOT_TOKEN"
_PROD_TOKEN_VAR = "LISJONG_BOT_TOKEN"
_TRACE_PATH_VAR = "RIICHILAB_TRACE_PATH"
_ALL_TOKEN_VARS = (_DEV_TOKEN_VAR, _BASELINE_TOKEN_VAR, _PROD_TOKEN_VAR)


def _fake_result(**overrides: object) -> RankedGameResult:
    fields: dict[str, object] = {
        "end_game_received": True,
        "seat": Seat.SEAT_2,
        "requests_received": 3,
        "responses_sent": 3,
        "ack_history": {},
        "scores": None,
    }
    fields.update(overrides)
    return RankedGameResult(**fields)


def _async_returning(result: RankedGameResult):
    async def _fake_run_ranked_game(policy: object, token: str, **kwargs: object):
        return result

    return _fake_run_ranked_game


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
    def test_resolved_policy_and_token_reach_run_ranked_game(self) -> None:
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
    def test_success_output_reports_seat_requests_responses_and_end_game(
        self,
    ) -> None:
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
