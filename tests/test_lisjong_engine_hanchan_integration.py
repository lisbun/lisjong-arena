"""first-party engine上でreal lisjong Policy 4体の半荘を完走させるend-to-end test。

engine側fake selectorではなく、Arena bridgeが実際に`execute_policy()`を経由して
4席すべてのdecisionを解決することを固定する。evaluation protocolの検証ではなく、
first-party execution capabilityの成立確認である。

1本の半荘実行は数秒かかるため、observation / decision / descriptorの観測は
`setUpClass`の1回の実行へまとめる。再現性確認だけは、共有stateへ依存しないよう
独立した2回の実行で固定する。
"""

import unittest

from lisjong.policies.minimal import MinimalPolicy
from lisjong.policy_contract import DecisionContext
from lisjong_engine.action_descriptor import ACTION_DESCRIPTOR_TYPES
from lisjong_engine.match_state import CompletedMatch
from lisjong_engine.observation import SeatObservation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan
from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector

_SEED = 20260824


class _CountingPolicy:
    """実`MinimalPolicy`へ委譲しつつ、Policy経由のdecision数を数えるwrapper。"""

    def __init__(self) -> None:
        self._policy = MinimalPolicy()
        self.decisions: list[DecisionContext] = []

    def choose_action(self, decision: DecisionContext):
        self.decisions.append(decision)
        return self._policy.choose_action(decision)


class _RecordingSelector(PolicySeatSelector):
    """selectorが受け取ったengine値と返したdescriptorを記録するwrapper。"""

    def __init__(self, seat, policy, log) -> None:
        super().__init__(seat, policy)
        self._log = log

    def __call__(self, observation, options):
        chosen = super().__call__(observation, options)
        self._log.append((observation, tuple(options), chosen))
        return chosen


class MinimalPolicyHanchanTest(unittest.TestCase):
    """`MinimalPolicy` x 4、fixed RuleSet、fixed seedの1半荘を観測する。"""

    @classmethod
    def setUpClass(cls) -> None:
        from lisjong_engine.driver import run_hanchan
        from lisjong_engine.match_state import MatchState

        cls.log: list[tuple] = []
        cls.policies = {seat: _CountingPolicy() for seat in EngineSeat}
        selectors = {
            seat: _RecordingSelector(seat, cls.policies[seat], cls.log)
            for seat in EngineSeat
        }
        cls.completed = run_hanchan(
            MatchState(seed=_SEED, rules=RuleSet.default()),
            selectors,
        )

    def test_reaches_a_completed_match(self) -> None:
        self.assertIsInstance(self.completed, CompletedMatch)
        self.assertTrue(self.completed.history)
        self.assertEqual(len(self.completed.final_score.players), 4)
        self.assertEqual(
            {player.rank for player in self.completed.final_score.players},
            {1, 2, 3, 4},
        )

    def test_every_seat_decides_through_execute_policy(self) -> None:
        for seat, policy in self.policies.items():
            with self.subTest(seat=seat):
                self.assertGreater(len(policy.decisions), 0)

    def test_every_policy_call_receives_a_decision_context_for_its_own_seat(
        self,
    ) -> None:
        for seat, policy in self.policies.items():
            expected_seat = int(list(EngineSeat).index(seat))
            for decision in policy.decisions:
                self.assertIsInstance(decision, DecisionContext)
                self.assertEqual(int(decision.input.self_seat), expected_seat)

    def test_every_selector_call_returns_one_of_the_offered_descriptors(self) -> None:
        self.assertTrue(self.log)
        for observation, options, chosen in self.log:
            self.assertIsInstance(observation, SeatObservation)
            self.assertIsInstance(chosen, ACTION_DESCRIPTOR_TYPES)
            self.assertIn(chosen, options)

    def test_selector_calls_and_policy_calls_are_one_to_one(self) -> None:
        """1 decisionにつき`execute_policy()`を1回だけ呼ぶ。"""
        policy_calls = sum(len(policy.decisions) for policy in self.policies.values())
        self.assertEqual(policy_calls, len(self.log))


class DeterminismTest(unittest.TestCase):
    def test_the_same_seed_and_policies_reproduce_the_same_completed_match(
        self,
    ) -> None:
        first = run_policy_hanchan(
            {seat: MinimalPolicy() for seat in EngineSeat}, seed=_SEED
        )
        second = run_policy_hanchan(
            {seat: MinimalPolicy() for seat in EngineSeat}, seed=_SEED
        )
        self.assertEqual(first, second)


class RunPolicyHanchanValidationTest(unittest.TestCase):
    """半荘を開始する前にfail closedする入力条件。"""

    def test_rejects_a_non_int_seed(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        with self.assertRaises(TypeError):
            run_policy_hanchan(policies, seed="0")

    def test_rejects_a_non_ruleset(self) -> None:
        policies = {seat: MinimalPolicy() for seat in EngineSeat}
        with self.assertRaises(TypeError):
            run_policy_hanchan(policies, seed=_SEED, rules=object())

    def test_missing_seat_policy_fails_before_starting_a_match(self) -> None:
        policies = {
            seat: MinimalPolicy() for seat in EngineSeat if seat is not EngineSeat.WEST
        }
        with self.assertRaises(ValueError):
            run_policy_hanchan(policies, seed=_SEED)


if __name__ == "__main__":
    unittest.main()
