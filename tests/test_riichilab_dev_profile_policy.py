"""Issue #223: RiichiLab lisjong-dev profileのcurrent Policy mappingを固定する。"""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from lisjong.policies import MechanismRiichiDefenseYakuhaiCallPolicy, MinimalPolicy
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab.profile import (
    build_runtime_summary,
    format_runtime_summary,
    resolve_profile,
)
from lisjong_arena.riichilab.ranked import (
    RankedGameResult,
    _run_cli as run_ranked_cli,
)
from lisjong_arena.riichilab.validation import (
    ValidationResult,
    _run_cli as run_validation_cli,
)

_DEV_TOKEN_VAR = "LISJONG_DEV_BOT_TOKEN"
_TRACE_PATH_VAR = "RIICHILAB_TRACE_PATH"


class DevProfileMappingTest(unittest.TestCase):
    def test_lisjong_dev_maps_to_exact_mechanism_policy(self) -> None:
        profile = resolve_profile("lisjong-dev")

        self.assertEqual(profile.credential_env_var, _DEV_TOKEN_VAR)
        self.assertEqual(profile.runtime_namespace, "lisjong-dev")
        self.assertIs(
            type(profile.policy_factory()), MechanismRiichiDefenseYakuhaiCallPolicy
        )
        self.assertIsNot(profile.policy_factory(), profile.policy_factory())

    def test_other_profile_policy_mappings_are_unchanged(self) -> None:
        self.assertIs(
            type(resolve_profile("lisjong-baseline").policy_factory()), MinimalPolicy
        )
        self.assertIs(type(resolve_profile("lisjong").policy_factory()), MinimalPolicy)

    def test_runtime_summary_reports_the_actual_mechanism_policy(self) -> None:
        profile = resolve_profile("lisjong-dev")
        policy = profile.policy_factory()
        summary = build_runtime_summary(
            profile, mode="ranked", trace_path=None, policy=policy
        )

        self.assertEqual(
            summary.policy_label, "MechanismRiichiDefenseYakuhaiCallPolicy"
        )
        self.assertIn(
            "policy: MechanismRiichiDefenseYakuhaiCallPolicy",
            format_runtime_summary(summary),
        )


class DevProfileCliCompositionTest(unittest.TestCase):
    def test_ranked_cli_passes_exact_mechanism_policy_to_one_game_runner(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        async def fake_run_ranked_game(policy: object, token: str, **kwargs: object):
            captured["policy"] = policy
            captured["token"] = token
            return RankedGameResult(
                end_game_received=True,
                seat=Seat.SEAT_0,
                requests_received=1,
                responses_sent=1,
                ack_history={},
                scores=None,
            )

        with (
            patch.dict(
                os.environ,
                {_DEV_TOKEN_VAR: "unit-test-token", _TRACE_PATH_VAR: ""},
            ),
            patch(
                "lisjong_arena.riichilab.ranked.run_ranked_game",
                fake_run_ranked_game,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = run_ranked_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 0)
        self.assertIs(type(captured["policy"]), MechanismRiichiDefenseYakuhaiCallPolicy)
        self.assertEqual(captured["token"], "unit-test-token")

    def test_validation_cli_passes_exact_mechanism_policy_to_validator(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        async def fake_run_validation(policy: object, token: str, **kwargs: object):
            captured["policy"] = policy
            captured["token"] = token
            return ValidationResult(
                passed=True,
                validation_result_received=True,
                end_game_received=True,
                failure_reason=None,
                requests_received=1,
                responses_sent=1,
                ack_history={},
            )

        with (
            patch.dict(
                os.environ,
                {_DEV_TOKEN_VAR: "unit-test-token", _TRACE_PATH_VAR: ""},
            ),
            patch(
                "lisjong_arena.riichilab.validation.run_validation",
                fake_run_validation,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return_code = run_validation_cli(["--profile", "lisjong-dev"])

        self.assertEqual(return_code, 0)
        self.assertIs(type(captured["policy"]), MechanismRiichiDefenseYakuhaiCallPolicy)
        self.assertEqual(captured["token"], "unit-test-token")


if __name__ == "__main__":
    unittest.main()
