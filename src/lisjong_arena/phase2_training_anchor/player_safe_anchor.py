"""Phase 2のfrozen player-safe anchor value。

このmoduleはPhase 2 pipelineのplayer-safe側だけを所有する。`MatchState`、
`RoundState`、internal omniscient event、training label、label availability、
exclusion reason、hidden truthはいずれもここへ入らない。moduleのimport一覧が
そのままinformation-flow boundaryの宣言になる。

anchorへbindingするplayer-safe valueは、engine-ownedな2つのtrusted
declassifierの出力だけである。

```text
MatchState  -> build_seat_observation() -> SeatObservation
RoundState  -> build_round_evidence()   -> ordered player-safe evidence
```

Arenaはこの2つの出力を再解釈せず、そのままtrusted snapshotとして保持する。
engine-owned `SeatObservation` / `RoundEvidence`のsemanticsをArena側で
再定義しない。

## Anchor-time freeze

future leakage防止のprimary mechanismは、anchor到達時にplayer-safe valueを
freezeすることである。`SeatObservation`はfrozen valueであり、
`build_round_evidence()`はその呼び出し時点までのevidenceを新しいtupleとして
materializeする。したがってanchor capture後にgameが進んでも、既にfreezeされた
anchorのvalueは変化しない。

終了後のfinal stateから過去anchorを再構成する経路はprimary implementationに
しない。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.round_evidence import RoundEvidence
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.phase2_training_anchor.rule_provenance import (
    EffectiveRuleProvenance,
)


class AnchorKind(Enum):
    """Phase 2 initial contractで採用するanchor種別。

    initial anchorはTURN / pre-actionだけである。`RIICHI_DISCARD`やreaction
    decisionは本Phaseのanchorへ追加しない。
    """

    TURN = "turn"


@dataclass(frozen=True, slots=True)
class AnchorSourceIdentity:
    """anchorがどのsource / gameで観測されたかを表すimmutable identity。

    first-party bootstrap sourceではgame identityは`MatchState`のseedである。
    persistent global UUIDやgeneric dataset row IDは持たない。
    """

    source_class: str
    game_seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, str) or not self.source_class:
            raise ValueError("source_class must be a non-empty str")
        if type(self.game_seed) is not int:
            raise TypeError("game_seed must be an int")


@dataclass(frozen=True, slots=True)
class FrozenPlayerSafeAnchor:
    """1つのTURN anchorでfreezeされたplayer-safe input value。

    この型にlabel、label availability、unsupported reason、hidden truthを
    持たせない。それらはtraining-only metadataであり、別pathが所有する。

    `hand_number`と`honba`は繰り返し局（連荘 / 流局続行）を区別するための
    repeated-hand discriminatorとして両方bindingする。`round_revision`は局内で
    単調増加するengine state revisionであり、同一局内でanchorのstate positionを
    deterministicに識別する。
    """

    source: AnchorSourceIdentity
    hand_number: int
    honba: int
    round_revision: int
    viewer_seat: EngineSeat
    anchor_kind: AnchorKind
    anchor_index: int
    observation: SeatObservation
    evidence: tuple[RoundEvidence, ...]
    rule_provenance: EffectiveRuleProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.source, AnchorSourceIdentity):
            raise TypeError("source must be an AnchorSourceIdentity")
        if type(self.hand_number) is not int or self.hand_number < 1:
            raise ValueError("hand_number must be a positive int")
        if type(self.honba) is not int or self.honba < 0:
            raise ValueError("honba must be a non-negative int")
        if type(self.round_revision) is not int or self.round_revision < 0:
            raise ValueError("round_revision must be a non-negative int")
        if not isinstance(self.viewer_seat, EngineSeat):
            raise TypeError("viewer_seat must be a lisjong-engine Seat")
        if self.anchor_kind is not AnchorKind.TURN:
            raise ValueError("Phase 2 initial anchors must be TURN anchors")
        if type(self.anchor_index) is not int or self.anchor_index < 0:
            raise ValueError("anchor_index must be a non-negative int")
        if not isinstance(self.observation, SeatObservation):
            raise TypeError("observation must be a lisjong-engine SeatObservation")
        if self.observation.viewer_seat is not self.viewer_seat:
            raise ValueError("observation viewer seat must match the anchor viewer")
        if self.observation.decision_kind is not ObservationDecisionKind.TURN:
            raise ValueError("anchor observation decision_kind must be TURN")
        if not isinstance(self.evidence, tuple):
            raise TypeError("evidence must be a tuple of RoundEvidence")
        for item in self.evidence:
            if not isinstance(item, RoundEvidence):
                raise TypeError("evidence must contain only RoundEvidence values")
        if not isinstance(self.rule_provenance, EffectiveRuleProvenance):
            raise TypeError("rule_provenance must be an EffectiveRuleProvenance")


def freeze_player_safe_anchor(
    *,
    source: AnchorSourceIdentity,
    observation: SeatObservation,
    evidence: tuple[RoundEvidence, ...],
    round_revision: int,
    anchor_index: int,
    rule_provenance: EffectiveRuleProvenance,
) -> FrozenPlayerSafeAnchor:
    """trusted engine projectionの出力からfrozen player-safe anchorを構成する。

    `observation`はselector callbackが既に受け取っているtrusted snapshotで
    あり、`evidence`はanchor時点で`build_round_evidence()`が返したtupleである。
    どちらもここで再解釈しない。`hand_number` / `honba`はplayer-safeな
    `SeatObservation`から取り、omniscient stateからは読み直さない。

    この関数のparameterに`MatchState` / `RoundState`が現れないことが、
    downstream player-safe pathのtype boundaryである。
    """
    if not isinstance(observation, SeatObservation):
        raise TypeError("observation must be a lisjong-engine SeatObservation")

    return FrozenPlayerSafeAnchor(
        source=source,
        hand_number=observation.hand_number,
        honba=observation.honba,
        round_revision=round_revision,
        viewer_seat=observation.viewer_seat,
        anchor_kind=AnchorKind.TURN,
        anchor_index=anchor_index,
        observation=observation,
        evidence=evidence,
        rule_provenance=rule_provenance,
    )
