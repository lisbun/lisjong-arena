"""Phase 0.5のexperiment-local seat-safe feature表現とencoder。

encoderは`PolicyInput`だけをargumentに取り、`MatchState`等のomniscient
objectを受け取らない。これはIssue #22のleakage check 1（feature encoder type
boundary）をtype levelで満たすためであり、canonical production feature schema
を定義するものではない。

feature cellは1 anchorにつき、

```text
3 opponents (canonical wind order, viewer wind除外)
    x 34 base tile kinds
```

の102個であり、own rowはprediction対象外なので生成しない。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import (
    TILE_TYPE_COUNT,
    derive_remaining_tile_inventory,
    tile_type_from_index,
    tile_type_index,
    wind_for_seat,
    wind_from_index,
    wind_index,
)
from lisjong.policy_contract import (
    PolicyInput,
    RiichiState,
    Seat,
    TileType,
    Wind,
)

OPPONENT_COUNT = 3
"""viewer以外のwind数。own rowはprediction / loss / metric対象外。"""

_MAX_COPIES_PER_TILE_TYPE = 4
_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_EQUIVALENT_TILE_COUNT = 3
_EARLY_TURN_MAX_DISCARDS = 23
_MIDDLE_TURN_MAX_DISCARDS = 47

_CANONICAL_TILE_TYPES: tuple[TileType, ...] = tuple(
    tile_type_from_index(index) for index in range(TILE_TYPE_COUNT)
)


class TurnBucket(Enum):
    """round-global public discard countのcoarse bucket。"""

    EARLY = "early"
    MIDDLE = "middle"
    LATE = "late"


class OpponentDiscardBucket(Enum):
    """対象opponentが同じbase tile kindを過去に捨てた枚数のcoarse bucket。"""

    NONE = "0"
    ONE = "1"
    MANY = "2+"


@dataclass(frozen=True, slots=True)
class Phase05Feature:
    """1 (opponent, tile kind) predictionのexperiment-local feature。

    すべてcurrent `PolicyInput`から導出したseat-visible valueである。
    `viewer_wind`はsample identityとreplay検証のために保持するが、Issue #22で
    lockしたbackoff hierarchyのkeyには含まれない。
    """

    viewer_wind: Wind
    opponent_wind: Wind
    tile_type: TileType
    remaining_tile_count: int
    opponent_meld_count: int
    opponent_riichi_state: RiichiState
    turn_bucket: TurnBucket
    opponent_discard_bucket: OpponentDiscardBucket

    def __post_init__(self) -> None:
        if not isinstance(self.viewer_wind, Wind):
            raise TypeError("viewer_wind must be a Wind")
        if not isinstance(self.opponent_wind, Wind):
            raise TypeError("opponent_wind must be a Wind")
        if self.viewer_wind is self.opponent_wind:
            raise ValueError("opponent_wind must differ from viewer_wind")
        if not isinstance(self.tile_type, TileType):
            raise TypeError("tile_type must be a TileType")
        if type(self.remaining_tile_count) is not int:
            raise TypeError("remaining_tile_count must be an int")
        if not 0 <= self.remaining_tile_count <= _MAX_COPIES_PER_TILE_TYPE:
            raise ValueError("remaining_tile_count must be within 0..4")
        if type(self.opponent_meld_count) is not int:
            raise TypeError("opponent_meld_count must be an int")
        if not 0 <= self.opponent_meld_count <= 4:
            raise ValueError("opponent_meld_count must be within 0..4")
        if not isinstance(self.opponent_riichi_state, RiichiState):
            raise TypeError("opponent_riichi_state must be a RiichiState")
        if not isinstance(self.turn_bucket, TurnBucket):
            raise TypeError("turn_bucket must be a TurnBucket")
        if not isinstance(self.opponent_discard_bucket, OpponentDiscardBucket):
            raise TypeError("opponent_discard_bucket must be an OpponentDiscardBucket")


@dataclass(frozen=True, slots=True)
class Phase05AnchorFeatures:
    """1 TURN anchorのviewer-relative feature set。

    `features`は`opponent_winds`のorderをmajor、canonical tile type indexを
    minorとしたlength 102のflattened tableである。
    """

    viewer_wind: Wind
    opponent_winds: tuple[Wind, Wind, Wind]
    opponent_meld_counts: tuple[int, int, int]
    remaining_tile_counts: tuple[int, ...]
    features: tuple[Phase05Feature, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.viewer_wind, Wind):
            raise TypeError("viewer_wind must be a Wind")
        if len(self.opponent_winds) != OPPONENT_COUNT:
            raise ValueError("opponent_winds must contain exactly 3 winds")
        if len(set(self.opponent_winds)) != OPPONENT_COUNT:
            raise ValueError("opponent_winds must be distinct")
        if self.viewer_wind in self.opponent_winds:
            raise ValueError("opponent_winds must not contain viewer_wind")
        if len(self.opponent_meld_counts) != OPPONENT_COUNT:
            raise ValueError("opponent_meld_counts must contain exactly 3 values")
        if len(self.remaining_tile_counts) != TILE_TYPE_COUNT:
            raise ValueError(
                f"remaining_tile_counts must contain exactly {TILE_TYPE_COUNT} values"
            )
        if len(self.features) != OPPONENT_COUNT * TILE_TYPE_COUNT:
            raise ValueError(
                "features must contain exactly "
                f"{OPPONENT_COUNT * TILE_TYPE_COUNT} values"
            )

    def feature(self, opponent_offset: int, tile_index: int) -> Phase05Feature:
        """`opponent_winds[opponent_offset]`と`tile_index`のfeatureを返す。"""
        return self.features[opponent_offset * TILE_TYPE_COUNT + tile_index]


def _turn_bucket(policy_input: PolicyInput) -> TurnBucket:
    discard_count = sum(len(player.discards) for player in policy_input.players)
    if discard_count <= _EARLY_TURN_MAX_DISCARDS:
        return TurnBucket.EARLY
    if discard_count <= _MIDDLE_TURN_MAX_DISCARDS:
        return TurnBucket.MIDDLE
    return TurnBucket.LATE


def _opponent_discard_bucket(count: int) -> OpponentDiscardBucket:
    if count == 0:
        return OpponentDiscardBucket.NONE
    if count == 1:
        return OpponentDiscardBucket.ONE
    return OpponentDiscardBucket.MANY


def _seat_discard_counts(policy_input: PolicyInput, seat: Seat) -> tuple[int, ...]:
    """`seat`のpublic discard riverにおける34牌種別のdiscard枚数。

    calledされたdiscardも「その席が公開の場へ捨てた」という事実は残るため
    除外しない。これは粗いvertical slice featureであり、formal
    public-evidence schemaではない。
    """
    counts = [0] * TILE_TYPE_COUNT
    for discard in policy_input.players[int(seat)].discards:
        counts[tile_type_index(discard.tile.tile_type)] += 1
    return tuple(counts)


def encode_phase05_anchor_features(
    policy_input: PolicyInput,
) -> Phase05AnchorFeatures:
    """current `PolicyInput`だけから1 anchorのfeature setを生成する。

    `MatchState`等のomniscient objectはargumentとして受け取らない。
    """
    if not isinstance(policy_input, PolicyInput):
        raise TypeError("policy_input must be a PolicyInput")

    dealer_seat = policy_input.round.dealer_seat
    viewer_wind = wind_for_seat(policy_input.self_seat, dealer_seat)
    viewer_wind_number = wind_index(viewer_wind)
    remaining = derive_remaining_tile_inventory(policy_input).remaining_tile_counts
    turn_bucket = _turn_bucket(policy_input)

    seats_by_wind_number: dict[int, Seat] = {}
    for seat in Seat:
        seats_by_wind_number[wind_index(wind_for_seat(seat, dealer_seat))] = seat

    opponent_winds: list[Wind] = []
    opponent_meld_counts: list[int] = []
    features: list[Phase05Feature] = []
    for wind_number in range(4):
        if wind_number == viewer_wind_number:
            continue
        opponent_wind = wind_from_index(wind_number)
        opponent_seat = seats_by_wind_number[wind_number]
        opponent = policy_input.players[int(opponent_seat)]
        meld_count = len(opponent.melds)
        if _STABLE_EQUIVALENT_TILE_COUNT - _MELD_EQUIVALENT_TILE_COUNT * meld_count < 0:
            raise ValueError("public meld count implies negative concealed slots")
        discard_counts = _seat_discard_counts(policy_input, opponent_seat)

        opponent_winds.append(opponent_wind)
        opponent_meld_counts.append(meld_count)
        for tile_index, tile_type in enumerate(_CANONICAL_TILE_TYPES):
            features.append(
                Phase05Feature(
                    viewer_wind=viewer_wind,
                    opponent_wind=opponent_wind,
                    tile_type=tile_type,
                    remaining_tile_count=remaining[tile_index],
                    opponent_meld_count=meld_count,
                    opponent_riichi_state=opponent.riichi,
                    turn_bucket=turn_bucket,
                    opponent_discard_bucket=_opponent_discard_bucket(
                        discard_counts[tile_index]
                    ),
                )
            )

    return Phase05AnchorFeatures(
        viewer_wind=viewer_wind,
        opponent_winds=tuple(opponent_winds),
        opponent_meld_counts=tuple(opponent_meld_counts),
        remaining_tile_counts=remaining,
        features=tuple(features),
    )
