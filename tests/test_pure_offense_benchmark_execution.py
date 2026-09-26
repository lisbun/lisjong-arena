"""Issue #389 benchmark arm executionのunit test。

単一game実行境界``_run_benchmark_game``だけを差し替え、実RiichiEnvを起動せずに
rotation / Policy assignment / fresh instance / 実行順序 / fail closedを固定する。
"""

import unittest
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    PassiveTsumogiriPolicy,
)
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.pure_offense_benchmark import execution
from lisjong_arena.pure_offense_benchmark.protocol import MAX_STEPS
from lisjong_arena.pure_offense_benchmark.record import derive_kyoku_offense_facts
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.single_round_evaluation import SingleRoundEvaluationError

_FOCAL = PolicySpec(identity="minimal", factory=MinimalPolicy)
_SCORES = (25000, 25000, 25000, 25000)
_DRAW_EVENTS = [
    {"type": "start_kyoku", "oya": 0},
    {"type": "ryukyoku", "reason": "kyushu_kyuhai", "deltas": [0, 0, 0, 0]},
]


def _fake_result(seed: int) -> LocalGameResult:
    return LocalGameResult(
        seed=seed,
        game_mode="4p-red-single",
        scores=_SCORES,
        ranks=(1, 2, 3, 4),
        steps=1,
        decisions=1,
        seat_round_stats=neutral_seat_round_stats_tuple(_SCORES),
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict]] = []

    def __call__(self, policies, *, seed, max_steps):
        self.calls.append((seed, dict(policies)))
        return _fake_result(seed), derive_kyoku_offense_facts(_DRAW_EVENTS)


class BenchmarkPlanTest(unittest.TestCase):
    def test_plan_uses_existing_passive_opponent_and_fixed_max_steps(self) -> None:
        plan = execution.benchmark_plan(_FOCAL, (3, 1))
        self.assertEqual(plan.baseline.identity, PASSIVE_TSUMOGIRI_IDENTITY)
        self.assertEqual(plan.max_steps, MAX_STEPS)
        self.assertEqual(plan.seeds, (3, 1))

    def test_focal_must_not_be_the_opponent(self) -> None:
        opponent = PolicySpec(
            identity=PASSIVE_TSUMOGIRI_IDENTITY, factory=PassiveTsumogiriPolicy
        )
        with self.assertRaises(ValueError):
            execution.benchmark_plan(opponent, (1,))

    def test_run_rejects_non_benchmark_plans(self) -> None:
        wrong_opponent = SingleRoundEvaluationPlan(
            candidate=_FOCAL,
            baseline=PolicySpec(identity="other", factory=MinimalPolicy),
            seeds=(1,),
            max_steps=MAX_STEPS,
        )
        wrong_steps = SingleRoundEvaluationPlan(
            candidate=_FOCAL,
            baseline=execution.benchmark_plan(_FOCAL, (1,)).baseline,
            seeds=(1,),
            max_steps=5,
        )
        for plan in (wrong_opponent, wrong_steps):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                execution.run_benchmark_arm(plan, max_workers=1)


class RunBenchmarkArmSerialTest(unittest.TestCase):
    def test_rotation_order_assignment_and_fresh_instances(self) -> None:
        recorder = _Recorder()
        progress: list[tuple[int, int]] = []
        with mock.patch.object(execution, "_run_benchmark_game", recorder):
            arm = execution.run_benchmark_arm(
                execution.benchmark_plan(_FOCAL, (9, 4)),
                max_workers=1,
                progress_callback=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(
            [(item.seed, item.rotation) for item in arm.evaluation.game_results],
            [(9, 0), (9, 1), (9, 2), (9, 3), (4, 0), (4, 1), (4, 2), (4, 3)],
        )
        self.assertEqual(
            [
                (item.seed, item.rotation, item.focal_seat)
                for item in arm.offense_records
            ],
            [
                (seed, rotation, Seat(rotation))
                for seed in (9, 4)
                for rotation in range(4)
            ],
        )
        self.assertEqual(progress[-1], (8, 8))
        for index, (_, policies) in enumerate(recorder.calls):
            focal_seat = Seat(index % 4)
            for seat, policy in policies.items():
                expected = (
                    MinimalPolicy if seat == focal_seat else PassiveTsumogiriPolicy
                )
                self.assertIsInstance(policy, expected)
            # baseline 3 seatを含め、同じgame内でinstanceを共有しない。
            self.assertEqual(len({id(policy) for policy in policies.values()}), 4)

    def test_game_failure_fails_the_whole_arm(self) -> None:
        def failing(policies, *, seed, max_steps):
            if seed == 2:
                raise RuntimeError("boom")
            return _fake_result(seed), derive_kyoku_offense_facts(_DRAW_EVENTS)

        with mock.patch.object(execution, "_run_benchmark_game", failing):
            with self.assertRaises(SingleRoundEvaluationError) as raised:
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1, 2)), max_workers=1
                )
        self.assertEqual((raised.exception.seed, raised.exception.rotation), (2, 0))

    def test_facts_inconsistent_with_round_stats_fail_closed(self) -> None:
        exhaustive_events = [
            {"type": "start_kyoku", "oya": 0},
            {"type": "ryukyoku", "reason": "exhaustive_draw", "deltas": [0] * 4},
        ]

        def inconsistent(policies, *, seed, max_steps):
            return _fake_result(seed), derive_kyoku_offense_facts(exhaustive_events)

        with mock.patch.object(execution, "_run_benchmark_game", inconsistent):
            with self.assertRaises(SingleRoundEvaluationError):
                execution.run_benchmark_arm(
                    execution.benchmark_plan(_FOCAL, (1,)), max_workers=1
                )


class BenchmarkWorkerTest(unittest.TestCase):
    def test_worker_converts_failures_to_outcome_text(self) -> None:
        plan = execution.benchmark_plan(_FOCAL, (1,))
        job = execution.GameJob(
            seed=1,
            rotation=0,
            assignment=execution._seat_assignment(plan, 0),
            game_mode="4p-red-single",
            max_steps=MAX_STEPS,
        )
        with mock.patch.object(
            execution, "_run_benchmark_game", side_effect=RuntimeError("boom")
        ):
            outcome = execution._run_benchmark_game_job(job)
        self.assertIsNone(outcome.result)
        self.assertIsNone(outcome.facts)
        self.assertIn("boom", outcome.error_text)

        with mock.patch.object(execution, "_run_benchmark_game", _Recorder()):
            outcome = execution._run_benchmark_game_job(job)
        self.assertIsNone(outcome.error_text)
        self.assertIsNotNone(outcome.facts)


if __name__ == "__main__":
    unittest.main()
