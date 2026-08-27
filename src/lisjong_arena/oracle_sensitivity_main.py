"""lisjong-project #20 Track Bのpre-registered main measurement。

pilot後にIssue #20へlockした条件どおり、fresh seeds 20..29だけを使って
same-position baseline/oracle HandBeliefのdecision sensitivityを測る。

online trajectoryは既存TwoStepUkeirePolicyだけで進める。omniscient opponent
handsとlive wallはArena-side recorderだけが読み、PolicyInput / DecisionContext /
DecisionTraceへ流さない。
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from lisjong.belief import estimate_conditional_uniform_hand_belief
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies.experimental_hand_belief_sensitivity import (
    evaluate_hand_belief_sensitive_discard,
)
from lisjong.policies.two_step_ukeire import (
    _DecisionShantenEvaluator,
    _effective_tile_types,
    _remove_one_matching_tile,
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
from lisjong_engine.observation import SeatObservation
from lisjong_engine.public_state import public_tile
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.lisjong_engine.domain_conversion import tile_from_public_tile
from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.oracle_sensitivity_pilot import (
    _build_oracle_belief,
    _opponent_slot_counts_by_wind,
)

_MAIN_SEEDS = tuple(range(20, 30))
_MINIMUM_ACTIVE_POSITIONS = 400
_MATERIALITY_THRESHOLD = 0.05
_WILSON_Z_95 = 1.959963984540054
_WINNING_ACTION_TYPES = (RonAction, TsumoAction)


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    """binary proportionのWilson 95% interval。"""

    low: float
    high: float


@dataclass(frozen=True, slots=True)
class OracleSensitivityMainSeedResult:
    """1 seedのpaired measurement aggregate。hidden raw stateは保持しない。"""

    seed: int
    total_decisions: int
    discard_eligible_decisions: int
    oracle_buildable_decisions: int
    consumer_active_decisions: int
    action_divergences: int
    proxy_oracle_better: int
    proxy_same: int
    proxy_oracle_worse: int
    proxy_delta_sum: int
    active_kind_counts: tuple[tuple[str, int], ...]
    divergence_kind_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class OracleSensitivityMainSummary:
    """pre-registered fresh 10-game main measurement aggregate。"""

    seeds: tuple[int, ...]
    total_decisions: int
    discard_eligible_decisions: int
    oracle_buildable_decisions: int
    consumer_active_decisions: int
    action_divergences: int
    proxy_oracle_better: int
    proxy_same: int
    proxy_oracle_worse: int
    proxy_delta_sum: int
    active_kind_counts: tuple[tuple[str, int], ...]
    divergence_kind_counts: tuple[tuple[str, int], ...]

    @property
    def action_divergence_rate(self) -> float:
        if self.consumer_active_decisions == 0:
            return 0.0
        return self.action_divergences / self.consumer_active_decisions

    @property
    def overall_divergence_rate(self) -> float:
        if self.discard_eligible_decisions == 0:
            return 0.0
        return self.action_divergences / self.discard_eligible_decisions

    @property
    def action_divergence_wilson_95(self) -> WilsonInterval | None:
        return _wilson_95(self.action_divergences, self.consumer_active_decisions)

    @property
    def materiality_classification(self) -> str:
        if self.consumer_active_decisions < _MINIMUM_ACTIVE_POSITIONS:
            return "insufficient coverage"
        interval = self.action_divergence_wilson_95
        if interval is None:
            return "insufficient coverage"
        if interval.high < _MATERIALITY_THRESHOLD:
            return "insensitive relative to 5% threshold"
        if interval.low > _MATERIALITY_THRESHOLD:
            return "sensitive relative to 5% threshold"
        return "inconclusive relative to 5% threshold"

    @property
    def proxy_compared_positions(self) -> int:
        return self.proxy_oracle_better + self.proxy_same + self.proxy_oracle_worse

    @property
    def proxy_mean_delta(self) -> float | None:
        if self.proxy_compared_positions == 0:
            return None
        return self.proxy_delta_sum / self.proxy_compared_positions


class _MainRecorder:
    """online selectorから分離されたArena-side omniscient paired observer。"""

    def __init__(self, match_state: MatchState) -> None:
        self._match_state = match_state
        self.total_decisions = 0
        self.discard_eligible_decisions = 0
        self.oracle_buildable_decisions = 0
        self.consumer_active_decisions = 0
        self.action_divergences = 0
        self.proxy_oracle_better = 0
        self.proxy_same = 0
        self.proxy_oracle_worse = 0
        self.proxy_delta_sum = 0
        self.active_kind_counts: Counter[str] = Counter()
        self.divergence_kind_counts: Counter[str] = Counter()

    def observe(self, observation: SeatObservation, options: object) -> None:
        self.total_decisions += 1
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

        self.discard_eligible_decisions += 1
        policy_input = engine_decision.context.input
        baseline = estimate_conditional_uniform_hand_belief(
            policy_input,
            _opponent_slot_counts_by_wind(policy_input),
        )
        oracle = _build_oracle_belief(self._match_state, policy_input, baseline)
        if oracle is None:
            return
        self.oracle_buildable_decisions += 1

        baseline_decision = evaluate_hand_belief_sensitive_discard(
            policy_input,
            discard_actions,
            baseline,
        )
        if not baseline_decision.consumer_active:
            return

        oracle_decision = evaluate_hand_belief_sensitive_discard(
            policy_input,
            discard_actions,
            oracle,
        )
        if not oracle_decision.consumer_active:
            raise RuntimeError(
                "consumer activation must depend only on non-belief hard filters"
            )

        decision_kind = observation.decision_kind.value
        self.consumer_active_decisions += 1
        self.active_kind_counts[decision_kind] += 1
        if baseline_decision.action == oracle_decision.action:
            return

        self.action_divergences += 1
        self.divergence_kind_counts[decision_kind] += 1
        baseline_proxy = _live_wall_structural_ukeire(
            self._match_state,
            policy_input,
            baseline_decision.action,
        )
        oracle_proxy = _live_wall_structural_ukeire(
            self._match_state,
            policy_input,
            oracle_decision.action,
        )
        delta = oracle_proxy - baseline_proxy
        self.proxy_delta_sum += delta
        if delta > 0:
            self.proxy_oracle_better += 1
        elif delta < 0:
            self.proxy_oracle_worse += 1
        else:
            self.proxy_same += 1

    def result(self, seed: int) -> OracleSensitivityMainSeedResult:
        return OracleSensitivityMainSeedResult(
            seed=seed,
            total_decisions=self.total_decisions,
            discard_eligible_decisions=self.discard_eligible_decisions,
            oracle_buildable_decisions=self.oracle_buildable_decisions,
            consumer_active_decisions=self.consumer_active_decisions,
            action_divergences=self.action_divergences,
            proxy_oracle_better=self.proxy_oracle_better,
            proxy_same=self.proxy_same,
            proxy_oracle_worse=self.proxy_oracle_worse,
            proxy_delta_sum=self.proxy_delta_sum,
            active_kind_counts=tuple(sorted(self.active_kind_counts.items())),
            divergence_kind_counts=tuple(sorted(self.divergence_kind_counts.items())),
        )


class _MainSelector:
    """paired観測後に既存PolicySeatSelectorへそのまま委譲するwrapper。"""

    def __init__(self, delegate: PolicySeatSelector, recorder: _MainRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation, options)
        return self._delegate(observation, options)


def _live_wall_structural_ukeire(
    match_state: MatchState,
    policy_input: PolicyInput,
    action: DiscardAction,
) -> int:
    """selected discard後のstructural effective tilesを現在live wall上で数える。"""
    round_state = match_state.active_round
    if round_state is None:
        raise RuntimeError("live-wall proxy requires an active round")

    post_discard_hand = _remove_one_matching_tile(
        policy_input.own_hand.concealed_tiles,
        action.tile,
    )
    evaluator = _DecisionShantenEvaluator()
    shanten = evaluator.calculate(post_discard_hand)
    effective_types = frozenset(
        _effective_tile_types(post_discard_hand, shanten, evaluator)
    )
    return sum(
        1
        for engine_tile in round_state.remaining_tiles
        if tile_from_public_tile(public_tile(engine_tile)).tile_type in effective_types
    )


def _wilson_95(successes: int, total: int) -> WilsonInterval | None:
    if type(successes) is not int or type(total) is not int:
        raise TypeError("successes and total must be int")
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("require 0 <= successes <= total")
    if total == 0:
        return None

    proportion = successes / total
    z2 = _WILSON_Z_95 * _WILSON_Z_95
    denominator = 1.0 + z2 / total
    center = (proportion + z2 / (2.0 * total)) / denominator
    half_width = (
        _WILSON_Z_95
        * math.sqrt(
            proportion * (1.0 - proportion) / total + z2 / (4.0 * total * total)
        )
        / denominator
    )
    return WilsonInterval(center - half_width, center + half_width)


def run_oracle_sensitivity_main_seed(
    seed: int,
    *,
    rules: RuleSet | None = None,
) -> OracleSensitivityMainSeedResult:
    """1 fresh seedのpaired main measurementを実行する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")

    match_state = MatchState(seed=seed, rules=rules or RuleSet.default())
    recorder = _MainRecorder(match_state)
    selectors: dict[EngineSeat, ActionSelector] = {}
    for seat in EngineSeat:
        delegate = PolicySeatSelector(seat, TwoStepUkeirePolicy())
        selectors[seat] = _MainSelector(delegate, recorder)

    run_hanchan(match_state, selectors)
    return recorder.result(seed)


