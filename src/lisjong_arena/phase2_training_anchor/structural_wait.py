"""per-opponent exact structural-wait targetのbackend-neutralなvalue contract。

`training_labels`が`lisjong-engine` execution seamから読んだomniscient state
に対して使うのと同じ

```text
OpponentIdentity              相対offset + absolute seat + seat windのbinding
StructuralWaitUnavailableReason
OpponentStructuralWait
structural_wait_for_hand()    stable 13-equivalentだけをlabel化する境界
```

は、`lisjong` value（`Tile` / `PublicMeld`）だけに依存しており、どの
execution backendからprivileged stateを取得したかに依存しない。

`phase4_raw_corpus`と`stage_a0_tenpai_feasibility`が同じsemanticsを再実装
しないよう、engine非依存の部分だけをここへ置く。`training_labels`は引き続き
これらを再exportし、既存importは変わらない。Tenpai / wait semanticsそのものの
正本はArenaではなく
`lisjong.belief.exact_wait_ground_truth.exact_hand_belief_with_waits()`である。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import SCALE, TILE_TYPE_COUNT, exact_hand_belief_with_waits
from lisjong.policy_contract import Seat, Wind

_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_STRUCTURAL_EQUIVALENT_COUNT = 3


class StructuralWaitUnavailableReason(Enum):
    """structural-wait targetだけがgenerateできなかったknown unsupported state。

    expected-count targetやanchor eligibilityへは影響しない。
    """

    UNSTABLE_HAND_SIZE = "unstable_hand_size"
    """`len(concealed) + 3 * len(melds) != 13`のtransient / 14-equivalent state。

    exact wait builderがstable 13-equivalent handだけを受け付けるための、
    このtarget固有のavailability条件である。expected-count targetはこの条件を
    共有しない。
    """


@dataclass(frozen=True, slots=True)
class OpponentIdentity:
    """target opponent rowのlogical identity。

    tensor row indexだけをidentityの正本にしない。viewerからの相対offsetに
    加えてabsolute seatとseat windを明示的にbindingするため、dealer / viewer
    rotationが起きてもrow identityは対応するopponentへ正しく追従する。
    """

    seat: Seat
    wind: Wind
    viewer_relative_offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.seat, Seat):
            raise TypeError("seat must be a lisjong Seat")
        if not isinstance(self.wind, Wind):
            raise TypeError("wind must be a lisjong Wind")
        if self.viewer_relative_offset not in (1, 2, 3):
            raise ValueError("viewer_relative_offset must be 1, 2 or 3")


@dataclass(frozen=True, slots=True)
class OpponentStructuralWait:
    """1 opponentのexact structural wait target、またはそのunavailable reason。

    `mask`は34 base tile kindのbinary multi-labelであり、`1`はその基本牌種を
    加えるとrealized handがstructurallyに完成することを表す。非聴牌の
    all-zero maskはvalid labelであり、`unavailable_reason`が設定された状態
    （maskが`None`）とは明確に区別する。
    """

    identity: OpponentIdentity
    mask: tuple[int, ...] | None
    unavailable_reason: StructuralWaitUnavailableReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, OpponentIdentity):
            raise TypeError("identity must be an OpponentIdentity")
        if (self.mask is None) == (self.unavailable_reason is None):
            raise ValueError("exactly one of mask / unavailable_reason must be set")
        if self.mask is not None:
            if len(self.mask) != TILE_TYPE_COUNT:
                raise ValueError(f"mask must contain exactly {TILE_TYPE_COUNT} values")
            for value in self.mask:
                if value not in (0, 1):
                    raise ValueError("mask values must be exactly 0 or 1")

    @property
    def is_available(self) -> bool:
        return self.mask is not None


def structural_wait_for_hand(
    identity: OpponentIdentity,
    concealed_tiles: tuple,
    own_melds: tuple,
) -> OpponentStructuralWait:
    """stable 13-equivalent handのときだけexact structural wait maskを作る。

    stable条件はこのtarget固有のavailability条件として先に判定し、known
    unsupported stateだけをreason codeにする。条件を満たしたあとの
    `exact_hand_belief_with_waits()`のfailureはunexpectedであり、
    `unavailable`へ丸めずそのまま伝播させる。
    """
    structural_size = len(concealed_tiles) + _MELD_STRUCTURAL_EQUIVALENT_COUNT * len(
        own_melds
    )
    if structural_size != _STABLE_EQUIVALENT_TILE_COUNT:
        return OpponentStructuralWait(
            identity=identity,
            mask=None,
            unavailable_reason=(StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE),
        )

    belief = exact_hand_belief_with_waits(concealed_tiles, own_melds)
    mask = []
    for index in range(TILE_TYPE_COUNT):
        raw = belief.wait_probability_raw[index]
        if raw not in (0, SCALE):
            raise ValueError(
                "exact structural wait ground truth must be binary 0 / SCALE"
            )
        mask.append(1 if raw == SCALE else 0)

    return OpponentStructuralWait(
        identity=identity,
        mask=tuple(mask),
        unavailable_reason=None,
    )


__all__ = [
    "OpponentIdentity",
    "OpponentStructuralWait",
    "StructuralWaitUnavailableReason",
    "structural_wait_for_hand",
]
