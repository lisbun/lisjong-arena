"""lisjong-project #22 Phase 0.5 decision-linked comparison tests。"""

import pickle
import unittest
from unittest.mock import patch

from lisjong.belief import wind_for_seat, wind_index
from lisjong.policies.experimental_hand_belief_sensitivity import (
    HandBeliefSensitivityError,
    OpponentExpectedCounts,
)
from lisjong.policy_contract import Wind
from lisjong_engine.action_projection import project_legal_actions
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

import lisjong_arena.phase05_belief_slice.decision_linked as decision_linked
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase05_belief_slice.decision_linked import (
    Phase05DecisionLinkedResult,
    _DecisionLinkedRecorder,
    _summed_opponent_counts,
    run_phase05_decision_linked,
)
from lisjong_arena.phase05_belief_slice.estimator import (
    BucketedExpectedCountEstimator,
)

_SEED = 20260827


def _dealer_turn_decision():
    match_state = MatchState(seed=_SEED, rules=RuleSet.default())
    match_state.start_round()
    round_state = match_state.active_round
    if round_state is None:
        raise AssertionError("start_round must produce an active round")
    round_state.draw(EngineSeat.EAST)
    observation = build_seat_observation(match_state, EngineSeat.EAST)
    projection = project_legal_actions(
        round_state.legal_actions(EngineSeat.EAST),
        round_state,
    )
    return match_state, observation, projection.options


def _flat_estimator(value: float) -> BucketedExpectedCountEstimator:
    """全cellが同じexpected countを返す最小のestimator代用。"""

    class _Flat(BucketedExpectedCountEstimator):
        def __init__(self) -> None:
            super().__init__(tuple({} for _ in range(6)))

        def predict(self, feature):
            from lisjong_arena.phase05_belief_slice.estimator import Phase05Prediction

            return Phase05Prediction(expected_count=value, backoff_level=0)

    return _Flat()


class SummedOpponentCountsTest(unittest.TestCase):
    def test_rows_are_summed_into_the_canonical_viewer_table(self) -> None:
        _, observation, _ = _dealer_turn_decision()
        policy_input = build_policy_input(observation)
        rows = (
            (1.0,) + (0.0,) * 33,
            (0.5,) + (0.0,) * 33,
            (0.25,) + (0.0,) * 33,
        )

        counts = _summed_opponent_counts(
            policy_input,
            (Wind.SOUTH, Wind.WEST, Wind.NORTH),
            rows,
        )

        self.assertIsInstance(counts, OpponentExpectedCounts)
        self.assertEqual(counts.counts[0], 1.75)
        self.assertEqual(counts.counts[1], 0.0)

    def test_viewer_wind_row_is_rejected(self) -> None:
        _, observation, _ = _dealer_turn_decision()
        policy_input = build_policy_input(observation)
        viewer_wind = wind_for_seat(
            policy_input.self_seat,
            policy_input.round.dealer_seat,
        )
        self.assertEqual(wind_index(viewer_wind), 0)

        with self.assertRaises(RuntimeError):
            _summed_opponent_counts(
                policy_input,
                (viewer_wind, Wind.WEST, Wind.NORTH),
                ((0.0,) * 34,) * 3,
            )


class RecorderBoundaryTest(unittest.TestCase):
    def test_non_turn_decisions_are_not_counted(self) -> None:
        match_state, observation, options = _dealer_turn_decision()
        recorder = _DecisionLinkedRecorder(match_state, _flat_estimator(0.0))
        non_turn = object.__new__(type(observation))
        for name in type(observation).__dataclass_fields__:
            value = getattr(observation, name)
            if name == "decision_kind":
                value = ObservationDecisionKind.DISCARD_REACTION
            object.__setattr__(non_turn, name, value)

        recorder.observe(non_turn, options)

        self.assertEqual(recorder.turn_decisions, 0)
        self.assertEqual(recorder.eligible_positions, 0)

    def test_learned_conservation_failure_is_excluded_not_raised(self) -> None:
        match_state, observation, options = _dealer_turn_decision()
        recorder = _DecisionLinkedRecorder(match_state, _flat_estimator(0.0))

        with patch.object(
            decision_linked,
            "evaluate_expected_count_sensitive_discard",
            side_effect=_conservation_failure_on_second_call(),
        ):
            recorder.observe(observation, options)

        self.assertEqual(recorder.learned_conservation_exclusions, 1)
        self.assertEqual(recorder.consumer_active_positions, 0)

    def test_recorder_uses_only_turn_anchor_positions(self) -> None:
        match_state, observation, options = _dealer_turn_decision()
        recorder = _DecisionLinkedRecorder(match_state, _flat_estimator(0.0))

        recorder.observe(observation, options)

        self.assertEqual(recorder.turn_decisions, 1)
        self.assertEqual(recorder.eligible_positions, 1)
        self.assertEqual(recorder.oracle_buildable_positions, 1)


