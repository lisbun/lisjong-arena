"""lisjong-project #20 Track Bのpre-registered pilotだけを実行する。

このmoduleはoracle-vs-baseline action divergenceを計算・表示しない。pilotで確認する
のは、Issue #20で事前に許可したcoverage / consumer activation / stable-state
exclusionだけである。main measurementはpilot結果をIssueへ記録し、effect sizeと
sample ruleをlockした後の別stepとする。

privileged engine stateはArena-side recorderだけが読み、online trajectoryを決める
`PolicySeatSelector`へは従来どおりplayer-safe `SeatObservation`だけを渡す。
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from lisjong.belief import (
    ConcealedHandBelief,
    estimate_conditional_uniform_hand_belief,
    exact_hand_belief_with_waits,
    wind_for_seat,
    wind_index,
)
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies.experimental_hand_belief_sensitivity import (
    evaluate_hand_belief_sensitive_discard,
)
from lisjong.policy_contract import (
    DiscardAction,
    PolicyInput,
    RiichiAction,
    RonAction,
    Seat,
    TsumoAction,
)
from lisjong_engine.driver import ActionSelector, run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import SeatObservation
from lisjong_engine.public_state import public_meld, public_tile
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.lisjong_engine.domain_conversion import (
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
)
from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.single_round_compare import parse_seeds

_WINNING_ACTION_TYPES = (RonAction, TsumoAction)
_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_EQUIVALENT_TILE_COUNT = 3


@dataclass(frozen=True, slots=True)
class OracleSensitivityPilotSeedResult:
    """1 seedのpilot coverage。oracle action / divergenceは保持しない。"""

    seed: int
    total_decisions: int
    discard_eligible_decisions: int
    oracle_buildable_decisions: int
    consumer_active_decisions: int
    unstable_state_exclusions: int
    decision_kind_counts: tuple[tuple[str, int], ...]
    discard_eligible_kind_counts: tuple[tuple[str, int], ...]
    oracle_buildable_kind_counts: tuple[tuple[str, int], ...]
    unstable_exclusion_kind_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class OracleSensitivityPilotSummary:
    """複数seedのpilot coverage aggregate。"""

    seeds: tuple[int, ...]
    total_decisions: int
    discard_eligible_decisions: int
    oracle_buildable_decisions: int
    consumer_active_decisions: int
    unstable_state_exclusions: int
    decision_kind_counts: tuple[tuple[str, int], ...]
    discard_eligible_kind_counts: tuple[tuple[str, int], ...]
    oracle_buildable_kind_counts: tuple[tuple[str, int], ...]
    unstable_exclusion_kind_counts: tuple[tuple[str, int], ...]


class _PilotRecorder:
    """online selectorから分離されたArena-side omniscient pilot observer。"""

    def __init__(self, match_state: MatchState) -> None:
        self._match_state = match_state
        self.total_decisions = 0
        self.discard_eligible_decisions = 0
        self.oracle_buildable_decisions = 0
        self.consumer_active_decisions = 0
        self.unstable_state_exclusions = 0
        self.decision_kind_counts: Counter[str] = Counter()
        self.discard_eligible_kind_counts: Counter[str] = Counter()
        self.oracle_buildable_kind_counts: Counter[str] = Counter()
        self.unstable_exclusion_kind_counts: Counter[str] = Counter()

    def observe(self, observation: SeatObservation, options: object) -> None:
        self.total_decisions += 1
        decision_kind = observation.decision_kind.value
        self.decision_kind_counts[decision_kind] += 1

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
        self.discard_eligible_kind_counts[decision_kind] += 1
        policy_input = engine_decision.context.input
        baseline = estimate_conditional_uniform_hand_belief(
            policy_input,
            _opponent_slot_counts_by_wind(policy_input),
        )
        if _build_oracle_belief(self._match_state, policy_input, baseline) is None:
            self.unstable_state_exclusions += 1
            self.unstable_exclusion_kind_counts[decision_kind] += 1
            return

        self.oracle_buildable_decisions += 1
        self.oracle_buildable_kind_counts[decision_kind] += 1
        baseline_decision = evaluate_hand_belief_sensitive_discard(
            policy_input,
            discard_actions,
            baseline,
        )
        if baseline_decision.consumer_active:
            self.consumer_active_decisions += 1

    def result(self, seed: int) -> OracleSensitivityPilotSeedResult:
        return OracleSensitivityPilotSeedResult(
            seed=seed,
            total_decisions=self.total_decisions,
            discard_eligible_decisions=self.discard_eligible_decisions,
            oracle_buildable_decisions=self.oracle_buildable_decisions,
            consumer_active_decisions=self.consumer_active_decisions,
            unstable_state_exclusions=self.unstable_state_exclusions,
            decision_kind_counts=tuple(sorted(self.decision_kind_counts.items())),
            discard_eligible_kind_counts=tuple(
                sorted(self.discard_eligible_kind_counts.items())
            ),
            oracle_buildable_kind_counts=tuple(
                sorted(self.oracle_buildable_kind_counts.items())
            ),
            unstable_exclusion_kind_counts=tuple(
                sorted(self.unstable_exclusion_kind_counts.items())
            ),
        )


class _PilotSelector:
    """pilot観測後に既存PolicySeatSelectorへそのまま委譲するwrapper。"""

    def __init__(
        self,
        delegate: PolicySeatSelector,
        recorder: _PilotRecorder,
    ) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation, options)
        return self._delegate(observation, options)


def _opponent_slot_counts_by_wind(
    policy_input: PolicyInput,
) -> tuple[int, int, int, int]:
    """公開meld数だけから他家stable concealed slot数をWind順で導出する。"""
    counts = [0, 0, 0, 0]
    for seat in Seat:
        wind_number = wind_index(wind_for_seat(seat, policy_input.round.dealer_seat))
        if seat is policy_input.self_seat:
            counts[wind_number] = 0
            continue
        meld_count = len(policy_input.players[int(seat)].melds)
        concealed_slots = (
            _STABLE_EQUIVALENT_TILE_COUNT - _MELD_EQUIVALENT_TILE_COUNT * meld_count
        )
        if concealed_slots < 0:
            raise RuntimeError("public meld count implies negative concealed slots")
        counts[wind_number] = concealed_slots
    return tuple(counts)


def _build_oracle_belief(
    match_state: MatchState,
    policy_input: PolicyInput,
    baseline: ConcealedHandBelief,
) -> ConcealedHandBelief | None:
    """他家だけをomniscient exact beliefへ置換し、不安定stateならNoneを返す。"""
    round_state = match_state.active_round
    if round_state is None:
        raise RuntimeError("oracle belief requires an active round")

    hands = list(baseline.hands)
    for engine_seat in EngineSeat:
        seat = seat_from_engine_seat(engine_seat)
        if seat is policy_input.self_seat:
            continue

        concealed_tiles = tuple(
            tile_from_public_tile(public_tile(tile))
            for tile in round_state.hand_tiles(engine_seat)
        )
        melds = tuple(
            public_meld_from_engine_meld(public_meld(meld))
            for meld in round_state.melds(engine_seat)
        )
        if (
            len(concealed_tiles) + _MELD_EQUIVALENT_TILE_COUNT * len(melds)
            != _STABLE_EQUIVALENT_TILE_COUNT
        ):
            return None

        wind_number = wind_index(wind_for_seat(seat, policy_input.round.dealer_seat))
        hands[wind_number] = exact_hand_belief_with_waits(
            concealed_tiles,
            own_melds=melds,
        )

    return ConcealedHandBelief(hands=tuple(hands))


def run_oracle_sensitivity_pilot_seed(
    seed: int,
    *,
    rules: RuleSet | None = None,
) -> OracleSensitivityPilotSeedResult:
    """TwoStepUkeirePolicy x4の1半荘でTrack B pilot coverageだけを収集する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")

    match_state = MatchState(seed=seed, rules=rules or RuleSet.default())
    recorder = _PilotRecorder(match_state)
    selectors: dict[EngineSeat, ActionSelector] = {}
    for seat in EngineSeat:
        delegate = PolicySeatSelector(seat, TwoStepUkeirePolicy())
        selectors[seat] = _PilotSelector(delegate, recorder)

    run_hanchan(match_state, selectors)
    return recorder.result(seed)


