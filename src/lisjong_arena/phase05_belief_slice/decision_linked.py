"""Phase 0.5のtest partition decision-linked comparison。

training dataset inclusionとは別subsetとして、Track Bと同じhard filter
（winning action不可、riichi action不可、discard candidate必須）を`TURN`
anchorへ適用する。同じpositionへ、

```text
conditional-uniform baseline
learned Phase 0.5 estimator
omniscient expected-count oracle
```

の3 beliefを与え、Track B consumerと同じranking seamで選択actionを比較する。

learned estimatorはexpected countしか提供しないため、`red_five_probability`を
0で捏造したりomniscient red-five truthを混ぜたりせず、`lisjong`側の
expected-count-only seam（`evaluate_expected_count_sensitive_discard()`）を
使う。これはgame EVではなく、prediction improvement / action divergence /
structural proxyを区別したまま報告するためのdecision-linked測定である。
"""

import time
from collections import Counter
from dataclasses import dataclass

from lisjong.belief import (
    TILE_TYPE_COUNT,
    estimate_conditional_uniform_hand_belief,
    wind_for_seat,
    wind_index,
)
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies.experimental_hand_belief_sensitivity import (
    HandBeliefSensitivityError,
    OpponentExpectedCounts,
    evaluate_expected_count_sensitive_discard,
    opponent_expected_counts_from_belief,
)
from lisjong.policy_contract import (
    DiscardAction,
    PolicyInput,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong_engine.driver import ActionSelector, run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.oracle_sensitivity_main import _live_wall_structural_ukeire
from lisjong_arena.oracle_sensitivity_pilot import (
    _build_oracle_belief,
    _opponent_slot_counts_by_wind,
)
from lisjong_arena.phase05_belief_slice.estimator import (
    BucketedExpectedCountEstimator,
)
from lisjong_arena.phase05_belief_slice.feature import (
    OPPONENT_COUNT,
    encode_phase05_anchor_features,
)

_WINNING_ACTION_TYPES = (RonAction, TsumoAction)


@dataclass(frozen=True, slots=True)
class Phase05DecisionLinkedResult:
    """test partitionのdecision-linked comparison aggregate。"""

    seeds: tuple[int, ...]
    turn_decisions: int
    eligible_positions: int
    oracle_buildable_positions: int
    consumer_active_positions: int
    learned_conservation_exclusions: int
    baseline_learned_divergences: int
    learned_oracle_agreements: int
    baseline_oracle_agreements: int
    proxy_learned_better: int
    proxy_same: int
    proxy_learned_worse: int
    proxy_delta_sum: int
    backoff_level_counts: tuple[tuple[int, int], ...]
    wall_clock_seconds: float

    @property
    def baseline_learned_divergence_rate(self) -> float:
        if self.consumer_active_positions == 0:
            return 0.0
        return self.baseline_learned_divergences / self.consumer_active_positions

    @property
    def learned_oracle_agreement_rate(self) -> float:
        if self.consumer_active_positions == 0:
            return 0.0
        return self.learned_oracle_agreements / self.consumer_active_positions

    @property
    def baseline_oracle_agreement_rate(self) -> float:
        if self.consumer_active_positions == 0:
            return 0.0
        return self.baseline_oracle_agreements / self.consumer_active_positions

    @property
    def proxy_compared_positions(self) -> int:
        return self.proxy_learned_better + self.proxy_same + self.proxy_learned_worse

    @property
    def proxy_mean_delta(self) -> float | None:
        if self.proxy_compared_positions == 0:
            return None
        return self.proxy_delta_sum / self.proxy_compared_positions


def _summed_opponent_counts(
    policy_input: PolicyInput,
    opponent_winds: tuple,
    rows: tuple[tuple[float, ...], ...],
) -> OpponentExpectedCounts:
    """opponent row別predictionをviewer視点の合算tableへ縮約する。"""
    self_wind_number = wind_index(
        wind_for_seat(policy_input.self_seat, policy_input.round.dealer_seat)
    )
    totals = [0.0] * TILE_TYPE_COUNT
    for wind, row in zip(opponent_winds, rows, strict=True):
        if wind_index(wind) == self_wind_number:
            raise RuntimeError("opponent rows must not contain the viewer wind")
        for tile_index, value in enumerate(row):
            totals[tile_index] += value
    return OpponentExpectedCounts(counts=tuple(totals))


def _oracle_opponent_counts(
    match_state: MatchState,
    policy_input: PolicyInput,
    baseline_belief: object,
) -> OpponentExpectedCounts | None:
    oracle_belief = _build_oracle_belief(match_state, policy_input, baseline_belief)
    if oracle_belief is None:
        return None
    return opponent_expected_counts_from_belief(policy_input, oracle_belief)


class _DecisionLinkedRecorder:
    """online selectorから分離されたArena-side omniscient paired observer。"""

    def __init__(
        self,
        match_state: MatchState,
        estimator: BucketedExpectedCountEstimator,
    ) -> None:
        self._match_state = match_state
        self._estimator = estimator
        self.turn_decisions = 0
        self.eligible_positions = 0
        self.oracle_buildable_positions = 0
        self.consumer_active_positions = 0
        self.learned_conservation_exclusions = 0
        self.baseline_learned_divergences = 0
        self.learned_oracle_agreements = 0
        self.baseline_oracle_agreements = 0
        self.proxy_learned_better = 0
        self.proxy_same = 0
        self.proxy_learned_worse = 0
        self.proxy_delta_sum = 0
        self.backoff_level_counts: Counter[int] = Counter()

    def observe(self, observation: SeatObservation, options: object) -> None:
        if observation.decision_kind is not ObservationDecisionKind.TURN:
            return

        self.turn_decisions += 1
        engine_decision = build_decision(observation, options)
        legal_actions = engine_decision.context.legal_actions
        if any(isinstance(action, _WINNING_ACTION_TYPES) for action in legal_actions):
            return
        if any(isinstance(action, RiichiAction) for action in legal_actions):
            return
        discard_actions = tuple(
            action for action in legal_actions if isinstance(action, DiscardAction)
        )
        if not discard_actions:
            return

        self.eligible_positions += 1
        policy_input = engine_decision.context.input
        baseline_belief = estimate_conditional_uniform_hand_belief(
            policy_input,
            _opponent_slot_counts_by_wind(policy_input),
        )
        oracle_counts = _oracle_opponent_counts(
            self._match_state,
            policy_input,
            baseline_belief,
        )
        if oracle_counts is None:
            return
        self.oracle_buildable_positions += 1

        baseline_counts = opponent_expected_counts_from_belief(
            policy_input,
            baseline_belief,
        )
        baseline_decision = evaluate_expected_count_sensitive_discard(
            policy_input,
            discard_actions,
            baseline_counts,
        )
        if not baseline_decision.consumer_active:
            return

        features = encode_phase05_anchor_features(policy_input)
        learned_rows: list[tuple[float, ...]] = []
        for offset in range(OPPONENT_COUNT):
            values: list[float] = []
            for tile_index in range(TILE_TYPE_COUNT):
                prediction = self._estimator.predict(
                    features.feature(offset, tile_index)
                )
                self.backoff_level_counts[prediction.backoff_level] += 1
                values.append(prediction.expected_count)
            learned_rows.append(tuple(values))
        learned_counts = _summed_opponent_counts(
            policy_input,
            features.opponent_winds,
            tuple(learned_rows),
        )

        try:
            learned_decision = evaluate_expected_count_sensitive_discard(
                policy_input,
                discard_actions,
                learned_counts,
            )
        except HandBeliefSensitivityError:
            self.learned_conservation_exclusions += 1
            return

        oracle_decision = evaluate_expected_count_sensitive_discard(
            policy_input,
            discard_actions,
            oracle_counts,
        )
        if not (learned_decision.consumer_active and oracle_decision.consumer_active):
            raise RuntimeError(
                "consumer activation must depend only on non-belief hard filters"
            )

        self.consumer_active_positions += 1
        if learned_decision.action == oracle_decision.action:
            self.learned_oracle_agreements += 1
        if baseline_decision.action == oracle_decision.action:
            self.baseline_oracle_agreements += 1
        if baseline_decision.action == learned_decision.action:
            return

        self.baseline_learned_divergences += 1
        baseline_proxy = _live_wall_structural_ukeire(
            self._match_state,
            policy_input,
            baseline_decision.action,
        )
        learned_proxy = _live_wall_structural_ukeire(
            self._match_state,
            policy_input,
            learned_decision.action,
        )
        delta = learned_proxy - baseline_proxy
        self.proxy_delta_sum += delta
        if delta > 0:
            self.proxy_learned_better += 1
        elif delta < 0:
            self.proxy_learned_worse += 1
        else:
            self.proxy_same += 1


class _DecisionLinkedSelector:
    """観測後に既存PolicySeatSelectorへそのまま委譲するwrapper。"""

    def __init__(
        self,
        delegate: PolicySeatSelector,
        recorder: _DecisionLinkedRecorder,
    ) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation, options)
        return self._delegate(observation, options)


