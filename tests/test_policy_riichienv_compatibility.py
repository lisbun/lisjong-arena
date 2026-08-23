"""``UkeirePolicy`` / ``TwoStepUkeirePolicy``の実RiichiEnv compatibility test。

Issue #33: これらのPolicyがreal RiichiEnv 0.4.8上で固定seed半荘を完走できるという
cross-layer compatibility coverageを、lisjong `tests/test_local_game_runner_integration.py`
からArena-local `LocalGameRunner`へre-homeしたもの。Policyの強さ・score・rankは
評価しない。``ShantenPolicy`` / ``MinimalPolicy`` / runner trace / deterministic
replayのcoverageは既存Arena testが担うため、ここへは複製しない。
"""

import unittest

from lisjong.policies import TwoStepUkeirePolicy, UkeirePolicy
from lisjong.policy_contract import Seat

from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

_SEED = 12345
_GAME_MODE = "4p-red-half"
_MAX_STEPS = 10_000


class UkeirePolicyRiichiEnvCompatibilityTest(unittest.TestCase):
    def test_fixed_seed_half_game_completes_with_ukeire_policy(self) -> None:
        result = LocalGameRunner(
            {seat: UkeirePolicy() for seat in Seat},
            seed=_SEED,
            game_mode=_GAME_MODE,
            max_steps=_MAX_STEPS,
        ).run()

        self.assertEqual(result.seed, _SEED)
        self.assertEqual(result.game_mode, _GAME_MODE)
        self.assertGreater(result.steps, 1)
        self.assertGreater(result.decisions, result.steps)


class TwoStepUkeirePolicyRiichiEnvCompatibilityTest(unittest.TestCase):
    def test_fixed_seed_half_game_completes_with_two_step_ukeire_policy(self) -> None:
        result = LocalGameRunner(
            {seat: TwoStepUkeirePolicy() for seat in Seat},
            seed=_SEED,
            game_mode=_GAME_MODE,
            max_steps=_MAX_STEPS,
        ).run()

        self.assertEqual(result.seed, _SEED)
        self.assertEqual(result.game_mode, _GAME_MODE)
        self.assertGreater(result.steps, 1)
        self.assertGreater(result.decisions, result.steps)


if __name__ == "__main__":
    unittest.main()
