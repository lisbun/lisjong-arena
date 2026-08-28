"""lisjong-project #22 Phase 0.5 decision-linked comparison tests。"""

import pickle
import unittest
from dataclasses import dataclass

from lisjong.belief import (
    estimate_conditional_uniform_hand_belief,
    wind_for_seat,
    wind_index,
)
from lisjong.policies.experimental_hand_belief_sensitivity import (
    OpponentExpectedCounts,
    evaluate_expected_count_sensitive_discard,
    opponent_expected_counts_from_belief,
)
from lisjong.policy_contract import DiscardAction, Wind
from lisjong_engine.action_projection import project_legal_actions
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.oracle_sensitivity_pilot import _opponent_slot_counts_by_wind
from lisjong_arena.phase05_belief_slice.decision_linked import (
    Phase05DecisionLinkedResult,
    _DecisionLinkedRecorder,
    _oracle_opponent_counts,
    _summed_opponent_counts,
    run_phase05_decision_linked,
)
from lisjong_arena.phase05_belief_slice.estimator import (
    BucketedExpectedCountEstimator,
)

_SEED = 20260827
_CONSERVATION_FAILING_COUNT = 4.0
"""3他家合計で1牌種12.0となり、必ずviewer-visibleな未見枚数を超える値。"""


@dataclass(frozen=True, slots=True)
class _ConsumerActivations:
    """同一positionで3 beliefへ与えたときのconsumer activation。"""

    baseline: bool
    oracle: bool
    zeroed: bool


def _consumer_activations(observation, options) -> _ConsumerActivations:
    """同じhard filterがbeliefに依存しないことを確認するためのprobe。"""
    match_state, _, _ = _dealer_turn_decision()
    engine_decision = build_decision(observation, options)
    policy_input = engine_decision.context.input
    discard_actions = tuple(
        action
        for action in engine_decision.context.legal_actions
        if isinstance(action, DiscardAction)
    )
    baseline_belief = estimate_conditional_uniform_hand_belief(
        policy_input,
        _opponent_slot_counts_by_wind(policy_input),
    )
    baseline_counts = opponent_expected_counts_from_belief(
        policy_input,
        baseline_belief,
    )
    oracle_counts = _oracle_opponent_counts(
        match_state,
        policy_input,
        baseline_belief,
    )
    if oracle_counts is None:
        raise unittest.SkipTest("fixture position must be oracle buildable")

    def active(counts) -> bool:
        return evaluate_expected_count_sensitive_discard(
            policy_input,
            discard_actions,
            counts,
        ).consumer_active

    return _ConsumerActivations(
        baseline=active(baseline_counts),
        oracle=active(oracle_counts),
        zeroed=active(OpponentExpectedCounts(counts=(0.0,) * 34)),
    )


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

    def test_recorder_uses_only_turn_anchor_positions(self) -> None:
        match_state, observation, options = _dealer_turn_decision()
        recorder = _DecisionLinkedRecorder(match_state, _flat_estimator(0.0))

        recorder.observe(observation, options)

        self.assertEqual(recorder.turn_decisions, 1)
        self.assertEqual(recorder.eligible_positions, 1)
        self.assertEqual(recorder.oracle_buildable_positions, 1)


