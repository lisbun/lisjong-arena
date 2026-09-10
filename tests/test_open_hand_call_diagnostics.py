"""Issue #196 purpose-specific decision diagnostics unit tests。

Locked measurement seedsは使用せず、real RiichiEnvも起動しない。
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import (
    ChiAction,
    DecisionContext,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    PonAction,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.open_hand_call_diagnostics import (
    OpenHandGameDiagnostics,
    _DecisionRecorder,
    _DiagnosticGameJobOutcome,
    _ShadowComparedCandidatePolicy,
    aggregate_open_hand_diagnostics,
    run_open_hand_diagnostic_evaluation,
    run_open_hand_diagnostic_evaluation_parallel,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.single_round_evaluation import (
    run_single_round_evaluation,
    summarize_single_round_strength,
)

_TILE_1M = Tile(TileType(TileCategory.MANZU, 1))


def _tile(rank: int) -> Tile:
    return Tile(TileType(TileCategory.MANZU, rank))


def _policy_input(seat: Seat = Seat.SEAT_0) -> PolicyInput:
    player = PlayerPublicState(
        score=25_000,
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
            dora_indicators=(_TILE_1M,),
            live_wall_tiles_remaining=70,
        ),
        players=(player, player, player, player),
        own_hand=OwnHandState(concealed_tiles=(_TILE_1M,), drawn_tile=None),
    )


def _pass(seat: Seat = Seat.SEAT_0) -> PassAction:
    return PassAction(actor=seat)


def _chi(seat: Seat = Seat.SEAT_0) -> ChiAction:
    return ChiAction(
        actor=seat,
        target=Seat((int(seat) - 1) % 4),
        called_tile=_tile(3),
        consumed_tiles=(_tile(1), _tile(2)),
    )


def _pon(seat: Seat = Seat.SEAT_0) -> PonAction:
    return PonAction(
        actor=seat,
        target=Seat((int(seat) + 2) % 4),
        called_tile=_tile(5),
        consumed_tiles=(_tile(5), _tile(5)),
    )


class _FixedPolicy:
    def __init__(self, action) -> None:
        self.action = action
        self.contexts = []

    def choose_action(self, decision):
        self.contexts.append(decision)
        return self.action


def _candidate_factory() -> object:
    return object()


def _baseline_factory() -> object:
    return object()


def _plan(seeds=(101, 202)) -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=PolicySpec(
            identity="open-hand-yaku-aware-call", factory=_candidate_factory
        ),
        baseline=PolicySpec(identity="yakuhai-call", factory=_baseline_factory),
        seeds=seeds,
    )


def _local_result(seed: int) -> LocalGameResult:
    scores = (25_000, 25_000, 25_000, 25_000)
    return LocalGameResult(
        seed=seed,
        game_mode="4p-red-single",
        scores=scores,
        ranks=(1, 2, 3, 4),
        steps=1,
        decisions=4,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


class ShadowDecisionTest(unittest.TestCase):
    def _run(self, candidate_action, baseline_action, legal_actions):
        candidate = _FixedPolicy(candidate_action)
        baseline = _FixedPolicy(baseline_action)
        recorder = _DecisionRecorder()
        wrapper = _ShadowComparedCandidatePolicy(candidate, baseline, recorder)
        context = DecisionContext(
            input=_policy_input(), legal_actions=tuple(legal_actions)
        )

        selected = wrapper.choose_action(context)

        self.assertEqual(selected, candidate_action)
        if candidate_action != baseline_action:
            self.assertNotEqual(selected, baseline_action)
        self.assertEqual(candidate.contexts, [context])
        self.assertEqual(baseline.contexts, [context])
        self.assertIs(candidate.contexts[0], baseline.contexts[0])
        return recorder.snapshot(
            seed=1,
            rotation=0,
            candidate_seat=Seat.SEAT_0,
            score_delta=0,
        )

    def test_shadow_baseline_never_replaces_candidate_action(self) -> None:
        candidate = _chi()
        diagnostic = self._run(candidate, _pass(), (_pass(), candidate))
        self.assertEqual(diagnostic.baseline_pass_to_candidate_chi, 1)

    def test_pass_to_chi_and_pass_to_pon_are_counted_exactly(self) -> None:
        chi = self._run(_chi(), _pass(), (_pass(), _chi()))
        pon = self._run(_pon(), _pass(), (_pass(), _pon()))
        self.assertEqual(chi.candidate_only_chi_count, 1)
        self.assertEqual(chi.candidate_only_pon_count, 0)
        self.assertEqual(pon.candidate_only_chi_count, 0)
        self.assertEqual(pon.candidate_only_pon_count, 1)

    def test_same_action_is_not_a_divergence(self) -> None:
        selected = _pass()
        diagnostic = self._run(selected, selected, (selected,))
        self.assertEqual(diagnostic.same_action_decisions, 1)
        self.assertEqual(diagnostic.divergent_action_decisions, 0)

    def test_shared_prefix_opportunity_is_separate_from_accepted_call(self) -> None:
        selected = _pass()
        opportunity_only = self._run(selected, selected, (selected, _chi()))
        accepted = self._run(_chi(), selected, (selected, _chi()))
        self.assertEqual(opportunity_only.shared_prefix_initial_call_opportunities, 1)
        self.assertEqual(opportunity_only.candidate_only_chi_count, 0)
        self.assertEqual(opportunity_only.divergent_action_decisions, 0)
        self.assertEqual(accepted.shared_prefix_initial_call_opportunities, 1)
        self.assertEqual(accepted.candidate_only_chi_count, 1)
        self.assertEqual(accepted.divergent_action_decisions, 1)

    def test_shared_prefix_opportunity_stops_after_first_divergence(self) -> None:
        passed = _pass()
        chi = _chi()
        pon = _pon()
        context = DecisionContext(
            input=_policy_input(), legal_actions=(passed, chi, pon)
        )
        recorder = _DecisionRecorder()

        recorder.record(context, passed, passed)
        recorder.record(context, chi, passed)
        recorder.record(context, pon, passed)
        diagnostic = recorder.snapshot(
            seed=1,
            rotation=0,
            candidate_seat=Seat.SEAT_0,
            score_delta=0,
        )

        self.assertEqual(diagnostic.total_candidate_seat_decisions, 3)
        self.assertEqual(diagnostic.shared_prefix_initial_call_opportunities, 2)
        self.assertEqual(diagnostic.divergent_action_decisions, 2)
        self.assertEqual(diagnostic.candidate_only_chi_count, 1)
        self.assertEqual(diagnostic.candidate_only_pon_count, 1)


def _game(
    seed: int,
    rotation: int,
    divergences: int,
    score_delta: int,
) -> OpenHandGameDiagnostics:
    return OpenHandGameDiagnostics(
        seed=seed,
        rotation=rotation,
        candidate_seat=Seat(rotation),
        total_candidate_seat_decisions=10,
        same_action_decisions=10 - divergences,
        divergent_action_decisions=divergences,
        shared_prefix_initial_call_opportunities=divergences,
        baseline_pass_to_candidate_chi=divergences,
        baseline_pass_to_candidate_pon=0,
        scaled_candidate_score_delta=score_delta,
    )


class AggregationTest(unittest.TestCase):
    def test_game_seed_and_divergent_block_outcome_coverage_is_exact(self) -> None:
        games = (
            _game(1, 0, 2, 10),
            _game(1, 1, 1, -5),
            _game(2, 0, 3, 0),
            _game(2, 1, 0, 0),
            _game(3, 0, 1, -1),
            _game(4, 0, 0, 2),
        )

        summary = aggregate_open_hand_diagnostics(games)

        self.assertEqual(summary.divergent_action_decisions, 7)
        self.assertEqual(summary.divergent_game_count, 4)
        self.assertEqual(summary.divergent_seed_block_count, 3)
        self.assertEqual(summary.nonzero_score_delta_seed_block_count, 3)
        self.assertEqual(
            (
                summary.divergent_positive_score_delta_seed_blocks,
                summary.divergent_zero_score_delta_seed_blocks,
                summary.divergent_negative_score_delta_seed_blocks,
            ),
            (1, 1, 1),
        )
        self.assertEqual(summary.mean_divergences_per_divergent_game, 7 / 4)
        self.assertEqual(summary.divergent_decisions_by_candidate_seat, (6, 1, 0, 0))


class SemanticNonInterferenceTest(unittest.TestCase):
    def test_diagnostics_enabled_and_disabled_have_same_results_and_summary(self):
        plan = _plan()

        def ordinary_runner(policies, *, seed, max_steps):
            return _local_result(seed)

        def diagnostic_runner(
            policies,
            shadow_baseline,
            *,
            seed,
            rotation,
            candidate_seat,
            max_steps,
        ):
            return _local_result(seed), _DecisionRecorder()

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game",
            ordinary_runner,
        ):
            ordinary = run_single_round_evaluation(plan)
        with mock.patch(
            "lisjong_arena.open_hand_call_diagnostics._run_diagnostic_single_game",
            diagnostic_runner,
        ):
            diagnostic = run_open_hand_diagnostic_evaluation(plan)

        self.assertEqual(
            ordinary.game_results, diagnostic.evaluation_result.game_results
        )
        self.assertEqual(
            ordinary.candidate_metrics,
            diagnostic.evaluation_result.candidate_metrics,
        )
        self.assertEqual(
            summarize_single_round_strength(
                ordinary.candidate_metrics, ordinary.game_results
            ),
            summarize_single_round_strength(
                diagnostic.evaluation_result.candidate_metrics,
                diagnostic.evaluation_result.game_results,
            ),
        )


class ParallelCanonicalizationTest(unittest.TestCase):
    def test_worker_completion_order_does_not_change_serialization_order(self):
        plan = _plan((30, 10))
        outcomes = {}
        for seed in reversed(plan.seeds):
            for rotation in reversed(range(4)):
                outcomes[(seed, rotation)] = _DiagnosticGameJobOutcome(
                    seed=seed,
                    rotation=rotation,
                    result=_local_result(seed),
                    error_text=None,
                    diagnostic=_game(seed, rotation, rotation % 2, 0),
                )

        with mock.patch(
            "lisjong_arena.open_hand_call_diagnostics.run_game_jobs",
            return_value=outcomes,
        ):
            result = run_open_hand_diagnostic_evaluation_parallel(plan, max_workers=4)

        expected = [(seed, rotation) for seed in plan.seeds for rotation in range(4)]
        self.assertEqual(
            [(game.seed, game.rotation) for game in result.game_diagnostics],
            expected,
        )
        self.assertEqual(
            [
                (game.seed, game.rotation)
                for game in result.evaluation_result.game_results
            ],
            expected,
        )


class BoundaryTest(unittest.TestCase):
    def test_shadow_boundary_accepts_only_the_public_decision_context(self) -> None:
        parameters = inspect.signature(
            _ShadowComparedCandidatePolicy.choose_action
        ).parameters

        self.assertEqual(tuple(parameters), ("self", "decision"))
        self.assertEqual(parameters["decision"].annotation, "DecisionContext")

    def test_module_does_not_import_private_lisjong_yaku_route_helpers(self) -> None:
        import lisjong_arena.open_hand_call_diagnostics as module

        source = inspect.getsource(module)
        self.assertNotIn("_yaku_route_value_for_tiles", source)
        self.assertNotIn("_route_compatible_post_call_shanten", source)


if __name__ == "__main__":
    unittest.main()
