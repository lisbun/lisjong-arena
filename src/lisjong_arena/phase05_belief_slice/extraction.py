"""Phase 0.5のbounded first-party self-play generationとTURN anchor抽出。

online trajectoryは既存`TwoStepUkeirePolicy` 4体だけで進める。Arena-side
recorderがpre-action snapshotごとに、

```text
seat-safe PolicyInput -> Phase05AnchorFeatures
MatchState            -> Phase05Labels
```

を別pathで生成し、`PolicySeatSelector`へ渡す情報はplayer-safe
`SeatObservation`のままにする。

anchorは`SeatObservation.decision_kind == TURN`だけとし、winning / riichi /
kan availability、Track B consumer activation、legal action compositionでは
選別しない。
"""

import time
from collections import Counter
from dataclasses import dataclass

from lisjong.belief import (
    TILE_TYPE_COUNT,
    estimate_conditional_uniform_hand_belief,
    tile_type_from_index,
)
from lisjong.policies import TwoStepUkeirePolicy
from lisjong_engine.driver import ActionSelector, run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.oracle_sensitivity_pilot import _opponent_slot_counts_by_wind
from lisjong_arena.phase05_belief_slice.feature import (
    encode_phase05_anchor_features,
)
from lisjong_arena.phase05_belief_slice.label import (
    Phase05LabelExclusionReason,
    build_phase05_labels,
)
from lisjong_arena.phase05_belief_slice.sample import (
    Phase05Sample,
    partition_for_seed,
)

ONLINE_POLICY_IDENTITY = "TwoStepUkeirePolicy"
"""online trajectoryを進めるPolicy identity。4席すべてで同じ。"""

_CANONICAL_TILE_TYPES = tuple(
    tile_type_from_index(index) for index in range(TILE_TYPE_COUNT)
)


@dataclass(frozen=True, slots=True)
class Phase05GameExtraction:
    """1 gameのsampleとcoverage / exclusion counts。"""

    seed: int
    total_decisions: int
    turn_anchors: int
    samples: tuple[Phase05Sample, ...]
    exclusion_counts: tuple[tuple[str, int], ...]
    wall_clock_seconds: float

    @property
    def excluded_anchors(self) -> int:
        return sum(count for _, count in self.exclusion_counts)


class _Phase05Recorder:
    """online selectorから分離されたArena-side omniscient extraction observer。"""

    def __init__(self, match_state: MatchState, seed: int) -> None:
        self._match_state = match_state
        self._seed = seed
        self._partition = partition_for_seed(seed)
        self.total_decisions = 0
        self.turn_anchors = 0
        self.samples: list[Phase05Sample] = []
        self.exclusion_counts: Counter[str] = Counter()

    def observe(self, observation: SeatObservation) -> None:
        self.total_decisions += 1
        if observation.decision_kind is not ObservationDecisionKind.TURN:
            return

        anchor_index = self.turn_anchors
        self.turn_anchors += 1
        policy_input = build_policy_input(observation)

        features = encode_phase05_anchor_features(policy_input)
        label_result = build_phase05_labels(self._match_state, policy_input)
        if label_result.labels is None:
            reason = label_result.exclusion_reason
            if not isinstance(reason, Phase05LabelExclusionReason):
                raise RuntimeError("label exclusion must carry a reason code")
            self.exclusion_counts[reason.value] += 1
            return

        self.samples.append(
            Phase05Sample(
                seed=self._seed,
                partition=self._partition,
                anchor_index=anchor_index,
                features=features,
                labels=label_result.labels,
                baseline_expected_counts=_baseline_expected_counts(
                    policy_input,
                    features.opponent_winds,
                ),
            )
        )

    def result(self, wall_clock_seconds: float) -> Phase05GameExtraction:
        return Phase05GameExtraction(
            seed=self._seed,
            total_decisions=self.total_decisions,
            turn_anchors=self.turn_anchors,
            samples=tuple(self.samples),
            exclusion_counts=tuple(sorted(self.exclusion_counts.items())),
            wall_clock_seconds=wall_clock_seconds,
        )


class _Phase05Selector:
    """extraction観測後に既存PolicySeatSelectorへそのまま委譲するwrapper。"""

    def __init__(
        self, delegate: PolicySeatSelector, recorder: _Phase05Recorder
    ) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation)
        return self._delegate(observation, options)


def _baseline_expected_counts(
    policy_input: object,
    opponent_winds: tuple,
) -> tuple[tuple[float, ...], ...]:
    """conditional-uniform baselineをopponent row orderで取り出す。"""
    belief = estimate_conditional_uniform_hand_belief(
        policy_input,
        _opponent_slot_counts_by_wind(policy_input),
    )
    return tuple(
        tuple(
            belief.expected_count(wind, tile_type)
            for tile_type in _CANONICAL_TILE_TYPES
        )
        for wind in opponent_winds
    )


def extract_phase05_game(
    seed: int,
    *,
    rules: RuleSet | None = None,
) -> Phase05GameExtraction:
    """1 hanchanを実行し、TURN anchorのsampleとcoverageを収集する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")

    match_state = MatchState(seed=seed, rules=rules or RuleSet.default())
    recorder = _Phase05Recorder(match_state, seed)
    selectors: dict[EngineSeat, ActionSelector] = {}
    for engine_seat in EngineSeat:
        delegate = PolicySeatSelector(engine_seat, TwoStepUkeirePolicy())
        selectors[engine_seat] = _Phase05Selector(delegate, recorder)

    started = time.perf_counter()
    run_hanchan(match_state, selectors)
    return recorder.result(time.perf_counter() - started)
