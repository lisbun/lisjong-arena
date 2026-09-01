import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import (
    DecisionTrace,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.model import PolicySpec
from lisjong_arena.mortal_decision_comparison import (
    MortalDecisionComparisonRecord,
    NormalizedRiichiEnvAction,
    RiichiEnvActionKind,
)
from lisjong_arena.mortal_decision_evaluation import (
    MortalDecisionEvaluationError,
    MortalDecisionEvaluationPlan,
    run_mortal_decision_evaluation,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

_MODULE = "lisjong_arena.mortal_decision_evaluation"
_TILE = Tile(TileType(TileCategory.MANZU, 1))


class _Policy:
    pass


def _policy_input(seat: Seat) -> PolicyInput:
    player = PlayerPublicState(
        score=25000,
        discards=(),
        melds=(),
        riichi=RiichiState.NONE,
    )
    return PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(_TILE,),
            live_wall_tiles_remaining=70,
        ),
        players=(player, player, player, player),
        own_hand=OwnHandState(concealed_tiles=(_TILE,), drawn_tile=None),
    )


def _comparison(seed: int, rotation: int) -> MortalDecisionComparisonRecord:
    seat = Seat(rotation)
    selected = PassAction(actor=seat)
    normalized = NormalizedRiichiEnvAction(
        kind=RiichiEnvActionKind.PASS,
        actor=seat,
        tile=None,
        consume_tiles=(),
        tsumogiri=None,
    )
    return MortalDecisionComparisonRecord(
        seed=seed,
        rotation=rotation,
        mortal_seat=seat,
        decision_ordinal=0,
        shadow_policy_identity="combined",
        policy_input=_policy_input(seat),
        decision_trace=DecisionTrace(
            legal_actions=(selected,), selected_action=selected
        ),
        driver_mortal_action=normalized,
        shadow_policy_action=normalized,
        agreement=True,
    )


def _local_result(seed: int, mortal_seat: Seat) -> LocalGameResult:
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


class MortalDecisionEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        model = Path(directory.name) / "mortal.pth"
        model.write_bytes(b"model")
        self.config = MortalDockerConfig(
            image="mortal:test",
            implementation_revision="revision",
            model_path=model,
        )
        self.created = []

        def factory():
            policy = _Policy()
            self.created.append(policy)
            return policy

        self.plan = MortalDecisionEvaluationPlan(
            policy=PolicySpec(identity="combined", factory=factory),
            seeds=(11, 22),
            mortal_config=self.config,
        )

    def test_runs_canonical_rotations_with_independent_shadow_instances(self) -> None:
        calls = []

        def run_game(
            policies,
            shadow_policy,
            *,
            shadow_policy_identity,
            mortal_seat,
            mortal_config,
            seed,
            max_steps,
        ):
            calls.append((dict(policies), shadow_policy, mortal_seat, seed))
            self.assertEqual(shadow_policy_identity, "combined")
            self.assertIs(mortal_config, self.config)
            return _local_result(seed, mortal_seat), (
                _comparison(seed, int(mortal_seat)),
            )

        progress = []
        with mock.patch(f"{_MODULE}._run_mortal_decision_game", side_effect=run_game):
            result = run_mortal_decision_evaluation(
                self.plan,
                progress_callback=lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(
            [(seed, int(seat)) for _, _, seat, seed in calls],
            [(seed, rotation) for seed in (11, 22) for rotation in range(4)],
        )
        for policies, shadow, mortal_seat, _ in calls:
            self.assertEqual(set(policies), set(Seat) - {mortal_seat})
            self.assertNotIn(id(shadow), {id(policy) for policy in policies.values()})
        self.assertEqual(len(self.created), 32)
        self.assertEqual(len({id(policy) for policy in self.created}), 32)
        self.assertEqual(result.summary.total_paired_decisions, 8)
        self.assertEqual(result.summary.agreements, 8)
        self.assertEqual(progress, [(index, 8) for index in range(1, 9)])

    def test_factory_reusing_instance_fails_before_game_execution(self) -> None:
        shared = _Policy()
        plan = MortalDecisionEvaluationPlan(
            policy=PolicySpec(identity="combined", factory=lambda: shared),
            seeds=(0,),
            mortal_config=self.config,
        )
        with (
            mock.patch(f"{_MODULE}._run_mortal_decision_game") as runner,
            self.assertRaisesRegex(
                MortalDecisionEvaluationError, "independent actual-seat and shadow"
            ),
        ):
            run_mortal_decision_evaluation(plan)

        runner.assert_not_called()

    def test_failure_returns_no_partial_result_and_reports_rotation(self) -> None:
        calls = 0

        def run_game(*args, mortal_seat, seed, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("failed")
            return _local_result(seed, mortal_seat), (
                _comparison(seed, int(mortal_seat)),
            )

        with (
            mock.patch(f"{_MODULE}._run_mortal_decision_game", side_effect=run_game),
            self.assertRaises(MortalDecisionEvaluationError) as raised,
        ):
            run_mortal_decision_evaluation(self.plan)

        self.assertEqual((raised.exception.seed, raised.exception.rotation), (11, 1))
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