class DenominatorAccountingTest(unittest.TestCase):
    """consumer activationがbelief非依存であることを母数へ反映する。

    `_CONSERVATION_FAILING_COUNT`は3他家合計で1牌種あたり12.0となり、
    viewer-visibleな未見枚数（最大4）を必ず超えるため、mockではなく実際の
    Track B consumerが`HandBeliefSensitivityError`をfail closedで送出する。
    """

    def setUp(self) -> None:
        self.match_state, self.observation, self.options = _dealer_turn_decision()
        activation = _consumer_activations(self.observation, self.options)
        if not activation.baseline:
            raise unittest.SkipTest(
                "fixture position must activate the Track B consumer"
            )

    def _observe(self, expected_count: float) -> _DecisionLinkedRecorder:
        recorder = _DecisionLinkedRecorder(
            self.match_state,
            _flat_estimator(expected_count),
        )
        recorder.observe(self.observation, self.options)
        return recorder

    def test_learned_conservation_failure_keeps_the_position_consumer_active(
        self,
    ) -> None:
        recorder = self._observe(_CONSERVATION_FAILING_COUNT)

        self.assertEqual(recorder.consumer_active_positions, 1)
        self.assertEqual(recorder.learned_conservation_exclusions, 1)

    def test_baseline_oracle_comparison_survives_learned_conservation_failure(
        self,
    ) -> None:
        evaluable = self._observe(0.0)
        excluded = self._observe(_CONSERVATION_FAILING_COUNT)

        self.assertEqual(evaluable.learned_conservation_exclusions, 0)
        self.assertEqual(excluded.learned_conservation_exclusions, 1)
        self.assertEqual(
            excluded.consumer_active_positions,
            evaluable.consumer_active_positions,
        )
        self.assertEqual(
            excluded.baseline_oracle_agreements,
            evaluable.baseline_oracle_agreements,
        )

    def test_learned_comparisons_are_dropped_when_learned_is_not_evaluable(
        self,
    ) -> None:
        recorder = self._observe(_CONSERVATION_FAILING_COUNT)

        self.assertEqual(recorder.learned_oracle_agreements, 0)
        self.assertEqual(recorder.baseline_learned_divergences, 0)
        self.assertEqual(recorder.proxy_learned_better, 0)
        self.assertEqual(recorder.proxy_same, 0)
        self.assertEqual(recorder.proxy_learned_worse, 0)

    def test_track_b_consumer_activation_is_belief_independent(self) -> None:
        activation = _consumer_activations(self.observation, self.options)

        self.assertEqual(activation.baseline, activation.oracle)
        self.assertEqual(activation.baseline, activation.zeroed)


def _result(**overrides) -> Phase05DecisionLinkedResult:
    fields = {
        "seeds": (150,),
        "turn_decisions": 100,
        "eligible_positions": 80,
        "oracle_buildable_positions": 80,
        "consumer_active_positions": 40,
        "learned_conservation_exclusions": 0,
        "baseline_learned_divergences": 10,
        "learned_oracle_agreements": 30,
        "baseline_oracle_agreements": 34,
        "proxy_learned_better": 4,
        "proxy_same": 3,
        "proxy_learned_worse": 3,
        "proxy_delta_sum": 2,
        "backoff_level_counts": ((0, 100),),
        "wall_clock_seconds": 1.0,
    }
    fields.update(overrides)
    return Phase05DecisionLinkedResult(**fields)


class ResultAggregationTest(unittest.TestCase):
    def test_learned_rates_use_learned_evaluable_denominator(self) -> None:
        result = _result(learned_conservation_exclusions=2)

        self.assertEqual(result.learned_evaluable_positions, 38)
        self.assertEqual(result.baseline_learned_divergence_rate, 10 / 38)
        self.assertEqual(result.learned_oracle_agreement_rate, 30 / 38)
        self.assertEqual(result.proxy_compared_positions, 10)
        self.assertEqual(result.proxy_mean_delta, 0.2)

    def test_baseline_oracle_rate_is_independent_of_learned_validity(self) -> None:
        without_exclusions = _result()
        with_exclusions = _result(learned_conservation_exclusions=2)

        self.assertEqual(without_exclusions.baseline_oracle_agreement_rate, 0.85)
        self.assertEqual(
            with_exclusions.baseline_oracle_agreement_rate,
            without_exclusions.baseline_oracle_agreement_rate,
        )

    def test_zero_exclusions_keeps_every_rate_on_the_same_denominator(self) -> None:
        """exclusion 0件なら従来のrate semanticsと一致する。

        lisjong-project #22のauthoritative runは
        `learned_conservation_exclusions == 0` だったため、この等式が
        投稿済みdecision-linked数値の不変性を保証する。
        """
        result = _result()

        self.assertEqual(
            result.learned_evaluable_positions,
            result.consumer_active_positions,
        )
        self.assertEqual(result.baseline_learned_divergence_rate, 10 / 40)
        self.assertEqual(result.learned_oracle_agreement_rate, 30 / 40)
        self.assertEqual(result.baseline_oracle_agreement_rate, 34 / 40)

    def test_counts_outside_their_denominator_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            _result(learned_conservation_exclusions=41)
        with self.assertRaises(ValueError):
            _result(baseline_oracle_agreements=41)
        with self.assertRaises(ValueError):
            _result(
                learned_conservation_exclusions=35,
                learned_oracle_agreements=30,
            )
        with self.assertRaises(ValueError):
            _result(consumer_active_positions=81)

    def test_proxy_outcomes_must_cover_the_divergent_positions(self) -> None:
        with self.assertRaises(ValueError):
            _result(proxy_same=2)

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