def _aggregate_main_results(
    results: Sequence[OracleSensitivityMainSeedResult],
) -> OracleSensitivityMainSummary:
    normalized = tuple(results)
    if not normalized:
        raise ValueError("results must not be empty")

    active_kinds: Counter[str] = Counter()
    divergence_kinds: Counter[str] = Counter()
    for result in normalized:
        active_kinds.update(dict(result.active_kind_counts))
        divergence_kinds.update(dict(result.divergence_kind_counts))

    return OracleSensitivityMainSummary(
        seeds=tuple(result.seed for result in normalized),
        total_decisions=sum(result.total_decisions for result in normalized),
        discard_eligible_decisions=sum(
            result.discard_eligible_decisions for result in normalized
        ),
        oracle_buildable_decisions=sum(
            result.oracle_buildable_decisions for result in normalized
        ),
        consumer_active_decisions=sum(
            result.consumer_active_decisions for result in normalized
        ),
        action_divergences=sum(result.action_divergences for result in normalized),
        proxy_oracle_better=sum(result.proxy_oracle_better for result in normalized),
        proxy_same=sum(result.proxy_same for result in normalized),
        proxy_oracle_worse=sum(result.proxy_oracle_worse for result in normalized),
        proxy_delta_sum=sum(result.proxy_delta_sum for result in normalized),
        active_kind_counts=tuple(sorted(active_kinds.items())),
        divergence_kind_counts=tuple(sorted(divergence_kinds.items())),
    )


