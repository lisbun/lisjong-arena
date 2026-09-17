"""Stage A0 non-riichi structural-Tenpai targetのavailability分類とlabel導出。

Tenpai semanticsはArena側へ再実装しない。canonical authorityは常に

```text
lisjong.belief.exact_wait_ground_truth.exact_hand_belief_with_waits()
```

であり、Stage A0 targetはそのstructural wait maskの存在量化だけである。

```text
W[j, t] = canonical exact structural-completion-wait
T[j]    = OR_t W[j, t]
```

furiten / yaku / remaining live copies / ron legality / hand value /
future action / future drawは含めない。

`lisjong_arena.phase2_training_anchor.structural_wait`が確立した

```text
OpponentIdentity                (相対offset + absolute seat + seat windのbinding)
structural_wait_for_hand()      (stable 13-equivalentだけをlabel化する境界)
```

はbackendに依存しないvalue levelのcontractなので、そのまま薄く再利用する。
`training_labels`が読むlisjong-engine execution seamはStage A0では使わない。
Stage A0はその上に、#258が要求するtarget availability reason code contractだけを
追加する。unavailableは決してnegative labelへ丸めない。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import (
    BASE_TILE_COUNT_MAX,
    SCALE,
    STANDARD_RED_FIVE_COUNTS,
    TILE_TYPE_COUNT,
    red_five_index,
    tile_type_index,
    wind_for_seat,
)
from lisjong.policy_contract import Seat
from lisjong.policy_contract.meld import PublicMeld
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.tile import Tile

from lisjong_arena.phase2_training_anchor.structural_wait import (
    OpponentIdentity,
    StructuralWaitUnavailableReason,
    structural_wait_for_hand,
)

from .errors import StageA0LabelError

_MELD_STRUCTURAL_EQUIVALENT_COUNT = 3
_STABLE_EQUIVALENT_TILE_COUNT = 13


class TargetAvailability(Enum):
    """1 opponent-target cellのexactly one classification。

    `AVAILABLE`以外はすべてmaskedであり、`T = 0`のnegative labelではない。
    """

    AVAILABLE = "AVAILABLE"
    RIICHI_EXCLUDED = "RIICHI_EXCLUDED"
    NOT_STABLE_13_EQUIVALENT = "NOT_STABLE_13_EQUIVALENT"
    HIDDEN_HAND_UNAVAILABLE = "HIDDEN_HAND_UNAVAILABLE"
    MELD_STATE_UNAVAILABLE = "MELD_STATE_UNAVAILABLE"
    SEAT_MAPPING_UNRESOLVED = "SEAT_MAPPING_UNRESOLVED"
    INVALID_PHYSICAL_INVENTORY = "INVALID_PHYSICAL_INVENTORY"
    OTHER_FAIL_CLOSED = "OTHER_FAIL_CLOSED"

    @property
    def is_available(self) -> bool:
        return self is TargetAvailability.AVAILABLE


UNAVAILABLE_REASONS = tuple(
    availability
    for availability in TargetAvailability
    if availability is not TargetAvailability.AVAILABLE
)

CLASSIFICATION_ORDER = (
    TargetAvailability.SEAT_MAPPING_UNRESOLVED,
    TargetAvailability.RIICHI_EXCLUDED,
    TargetAvailability.HIDDEN_HAND_UNAVAILABLE,
    TargetAvailability.MELD_STATE_UNAVAILABLE,
    TargetAvailability.INVALID_PHYSICAL_INVENTORY,
    TargetAvailability.NOT_STABLE_13_EQUIVALENT,
    TargetAvailability.OTHER_FAIL_CLOSED,
    TargetAvailability.AVAILABLE,
)
"""classificationの評価順。1 cellは必ず最初に一致した1つだけを持つ。"""


@dataclass(frozen=True, slots=True)
class OpponentTenpaiTarget:
    """1 opponent-target cellのStage A0 target、またはそのunavailable reason。

    `wait_mask`は34 base tile kindのbinary multi-labelであり、非聴牌の
    all-zero maskはvalid labelである（`availability == AVAILABLE`かつ
    `tenpai == 0`）。`availability != AVAILABLE`のcellは`wait_mask is None`
    かつ`tenpai is None`であり、`tenpai = 0`へは決して丸めない。
    """

    identity: OpponentIdentity
    availability: TargetAvailability
    wait_mask: tuple[int, ...] | None
    tenpai: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, OpponentIdentity):
            raise TypeError("identity must be an OpponentIdentity")
        if not isinstance(self.availability, TargetAvailability):
            raise TypeError("availability must be a TargetAvailability")
        if self.availability.is_available:
            if self.wait_mask is None or self.tenpai is None:
                raise StageA0LabelError(
                    "an AVAILABLE target must carry both a wait mask and T"
                )
            if len(self.wait_mask) != TILE_TYPE_COUNT:
                raise StageA0LabelError(
                    f"wait mask must contain exactly {TILE_TYPE_COUNT} values"
                )
            if any(value not in (0, 1) for value in self.wait_mask):
                raise StageA0LabelError("wait mask values must be exactly 0 or 1")
            expected = 1 if any(self.wait_mask) else 0
            if self.tenpai != expected:
                raise StageA0LabelError(
                    "T must be the existential OR of the canonical wait mask"
                )
            return
        if self.wait_mask is not None or self.tenpai is not None:
            raise StageA0LabelError(
                "an unavailable target must never carry a label; unavailable "
                "is not a negative label"
            )


def opponent_identity(
    actor_seat: Seat, relative_offset: int, dealer_seat: Seat
) -> OpponentIdentity:
    """viewer相対offsetからabsolute seatとseat windを明示的にbindingする。

    absolute seatをrow位置やclass名から暗黙導出せず、
    `(actor + offset) % 4`というcanonical relationだけを正本にする。
    """
    if not isinstance(actor_seat, Seat) or not isinstance(dealer_seat, Seat):
        raise TypeError("actor_seat and dealer_seat must be lisjong Seat values")
    if relative_offset not in (1, 2, 3):
        raise ValueError("relative_offset must be 1, 2 or 3")
    target_seat = Seat((int(actor_seat) + relative_offset) % 4)
    return OpponentIdentity(
        seat=target_seat,
        wind=wind_for_seat(target_seat, dealer_seat),
        viewer_relative_offset=relative_offset,
    )


def violates_physical_inventory(
    concealed_tiles: tuple[Tile, ...], melds: tuple[PublicMeld, ...]
) -> bool:
    """canonical physical tile inventoryを超えるstateかどうかを返す。

    上限値そのもの（基本牌種ごと4枚、色ごとの赤5は1枚）はlisjongの
    `lisjong.belief.tile_inventory`が正本であり、ここではその公開constantを
    参照するだけである。この判定はTenpai semanticsではなく、#258が要求する
    `INVALID_PHYSICAL_INVENTORY`というdistinct reason codeを、canonical
    builderのfail-closed理由から取り違えずに分類するためだけに存在する。
    canonical builder自身も同じ不変条件を独立に検証しており、両者の一致は
    testで固定する。
    """
    base_counts = [0] * TILE_TYPE_COUNT
    red_counts = [0] * len(STANDARD_RED_FIVE_COUNTS)
    for tile in (*concealed_tiles, *(tile for meld in melds for tile in meld.tiles)):
        base_counts[tile_type_index(tile.tile_type)] += 1
        if tile.is_red:
            red_counts[red_five_index(tile.tile_type.category)] += 1
    if any(count > BASE_TILE_COUNT_MAX for count in base_counts):
        return True
    return any(
        count > limit for count, limit in zip(red_counts, STANDARD_RED_FIVE_COUNTS)
    )


def is_stable_thirteen_equivalent(
    concealed_tiles: tuple[Tile, ...], melds: tuple[PublicMeld, ...]
) -> bool:
    """`len(concealed) + 3 * len(melds) == 13`のstable stateかどうかを返す。"""
    structural = len(concealed_tiles) + _MELD_STRUCTURAL_EQUIVALENT_COUNT * len(melds)
    return structural == _STABLE_EQUIVALENT_TILE_COUNT


def build_opponent_target(
    identity: OpponentIdentity,
    *,
    seat_resolved: bool,
    public_riichi: RiichiState | None,
    privileged_riichi_declared: bool | None,
    concealed_tiles: tuple[Tile, ...] | None,
    melds: tuple[PublicMeld, ...] | None,
) -> OpponentTenpaiTarget:
    """1 opponent-target cellをexactly one classificationへ解決する。

    評価順は`CLASSIFICATION_ORDER`で固定する。riichi exclusionはStage A0の
    target eligibility条件そのものなので、privileged handを読むより先に判定
    する。structural / physical prechecksを通過したあとのcanonical builder
    failureはunexpectedであり、silentにmaskせず`OTHER_FAIL_CLOSED`として
    countableな形で残す（qualificationはこのcountが0でなければ通さない）。
    """
    if not isinstance(identity, OpponentIdentity):
        raise TypeError("identity must be an OpponentIdentity")

    def unavailable(availability: TargetAvailability) -> OpponentTenpaiTarget:
        return OpponentTenpaiTarget(
            identity=identity,
            availability=availability,
            wait_mask=None,
            tenpai=None,
        )

    if not seat_resolved:
        return unavailable(TargetAvailability.SEAT_MAPPING_UNRESOLVED)
    if public_riichi is None or privileged_riichi_declared is None:
        return unavailable(TargetAvailability.SEAT_MAPPING_UNRESOLVED)
    if not isinstance(public_riichi, RiichiState):
        raise TypeError("public_riichi must be a RiichiState or None")
    if public_riichi is not RiichiState.NONE or privileged_riichi_declared:
        return unavailable(TargetAvailability.RIICHI_EXCLUDED)
    if concealed_tiles is None:
        return unavailable(TargetAvailability.HIDDEN_HAND_UNAVAILABLE)
    if melds is None:
        return unavailable(TargetAvailability.MELD_STATE_UNAVAILABLE)
    if violates_physical_inventory(concealed_tiles, melds):
        return unavailable(TargetAvailability.INVALID_PHYSICAL_INVENTORY)
    if not is_stable_thirteen_equivalent(concealed_tiles, melds):
        return unavailable(TargetAvailability.NOT_STABLE_13_EQUIVALENT)

    try:
        structural = structural_wait_for_hand(identity, concealed_tiles, melds)
    except ValueError:
        return unavailable(TargetAvailability.OTHER_FAIL_CLOSED)

    if structural.unavailable_reason is not None:
        if (
            structural.unavailable_reason
            is StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE
        ):
            return unavailable(TargetAvailability.NOT_STABLE_13_EQUIVALENT)
        return unavailable(TargetAvailability.OTHER_FAIL_CLOSED)

    mask = structural.mask
    assert mask is not None
    return OpponentTenpaiTarget(
        identity=identity,
        availability=TargetAvailability.AVAILABLE,
        wait_mask=mask,
        tenpai=1 if any(mask) else 0,
    )


__all__ = [
    "CLASSIFICATION_ORDER",
    "SCALE",
    "UNAVAILABLE_REASONS",
    "OpponentIdentity",
    "OpponentTenpaiTarget",
    "TargetAvailability",
    "build_opponent_target",
    "is_stable_thirteen_equivalent",
    "opponent_identity",
    "violates_physical_inventory",
]
