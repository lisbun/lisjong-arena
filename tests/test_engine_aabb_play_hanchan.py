"""#514 worker内の1半荘実行（``play_hanchan``）。

Rust wheelのない環境でも試せるよう、実行環境の取得とfactoryの解決だけを差し替える。
2つ目のtestは実engineの1半荘を、提示候補の最後を選ぶtest用Policyで回す
（lisjongの実Policyの挙動・棋力の確認ではない）。
"""

import unittest
from unittest import mock

from _engine_aabb_protocol_fixtures import CANDIDATE, ENVIRONMENT, INCUMBENT, WHEEL_PATH
from _engine_aabb_record_fixtures import SCRIPT_Y, play

from lisjong_arena.heuristic_candidate_engine_aabb import executor
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    HanchanTask,
    play_hanchan,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import locked_rules
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import build_records


class _LastActionPolicy:
    created: list = []

    def __init__(self, identity):
        self.identity = identity
        _LastActionPolicy.created.append(self)

    def choose_action(self, decision):
        return decision.legal_actions[-1]


def _factory(participant):
    return lambda: _LastActionPolicy(participant.policy_identity)


def _task(seed, placement):
    return HanchanTask(
        seed=seed,
        placement=placement,
        candidate=CANDIDATE.to_document(),
        incumbent=INCUMBENT.to_document(),
        wheel_path=WHEEL_PATH,
    )


class PlayHanchanCompositionTest(unittest.TestCase):
    def setUp(self):
        _LastActionPolicy.created = []
        patches = (
            mock.patch.object(
                executor, "environment_document", return_value=ENVIRONMENT
            ),
            mock.patch.object(executor, "resolve_factory", side_effect=_factory),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_fresh_policy_per_seat_and_hanchan_with_locked_rules(self):
        calls = []

        def fake_run(
            policies, *, seed, rules, on_round_evidence_complete, on_selector_call
        ):
            calls.append(
                (policies, seed, rules, on_round_evidence_complete, on_selector_call)
            )
            return play(seed, SCRIPT_Y)

        with mock.patch.object(executor, "run_policy_hanchan", side_effect=fake_run):
            first = play_hanchan(_task(7, 1))
            second = play_hanchan(_task(7, 1))
        self.assertEqual(len(_LastActionPolicy.created), 8)
        self.assertEqual(len({id(item) for item in _LastActionPolicy.created}), 8)
        policies, seed, rules, evidence, selector = calls[0]
        self.assertEqual(seed, 7)
        self.assertEqual(rules, locked_rules())
        self.assertIsNotNone(evidence)
        self.assertIsNotNone(selector)
        # placement 1: Aは seat 0, 2
        identities = [proxy._policy.identity for proxy in policies.values()]
        self.assertEqual(
            identities,
            [CANDIDATE.policy_identity, INCUMBENT.policy_identity] * 2,
        )
        self.assertEqual(
            [seat.role for seat in first.capture.seats], ["A", "B", "A", "B"]
        )
        self.assertEqual(first.environment, ENVIRONMENT)
        self.assertIsNot(calls[0][0], calls[1][0])
        del second

    def test_real_engine_hanchan_counts_selector_and_policy_calls(self):
        played = play_hanchan(_task(513000, 0))
        capture = played.capture
        self.assertEqual(capture.selector_call_counts, capture.policy_call_counts)
        self.assertTrue(all(count > 0 for count in capture.policy_call_counts))
        self.assertEqual(len(_LastActionPolicy.created), 4)
        rows, events, status = build_records([capture], event_facts_required=True)
        self.assertTrue(status.valid)
        self.assertEqual(len(rows[0]["kyokus"]), len(capture.match.history))

    def test_real_engine_hanchan_canonicalizes_match_and_actions(self):
        """#517 P2: 実engineの ``CompletedMatch`` とPolicyの行動を正規化できること。"""
        from lisjong_arena.heuristic_candidate_engine_aabb.backend_check import (
            canonical_engine_value,
        )

        actions = []
        capture = executor.play_task(
            _task(513000, 0),
            on_action=lambda seat, action: actions.append(
                [seat.name, canonical_engine_value(action)]
            ),
        )
        self.assertEqual(len(actions), sum(capture.policy_call_counts))
        completed = canonical_engine_value(capture.match)
        self.assertEqual(completed["__type"], "CompletedMatch")
        self.assertEqual(canonical_engine_value(capture.match), completed)


if __name__ == "__main__":
    unittest.main()
