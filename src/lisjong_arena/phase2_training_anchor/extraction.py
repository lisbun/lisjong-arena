"""first-party engine execution中のanchor-time freezeとsample composition。

TURN anchorへ到達した瞬間に、player-safe valueをfreezeしてから、同じanchorの
omniscient truthを別pathでlabel化する。

```text
TURN callback
  ├─ trusted snapshot   : selectorが受け取り済みのSeatObservation
  ├─ trusted history    : build_round_evidence(active_round, viewer)
  ├─ rule provenance    : effective RuleSet fingerprint
  │        -> freeze_player_safe_anchor()   (player-safe path)
  │
  └─ omniscient truth   : build_exact_training_labels(match_state, viewer)
                             (training-only path)

両pathが独立に完成したあとで compose_training_sample()
```

`build_round_evidence()`を呼ぶこのorchestration pointまではactive
`RoundState`を参照してよい。その出力より下流のplayer-safe path
（`player_safe_anchor`）へは`MatchState` / `RoundState` / internal omniscient
eventを渡さない。omniscient stateを受け取るのは`training_labels`だけである。

anchor eligibilityは`SeatObservation.decision_kind == TURN`だけで決める。legal
action composition、winning / riichi / kan capability、reaction availability、
label availabilityでanchorを選別しない。

Phase 0.5の`phase05_belief_slice`はauthoritative measurement済みのdisposable
experimentであり、本moduleはそのsemanticsを再利用も変更もしない別pathである。
generic replay / event-sourcing / persistence frameworkは作らない。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lisjong.policies import TwoStepUkeirePolicy
from lisjong_engine.driver import ActionSelector, run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    collect_pipeline_provenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    AnchorSourceIdentity,
    freeze_player_safe_anchor,
)
from lisjong_arena.phase2_training_anchor.training_labels import (
    build_exact_training_labels,
)
from lisjong_arena.phase2_training_anchor.training_sample import (
    TrainingSample,
    compose_training_sample,
)

FIRST_PARTY_SOURCE_CLASS = "first-party-bootstrap"
"""first-party `lisjong-engine` self-play由来のsource class identity。

