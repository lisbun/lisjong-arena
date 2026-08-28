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
"""

from dataclasses import dataclass

from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    FrozenPlayerSafeAnchor,
)
from lisjong_arena.phase2_training_anchor.training_labels import ExactTrainingLabels


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """1 anchor分の、player-safe inputとtraining-only labelsの対応付け。

    2つのpathがsame anchorを指していることをconstruction時にfail closedで
    検証する。viewer identityの不一致は、pre / post action混同やoff-by-one
    alignment errorの兆候であり、silentに通さない。
    """

    anchor: FrozenPlayerSafeAnchor
    labels: ExactTrainingLabels

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, FrozenPlayerSafeAnchor):
            raise TypeError("anchor must be a FrozenPlayerSafeAnchor")
        if not isinstance(self.labels, ExactTrainingLabels):
            raise TypeError("labels must be an ExactTrainingLabels")
        if self.labels.viewer_seat != seat_from_engine_seat(self.anchor.viewer_seat):
            raise ValueError(
                "anchor viewer seat and label viewer seat must identify the "
                "same seat at the same anchor"
            )


def compose_training_sample(
    anchor: FrozenPlayerSafeAnchor,
    labels: ExactTrainingLabels,
) -> TrainingSample:
    """独立に構成済みのanchorとlabelsをcomposeする。

    この関数はanchorを再構成せず、受け取ったvalueをそのまま保持する。
    """
    return TrainingSample(anchor=anchor, labels=labels)
