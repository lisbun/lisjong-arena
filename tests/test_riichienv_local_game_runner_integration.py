"""実RiichiEnvを使うArena-local ``LocalGameRunner``のsmall integration test。

Issue #31のrunner ownership migrationに必要な最小限のreal RiichiEnv
integrationだけをここへ置く。``UkeirePolicy`` / ``TwoStepUkeirePolicy``の
Policy-specific heavy半荘compatibility coverageはIssue #33で
``tests/test_policy_riichienv_compatibility.py``へre-home済みであり、
``ShantenPolicy``は既存AABB / ABBB real-RiichiEnv integrationで検証する。
本fileではfixed-seed single-gameのresult / GameTrace再現性を確認する。
GameTraceはIssue #43でArena-local``lisjong_arena.game_trace``へ切り替えた。
"""

import unittest

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.game_trace import GameTraceRecorder
from lisjong_arena.riichienv.local_game_runner import LocalGameRunner

_SEED = 12345


class LocalGameRunnerIntegrationTest(unittest.TestCase):
    def test_fixed_seed_single_game_completes_and_is_reproducible(self) -> None:
        first_recorder = GameTraceRecorder()
        first = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
            trace_sink=first_recorder,
        ).run()
        second_recorder = GameTraceRecorder()
        second = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
            trace_sink=second_recorder,
        ).run()
        first_trace = first_recorder.snapshot()
        second_trace = second_recorder.snapshot()

        self.assertEqual(first, second)
        self.assertEqual(first_trace, second_trace)
        self.assertEqual(first.seed, _SEED)
        self.assertEqual(first.game_mode, "4p-red-single")
        self.assertGreater(first.steps, 0)
        self.assertGreaterEqual(first.decisions, first.steps)
        self.assertEqual(sum(first.scores), 100_000)
        self.assertEqual(sorted(first.ranks), [1, 2, 3, 4])
        self.assertEqual(first_trace.seed, first.seed)
        self.assertEqual(first_trace.game_mode, first.game_mode)
        self.assertGreater(len(first_trace.events), 0)


if __name__ == "__main__":
    unittest.main()