既定のonline trajectoryは4席とも`TwoStepUkeirePolicy`で進める。callerが
`seat_policy_factories`でexplicitなfirst-party populationを指定した場合も
source classは同じである。Phase 2はanchor / label contractを固定することが
目的であり、Policy強度はここでの評価対象ではない。
"""


SeatPolicyFactories = Mapping["EngineSeat", Callable[[], object]]
"""1 gameのseatごとPolicy factory。`None`は既定populationを意味する。"""


def normalized_seat_policy_factories(
    factories: SeatPolicyFactories | None,
) -> dict[EngineSeat, Callable[[], object]]:
    """explicitなper-seat Policy factoryを検証して正準seat順へ正規化する。

    `None`はこのmoduleの既定populationである`TwoStepUkeirePolicy x4`を意味する。
    generic Policy configuration frameworkではなく、callerが既に決めた
    populationをそのまま受け取るためのseamである。Policy semanticsの検証は
    既存のlisjong execution boundaryへ委ねる。
    """
    if factories is None:
        return {seat: TwoStepUkeirePolicy for seat in EngineSeat}
    if not isinstance(factories, Mapping):
        raise TypeError("seat policy factories must be a mapping or None")
    if set(factories) != set(EngineSeat):
        raise ValueError("seat policy factories must cover every engine Seat exactly")
    if any(not callable(factories[seat]) for seat in EngineSeat):
        raise TypeError("every seat policy factory must be callable")
    return {seat: factories[seat] for seat in EngineSeat}


class Phase2AnchorRecorder:
    """TURN anchorでplayer-safe freezeとtraining-only labelingを行うobserver。

    online selectorからは分離されており、Policyへ渡す情報には影響しない。
    """

    def __init__(self, match_state: MatchState, source: AnchorSourceIdentity) -> None:
        if not isinstance(match_state, MatchState):
            raise TypeError("match_state must be a lisjong-engine MatchState")
        if not isinstance(source, AnchorSourceIdentity):
            raise TypeError("source must be an AnchorSourceIdentity")
        self._match_state = match_state
        self._source = source
        self._provenance = collect_pipeline_provenance(match_state.rules)
        self._rule_provenance = self._provenance.effective_rules
        self.total_decisions = 0
        self.turn_anchors = 0
        self.samples: list[TrainingSample] = []

    def observe(self, observation: SeatObservation) -> None:
        """1 decisionを観測し、TURN anchorであればsampleをcaptureする。"""
        if not isinstance(observation, SeatObservation):
            raise TypeError("observation must be a lisjong-engine SeatObservation")
        self.total_decisions += 1
        if observation.decision_kind is not ObservationDecisionKind.TURN:
            return

        anchor_index = self.turn_anchors
        self.turn_anchors += 1

        active_round = self._match_state.active_round
        if active_round is None:
            raise RuntimeError("a TURN anchor requires an active round")

        # --- player-safe path: anchor時点でfreezeする ---
        anchor = freeze_player_safe_anchor(
            source=self._source,
            observation=observation,
            evidence=build_round_evidence(active_round, observation.viewer_seat),
            round_revision=active_round.revision,
            anchor_index=anchor_index,
            rule_provenance=self._rule_provenance,
        )

        # --- training-only path: 同じanchorのomniscient truthから別に構成する ---
        # labelのanchor identityはanchorからcopyせず、privileged stateから
        # 独立に導出する。compositionのalignment検証はそのうえで行う。
        labels = build_exact_training_labels(self._match_state, observation.viewer_seat)

        self.samples.append(compose_training_sample(anchor, labels, self._provenance))


class _Phase2Selector:
    """anchor観測後に既存`PolicySeatSelector`へそのまま委譲するwrapper。"""

    __slots__ = ("_delegate", "_recorder")

    def __init__(
        self, delegate: PolicySeatSelector, recorder: Phase2AnchorRecorder
    ) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation)
        return self._delegate(observation, options)


@dataclass(frozen=True, slots=True)
class Phase2GameExtraction:
    """1 gameのanchor coverageとcomposed samples。"""

    source: AnchorSourceIdentity
    total_decisions: int
    turn_anchors: int
    samples: tuple[TrainingSample, ...]

    def __post_init__(self) -> None:
        if len(self.samples) != self.turn_anchors:
            raise ValueError(
                "every TURN anchor must produce exactly one composed sample; "
                "target-specific label availability must not drop anchors"
            )


def extract_phase2_game(
    seed: int,
    *,
    rules: RuleSet | None = None,
    seat_policy_factories: SeatPolicyFactories | None = None,
) -> Phase2GameExtraction:
    """1 hanchanを実行し、全TURN anchorのtraining samplesを収集する。

    optional targetがunavailableでもanchorはdropしない。TURN anchor数と
    sample数は常に一致する。

    `seat_policy_factories`はseatごとのPolicy factoryをexplicitに指定する。
    省略時のpopulationはこれまでどおり`TwoStepUkeirePolicy x4`であり、anchor /
    cutoff / label semanticsはPolicy populationに依存しない。
    """
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")
    policy_factories = normalized_seat_policy_factories(seat_policy_factories)

    match_state = MatchState(seed=seed, rules=rules or RuleSet.default())
    source = AnchorSourceIdentity(
        source_class=FIRST_PARTY_SOURCE_CLASS,
        game_seed=seed,
    )
    recorder = Phase2AnchorRecorder(match_state, source)

    selectors: dict[EngineSeat, ActionSelector] = {}
    for engine_seat in EngineSeat:
        delegate = PolicySeatSelector(engine_seat, policy_factories[engine_seat]())
        selectors[engine_seat] = _Phase2Selector(delegate, recorder)

    run_hanchan(match_state, selectors)

    return Phase2GameExtraction(
        source=source,
        total_decisions=recorder.total_decisions,
        turn_anchors=recorder.turn_anchors,
        samples=tuple(recorder.samples),
    )