def run_oracle_sensitivity_main() -> OracleSensitivityMainSummary:
    """pre-registered fresh seeds 20..29を変更せず実行する。"""
    results = tuple(run_oracle_sensitivity_main_seed(seed) for seed in _MAIN_SEEDS)
    return _aggregate_main_results(results)


def _build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Run preregistered Track B paired main measurement (seeds 20:29)",
    )


def _format_percent(value: float) -> str:
    return f"{100.0 * value:.2f}%"


def _print_summary(summary: OracleSensitivityMainSummary) -> None:
    print("Track B oracle sensitivity main measurement")
    print(f"seeds: {summary.seeds[0]}:{summary.seeds[-1]} ({len(summary.seeds)})")
    print(f"total decisions: {summary.total_decisions}")
    print(f"discard eligible: {summary.discard_eligible_decisions}")
    print(f"oracle buildable: {summary.oracle_buildable_decisions}")
    print(f"consumer active: {summary.consumer_active_decisions}")
    print(
        "action divergence: "
        f"{summary.action_divergences}/{summary.consumer_active_decisions} "
        f"({_format_percent(summary.action_divergence_rate)})"
    )
    interval = summary.action_divergence_wilson_95
    if interval is not None:
        print(
            "Wilson 95% CI: "
            f"[{_format_percent(interval.low)}, {_format_percent(interval.high)}]"
        )
    print(f"classification: {summary.materiality_classification}")
    print(
        "overall eligible divergence: "
        f"{summary.action_divergences}/{summary.discard_eligible_decisions} "
        f"({_format_percent(summary.overall_divergence_rate)})"
    )
    print(
        "live-wall proxy on divergent positions: "
        f"oracle_better={summary.proxy_oracle_better}, "
        f"same={summary.proxy_same}, "
        f"oracle_worse={summary.proxy_oracle_worse}"
    )
    if summary.proxy_mean_delta is not None:
        print(f"live-wall proxy mean paired delta: {summary.proxy_mean_delta:.3f}")
    print("exploratory decision-kind divergence:")
    active_by_kind = dict(summary.active_kind_counts)
    divergence_by_kind = dict(summary.divergence_kind_counts)
    for kind in sorted(active_by_kind):
        active = active_by_kind[kind]
        divergent = divergence_by_kind.get(kind, 0)
        print(f"  {kind}: {divergent}/{active}")


def main(argv: Sequence[str] | None = None) -> int:
    _build_arg_parser().parse_args(argv)
    summary = run_oracle_sensitivity_main()
    _print_summary(summary)
    if summary.consumer_active_decisions < _MINIMUM_ACTIVE_POSITIONS:
        print(
            "result: insufficient coverage "
            f"(< {_MINIMUM_ACTIVE_POSITIONS} consumer-active positions)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
