import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.model import PolicySpec
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.mortal_single_round_evaluation import (
    MortalSingleRoundEvaluationError,
    MortalSingleRoundEvaluationPlan,
    run_mortal_single_round_evaluation,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

_MODULE = "lisjong_arena.mortal_single_round_evaluation"


class _Policy:
    pass


class MortalSingleRoundEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        model_path = Path(directory.name) / "mortal.pth"
        model_path.write_bytes(b"model")
        self.config = MortalDockerConfig(
            image="mortal:test",
            implementation_revision="revision",
            model_path=model_path,
        )
        self.created_policies: list[_Policy] = []

        def create_policy() -> _Policy:
            policy = _Policy()
            self.created_policies.append(policy)
            return policy

        self.baseline = PolicySpec(identity="two-step", factory=create_policy)

    def plan(
        self, seeds: tuple[int, ...] = (11, 22)
    ) -> MortalSingleRoundEvaluationPlan:
        return MortalSingleRoundEvaluationPlan(
            baseline=self.baseline,
            seeds=seeds,
            mortal_config=self.config,
            max_steps=123,
        )

    @staticmethod
    def game_result(seed: int, mortal_seat: Seat) -> LocalGameResult:
        scores = tuple(40000 if seat == mortal_seat else 20000 for seat in range(4))
        return LocalGameResult(
            seed=seed,
            game_mode="4p-red-single",
            scores=scores,
            ranks=(1, 2, 3, 4),
            steps=1,
            decisions=1,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )

    def test_runs_four_rotations_per_seed_with_fresh_three_policy_instances(
        self,
    ) -> None:
        calls: list[tuple[dict[Seat, _Policy], Seat, int, int]] = []

        def run_game(
            policies,
            *,
            mortal_seat,
            mortal_config,
            seed,
            max_steps,
        ):
            self.assertIs(mortal_config, self.config)
            calls.append((dict(policies), mortal_seat, seed, max_steps))
            return self.game_result(seed, mortal_seat)

        progress: list[tuple[int, int]] = []
        with mock.patch(f"{_MODULE}._run_mortal_single_game", side_effect=run_game):
            result = run_mortal_single_round_evaluation(
                self.plan(),
                progress_callback=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(
            [(seed, int(seat)) for _, seat, seed, _ in calls],
            [(seed, rotation) for seed in (11, 22) for rotation in range(4)],
        )
        for policies, mortal_seat, _, max_steps in calls:
            self.assertEqual(set(policies), set(Seat) - {mortal_seat})
            self.assertEqual(len({id(policy) for policy in policies.values()}), 3)
            self.assertEqual(max_steps, 123)
        self.assertEqual(len(self.created_policies), 24)
        self.assertEqual(len({id(policy) for policy in self.created_policies}), 24)
        self.assertEqual(
            [
                (item.seed, item.rotation, item.candidate_seat)
                for item in result.game_results
            ],
            [
                (seed, rotation, Seat(rotation))
                for seed in (11, 22)
                for rotation in range(4)
            ],
        )
        self.assertEqual(result.candidate_metrics.candidate_identity, "mortal")
        self.assertEqual(result.candidate_metrics.game_count, 8)
        self.assertEqual(result.candidate_metrics.mean_candidate_score, 40000.0)
        self.assertEqual(progress, [(index, 8) for index in range(1, 9)])

    def test_one_game_failure_reports_seed_rotation_and_returns_no_partial_result(
        self,
    ) -> None:
        calls = 0
        progress: list[tuple[int, int]] = []

        def run_game(policies, *, mortal_seat, mortal_config, seed, max_steps):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("game failed")
            return self.game_result(seed, mortal_seat)

        with (
            mock.patch(f"{_MODULE}._run_mortal_single_game", side_effect=run_game),
            self.assertRaises(MortalSingleRoundEvaluationError) as raised,
        ):
            run_mortal_single_round_evaluation(
                self.plan((9,)),
                progress_callback=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual((raised.exception.seed, raised.exception.rotation), (9, 2))
        self.assertEqual(progress, [(1, 4), (2, 4)])
        self.assertEqual(calls, 3)

    def test_mismatched_runner_result_fails_closed(self) -> None:
        with (
            mock.patch(
                f"{_MODULE}._run_mortal_single_game",
                return_value=self.game_result(999, Seat.SEAT_0),
            ),
            self.assertRaises(MortalSingleRoundEvaluationError) as raised,
        ):
            run_mortal_single_round_evaluation(self.plan((9,)))

        self.assertEqual((raised.exception.seed, raised.exception.rotation), (9, 0))

    def test_rejects_non_two_step_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "two-step"):
            MortalSingleRoundEvaluationPlan(
                baseline=PolicySpec(identity="finite-horizon", factory=_Policy),
                seeds=(0,),
                mortal_config=self.config,
            )


if __name__ == "__main__":
    unittest.main()