def run_phase05_decision_linked(
    seeds: tuple[int, ...],
    estimator: BucketedExpectedCountEstimator,
    *,
    rules: RuleSet | None = None,
) -> Phase05DecisionLinkedResult:
    """test partition seedをdeterministicに再実行し、3 beliefを同一positionで比較する。"""
    if not isinstance(seeds, tuple) or not seeds:
        raise ValueError("seeds must be a non-empty tuple")
    if any(type(seed) is not int for seed in seeds):
        raise TypeError("seeds must contain only int values")
    if not isinstance(estimator, BucketedExpectedCountEstimator):
        raise TypeError("estimator must be a BucketedExpectedCountEstimator")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")

    turn_decisions = 0
    eligible_positions = 0
    oracle_buildable_positions = 0
    consumer_active_positions = 0
    learned_conservation_exclusions = 0
    baseline_learned_divergences = 0
    learned_oracle_agreements = 0
    baseline_oracle_agreements = 0
    proxy_learned_better = 0
    proxy_same = 0
    proxy_learned_worse = 0
    proxy_delta_sum = 0
    backoff_level_counts: Counter[int] = Counter()

    started = time.perf_counter()
    for seed in seeds:
        match_state = MatchState(seed=seed, rules=rules or RuleSet.default())
        recorder = _DecisionLinkedRecorder(match_state, estimator)
        selectors: dict[EngineSeat, ActionSelector] = {}
        for engine_seat in EngineSeat:
            delegate = PolicySeatSelector(engine_seat, TwoStepUkeirePolicy())
            selectors[engine_seat] = _DecisionLinkedSelector(delegate, recorder)
        run_hanchan(match_state, selectors)

        turn_decisions += recorder.turn_decisions
        eligible_positions += recorder.eligible_positions
        oracle_buildable_positions += recorder.oracle_buildable_positions
        consumer_active_positions += recorder.consumer_active_positions
        learned_conservation_exclusions += recorder.learned_conservation_exclusions
        baseline_learned_divergences += recorder.baseline_learned_divergences
        learned_oracle_agreements += recorder.learned_oracle_agreements
        baseline_oracle_agreements += recorder.baseline_oracle_agreements
        proxy_learned_better += recorder.proxy_learned_better
        proxy_same += recorder.proxy_same
        proxy_learned_worse += recorder.proxy_learned_worse
        proxy_delta_sum += recorder.proxy_delta_sum
        backoff_level_counts.update(recorder.backoff_level_counts)

    return Phase05DecisionLinkedResult(
        seeds=seeds,
        turn_decisions=turn_decisions,
        eligible_positions=eligible_positions,
        oracle_buildable_positions=oracle_buildable_positions,
        consumer_active_positions=consumer_active_positions,
        learned_conservation_exclusions=learned_conservation_exclusions,
        baseline_learned_divergences=baseline_learned_divergences,
        learned_oracle_agreements=learned_oracle_agreements,
        baseline_oracle_agreements=baseline_oracle_agreements,
        proxy_learned_better=proxy_learned_better,
        proxy_same=proxy_same,
        proxy_learned_worse=proxy_learned_worse,
        proxy_delta_sum=proxy_delta_sum,
        backoff_level_counts=tuple(sorted(backoff_level_counts.items())),
        wall_clock_seconds=time.perf_counter() - started,
    )