def run_oracle_sensitivity_pilot(
    seeds: Sequence[int],
) -> OracleSensitivityPilotSummary:
    """複数seedをserialに実行し、oracle resultを露出せずcoverageを集約する。"""
    normalized = tuple(seeds)
    if not normalized:
        raise ValueError("seeds must not be empty")
    if any(type(seed) is not int for seed in normalized):
        raise TypeError("seeds must contain only int values")

    results = tuple(run_oracle_sensitivity_pilot_seed(seed) for seed in normalized)
    decision_kinds: Counter[str] = Counter()
    discard_eligible_kinds: Counter[str] = Counter()
    oracle_buildable_kinds: Counter[str] = Counter()
    unstable_exclusion_kinds: Counter[str] = Counter()
    for result in results:
        decision_kinds.update(dict(result.decision_kind_counts))
        discard_eligible_kinds.update(dict(result.discard_eligible_kind_counts))
        oracle_buildable_kinds.update(dict(result.oracle_buildable_kind_counts))
        unstable_exclusion_kinds.update(dict(result.unstable_exclusion_kind_counts))

    return OracleSensitivityPilotSummary(
        seeds=normalized,
        total_decisions=sum(result.total_decisions for result in results),
        discard_eligible_decisions=sum(
            result.discard_eligible_decisions for result in results
        ),
        oracle_buildable_decisions=sum(
            result.oracle_buildable_decisions for result in results
        ),
        consumer_active_decisions=sum(
            result.consumer_active_decisions for result in results
        ),
        unstable_state_exclusions=sum(
            result.unstable_state_exclusions for result in results
        ),
        decision_kind_counts=tuple(sorted(decision_kinds.items())),
        discard_eligible_kind_counts=tuple(sorted(discard_eligible_kinds.items())),
        oracle_buildable_kind_counts=tuple(sorted(oracle_buildable_kinds.items())),
        unstable_exclusion_kind_counts=tuple(sorted(unstable_exclusion_kinds.items())),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Track B coverage-only oracle sensitivity pilot",
    )
    parser.add_argument(
        "--seeds",
        type=parse_seeds,
        default=tuple(range(20)),
        metavar="N|START:END",
        help="single seed or inclusive range (default: 0:19)",
    )
    return parser


