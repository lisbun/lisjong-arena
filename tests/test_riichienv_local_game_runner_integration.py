"""実RiichiEnvを使うArena-local ``LocalGameRunner``のsmall integration test。

Issue #31のrunner ownership migrationに必要な最小限のreal RiichiEnv
integrationだけをここへ置く。``ShantenPolicy`` / ``UkeirePolicy`` /
``TwoStepUkeirePolicy``のようなPolicy-specific heavy半荘integrationは
lisjong側``tests/test_local_game_runner_integration.py``に残し、ここへは
複製しない。既存AABB / ABBB real-RiichiEnv integrationは
``tests/test_comparison_integration.py`` /
``tests/test_single_round_evaluation_integration.py``がArena-local runnerへ
切り替えた状態ですでに検証している。
"""

import unittest

from lisjong.game_trace import GameTraceRecorder
from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

_SEED = 12345


class LocalGameRunnerIntegrationTest(unittest.TestCase):
    def test_fixed_seed_single_game_completes_and_is_reproducible(self) -> None:
        recorder = GameTraceRecorder()
        first = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
            trace_sink=recorder,
        ).run()
        second = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
        ).run()
        trace = recorder.snapshot()

        self.assertEqual(first, second)
        self.assertEqual(first.seed, _SEED)
        self.assertEqual(first.game_mode, "4p-red-single")
        self.assertGreater(first.steps, 0)
        self.assertGreaterEqual(first.decisions, first.steps)
        self.assertEqual(sum(first.scores), 100_000)
        self.assertEqual(sorted(first.ranks), [1, 2, 3, 4])
        self.assertEqual(trace.seed, first.seed)
        self.assertEqual(trace.game_mode, first.game_mode)
        self.assertGreater(len(trace.events), 0)


if __name__ == "__main__":
    unittest.main()