def _conservation_failure_on_second_call():
    from lisjong.policies.experimental_hand_belief_sensitivity import (
        evaluate_expected_count_sensitive_discard,
    )

    state = {"calls": 0}

    def side_effect(policy_input, discard_actions, counts):
        state["calls"] += 1
        if state["calls"] == 1:
            decision = evaluate_expected_count_sensitive_discard(
                policy_input,
                discard_actions,
                counts,
            )
            if not decision.consumer_active:
                raise unittest.SkipTest(
                    "fixture position must activate the Track B consumer"
                )
            return decision
        raise HandBeliefSensitivityError("simulated conservation failure")

    return side_effect


class ResultAggregationTest(unittest.TestCase):
    def test_rates_use_consumer_active_positions_as_denominator(self) -> None:
        result = Phase05DecisionLinkedResult(
            seeds=(150,),
            turn_decisions=100,
            eligible_positions=80,
            oracle_buildable_positions=80,
            consumer_active_positions=40,
            learned_conservation_exclusions=2,
            baseline_learned_divergences=10,
            learned_oracle_agreements=30,
            baseline_oracle_agreements=34,
            proxy_learned_better=4,
            proxy_same=3,
            proxy_learned_worse=3,
            proxy_delta_sum=2,
            backoff_level_counts=((0, 100),),
            wall_clock_seconds=1.0,
        )

        self.assertEqual(result.baseline_learned_divergence_rate, 0.25)
        self.assertEqual(result.learned_oracle_agreement_rate, 0.75)
        self.assertEqual(result.baseline_oracle_agreement_rate, 0.85)
        self.assertEqual(result.proxy_compared_positions, 10)
        self.assertEqual(result.proxy_mean_delta, 0.2)

    def test_empty_denominators_do_not_divide_by_zero(self) -> None:
        result = Phase05DecisionLinkedResult(
            seeds=(150,),
            turn_decisions=0,
            eligible_positions=0,
            oracle_buildable_positions=0,
            consumer_active_positions=0,
            learned_conservation_exclusions=0,
            baseline_learned_divergences=0,
            learned_oracle_agreements=0,
            baseline_oracle_agreements=0,
            proxy_learned_better=0,
            proxy_same=0,
            proxy_learned_worse=0,
            proxy_delta_sum=0,
            backoff_level_counts=(),
            wall_clock_seconds=0.0,
        )

        self.assertEqual(result.baseline_learned_divergence_rate, 0.0)
        self.assertIsNone(result.proxy_mean_delta)


class RunnerContractTest(unittest.TestCase):
    def test_runner_validates_its_inputs(self) -> None:
        estimator = _flat_estimator(0.0)

        with self.assertRaises(ValueError):
            run_phase05_decision_linked((), estimator)
        with self.assertRaises(TypeError):
            run_phase05_decision_linked(("150",), estimator)
        with self.assertRaises(TypeError):
            run_phase05_decision_linked((150,), object())

    def test_estimator_is_picklable_for_reuse(self) -> None:
        estimator = BucketedExpectedCountEstimator(tuple({} for _ in range(6)))

        self.assertEqual(
            pickle.loads(pickle.dumps(estimator)).training_cell_counts,
            estimator.training_cell_counts,
        )


if __name__ == "__main__":
    unittest.main()