def _print_summary(summary: OracleSensitivityPilotSummary) -> None:
    print("Track B oracle sensitivity pilot (coverage only)")
    print(f"seeds: {len(summary.seeds)}")
    print(f"total decisions: {summary.total_decisions}")
    print(f"discard eligible: {summary.discard_eligible_decisions}")
    print(f"oracle buildable: {summary.oracle_buildable_decisions}")
    print(f"consumer active: {summary.consumer_active_decisions}")
    print(f"unstable exclusions: {summary.unstable_state_exclusions}")
    print("decision kinds:")
    for kind, count in summary.decision_kind_counts:
        print(f"  {kind}: {count}")

    eligible_by_kind = dict(summary.discard_eligible_kind_counts)
    buildable_by_kind = dict(summary.oracle_buildable_kind_counts)
    excluded_by_kind = dict(summary.unstable_exclusion_kind_counts)
    coverage_kinds = sorted(
        set(eligible_by_kind) | set(buildable_by_kind) | set(excluded_by_kind)
    )
    if coverage_kinds:
        print("discard coverage by decision kind:")
        for kind in coverage_kinds:
            print(
                f"  {kind}: eligible={eligible_by_kind.get(kind, 0)}, "
                f"buildable={buildable_by_kind.get(kind, 0)}, "
                f"unstable_excluded={excluded_by_kind.get(kind, 0)}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    summary = run_oracle_sensitivity_pilot(args.seeds)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
