"""frozen player-safe anchorとtraining-only labelsのcomposition。

compositionは、両pathがそれぞれ独立に完成したあとでだけ行う。

```text
player-safe path   -> FrozenPlayerSafeAnchor
omniscient path    -> ExactTrainingLabels
                        \\
                         -> TrainingSample
```

compositionはplayer-safe anchor valueを変更しない。`TrainingSample`は既に
freezeされたanchorをそのまま保持するだけであり、label attachmentの前後で
anchorは同一valueである。

optional targetがunavailableであることを理由にsampleをsilent dropしない。
availabilityはlabel側のmetadataとして保持したまま、sampleは成立する。

## Same-anchor alignment

`TrainingSample`は、2つのpathが **同じstate position** を指していることを
construction時にfail closedで検証する。viewer一致だけでは、同じgame・同じ局・
同じviewerの別state position（別`round_revision`）や、繰り返し局（同じ
`hand_number`で`honba`違い）を取り違えたcompositionを検出できない。

そのため、anchor側のplayer-safe valueから期待される`LabelAnchorIdentity`を
構成し、label builderがprivileged stateから独立に導出した
`labels.anchor_identity`と完全一致することを要求する。labelのidentityは
anchorからのcopyではないため、この検証は自明には成立しない。
"""

from dataclasses import dataclass

from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    wind_from_engine_wind,
)
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    FrozenPlayerSafeAnchor,
)
from lisjong_arena.phase2_training_anchor.training_labels import (
    ExactTrainingLabels,
    LabelAnchorIdentity,
)


def anchor_identity_of(anchor: FrozenPlayerSafeAnchor) -> LabelAnchorIdentity:
    """frozen anchorのplayer-safe valueから、期待されるanchor identityを作る。

    `game_seed`はanchorのsource identity、`dealer_seat` / `prevailing_wind`は
    anchor時点のtrusted `SeatObservation`から取る。どれもplayer-safeな値で
    あり、hidden truthは参照しない。

    label側は同じ値をengine `MatchState`から独立に導出する。両者は別々の
    authorityから来るため、この比較は自明には成立しない。
    """
    if not isinstance(anchor, FrozenPlayerSafeAnchor):
        raise TypeError("anchor must be a FrozenPlayerSafeAnchor")
    return LabelAnchorIdentity(
        game_seed=anchor.source.game_seed,
        hand_number=anchor.hand_number,
        honba=anchor.honba,
        round_revision=anchor.round_revision,
        viewer_seat=seat_from_engine_seat(anchor.viewer_seat),
        dealer_seat=seat_from_engine_seat(anchor.observation.dealer_seat),
        prevailing_wind=wind_from_engine_wind(anchor.observation.prevailing_wind),
    )


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """1 anchor分の、player-safe inputとtraining-only labelsの対応付け。

    2つのpathがsame anchorを指していることをconstruction時にfail closedで
    検証する。state positionの不一致は、pre / post action混同やoff-by-one
    alignment errorの兆候であり、silentに通さない。
    """

    anchor: FrozenPlayerSafeAnchor
    labels: ExactTrainingLabels
    provenance: TrainingPipelineProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, FrozenPlayerSafeAnchor):
            raise TypeError("anchor must be a FrozenPlayerSafeAnchor")
        if not isinstance(self.labels, ExactTrainingLabels):
            raise TypeError("labels must be an ExactTrainingLabels")
        if not isinstance(self.provenance, TrainingPipelineProvenance):
            raise TypeError("provenance must be a TrainingPipelineProvenance")

        expected = anchor_identity_of(self.anchor)
        if self.labels.anchor_identity != expected:
            raise ValueError(
                "player-safe anchor and omniscient labels must describe exactly "
                "the same anchor state position; "
                f"anchor={expected!r} labels={self.labels.anchor_identity!r}"
            )
        if self.provenance.effective_rules != self.anchor.rule_provenance:
            raise ValueError(
                "pipeline provenance and anchor must bind the same effective rules"
            )


def compose_training_sample(
    anchor: FrozenPlayerSafeAnchor,
    labels: ExactTrainingLabels,
    provenance: TrainingPipelineProvenance,
) -> TrainingSample:
    """独立に構成済みのanchorとlabelsを、pipeline provenanceつきでcomposeする。

    この関数はanchorを再構成せず、受け取ったvalueをそのまま保持する。
    """
    return TrainingSample(anchor=anchor, labels=labels, provenance=provenance)
