"""実RiichiEnvを使うArena-local ``LocalGameRunner``のsmall integration test。

Issue #31のrunner ownership migrationに必要な最小限のreal RiichiEnv
integrationだけをここへ置く。Issue #51以降、lisjong-owned individual Policyの
heavyな半荘compatibility testはArena default suiteでPolicyごとに保持しない。
``ShantenPolicy``は既存AABB / ABBB real-RiichiEnv integrationで検証する。
本fileではfixed-seed single-gameのresult / GameTrace / same-process inspection
再現性を、既存の2 runを増やさず確認する。GameTraceはIssue #43でArena-local
``lisjong_arena.game_trace``へ切り替えた。
"""

import unittest

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

_SEED = 12345


class LocalGameRunnerIntegrationTest(unittest.TestCase):
    def test_fixed_seed_single_game_completes_and_is_reproducible(self) -> None:
        first_recorder = LocalGameInspectionRecorder()
        first = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
            inspection_recorder=first_recorder,
        ).run()
        second_recorder = LocalGameInspectionRecorder()
        second = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=_SEED,
            game_mode="4p-red-single",
            max_steps=10_000,
            inspection_recorder=second_recorder,
        ).run()
        first_inspection = first_recorder.snapshot()
        second_inspection = second_recorder.snapshot()
        first_trace = first_inspection.game_trace
        second_trace = second_inspection.game_trace

        self.assertEqual(first, second)
        self.assertEqual(first_inspection, second_inspection)
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
        self.assertIs(first_inspection.result, first)
        self.assertEqual(len(first_inspection.step_observations), first.steps)
        self.assertEqual(
            sum(
                len(step.seat_decisions) for step in first_inspection.step_observations
            ),
            first.decisions,
        )
        previous_end = 0
        for ordinal, step in enumerate(first_inspection.step_observations):
            self.assertEqual(step.step_ordinal, ordinal)
            self.assertGreaterEqual(step.event_sequence_start, previous_end)
            self.assertLessEqual(step.event_sequence_end, len(first_trace.events))
            for decision in step.seat_decisions:
                self.assertEqual(decision.policy_input.self_seat, decision.seat)
                self.assertEqual(
                    decision.decision_trace.selected_action.actor,
                    decision.seat,
                )
                self.assertIsNone(decision.decision_trace.analysis)
            previous_end = step.event_sequence_end


if __name__ == "__main__":
    unittest.main()
