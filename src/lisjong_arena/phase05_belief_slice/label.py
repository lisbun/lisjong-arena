"""Phase 0.5のomniscient exact expected-count label生成。

label builderだけが`MatchState`のprivileged stateを読み、各TURN anchorの
pre-action snapshotについてself以外の3 windのcurrent concealed handから、

```text
label[wind][tile kind] = そのconcealed handに存在する物理copy数
```

を生成する。normal fiveと赤5は34牌種側で合算し、public meld tilesは
concealed countへ含めない。wait ground truth、furiten、yaku、ron legalityへは
依存しない。kanを含む局面も一律には除外しない。

count labelを安全に生成できないstateだけをreason-coded exclusionとして返し、
countとrateをexperiment側で記録する。
"""

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import (
    TILE_TYPE_COUNT,
    tile_type_index,
    wind_for_seat,
    wind_from_index,
    wind_index,
)
from lisjong.policy_contract import PolicyInput, Wind
from lisjong_engine.match_state import MatchState
from lisjong_engine.public_state import public_tile
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    tile_from_public_tile,
)
from lisjong_arena.phase05_belief_slice.feature import OPPONENT_COUNT

_MAX_COPIES_PER_TILE_TYPE = 4
_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_EQUIVALENT_TILE_COUNT = 3


class Phase05LabelExclusionReason(Enum):
    """exact count labelを安全に生成できなかった具体的理由。"""

    NO_ACTIVE_ROUND = "no_active_round"
    UNSTABLE_OPPONENT_HAND_SIZE = "unstable_opponent_hand_size"
    TILE_COUNT_OUT_OF_RANGE = "tile_count_out_of_range"


@dataclass(frozen=True, slots=True)
class Phase05Labels:
    """1 anchorのviewer-relative exact concealed count labels。"""

    opponent_winds: tuple[Wind, Wind, Wind]
    counts: tuple[tuple[int, ...], ...]
    concealed_sizes: tuple[int, int, int]

    def __post_init__(self) -> None:
        if len(self.opponent_winds) != OPPONENT_COUNT:
            raise ValueError("opponent_winds must contain exactly 3 winds")
        if len(self.counts) != OPPONENT_COUNT:
            raise ValueError("counts must contain exactly 3 rows")
        for row in self.counts:
            if len(row) != TILE_TYPE_COUNT:
                raise ValueError(
                    f"each counts row must contain exactly {TILE_TYPE_COUNT} values"
                )
        if len(self.concealed_sizes) != OPPONENT_COUNT:
            raise ValueError("concealed_sizes must contain exactly 3 values")
        for row, size in zip(self.counts, self.concealed_sizes, strict=True):
            if sum(row) != size:
                raise ValueError("counts row sum must equal the concealed hand size")


@dataclass(frozen=True, slots=True)
class Phase05LabelResult:
    """label生成の成功またはreason-coded exclusion。"""

    labels: Phase05Labels | None
    exclusion_reason: Phase05LabelExclusionReason | None

    def __post_init__(self) -> None:
        if (self.labels is None) == (self.exclusion_reason is None):
            raise ValueError("exactly one of labels / exclusion_reason must be set")


def build_phase05_labels(
    match_state: MatchState,
    policy_input: PolicyInput,
) -> Phase05LabelResult:
    """omniscient stateから1 anchorのexact concealed count labelsを生成する。

    `policy_input`はviewer identity（self seat / dealer seat）とcanonical wind
    axisの解決のためだけに使い、labelそのものは`match_state`から生成する。
    """
    if not isinstance(match_state, MatchState):
        raise TypeError("match_state must be a lisjong-engine MatchState")
    if not isinstance(policy_input, PolicyInput):
        raise TypeError("policy_input must be a PolicyInput")

    round_state = match_state.active_round
    if round_state is None:
        return Phase05LabelResult(
            labels=None,
            exclusion_reason=Phase05LabelExclusionReason.NO_ACTIVE_ROUND,
        )

    dealer_seat = policy_input.round.dealer_seat
    viewer_wind_number = wind_index(wind_for_seat(policy_input.self_seat, dealer_seat))

    rows_by_wind_number: dict[int, tuple[int, ...]] = {}
    sizes_by_wind_number: dict[int, int] = {}
    for engine_seat in EngineSeat:
        seat = seat_from_engine_seat(engine_seat)
        wind_number = wind_index(wind_for_seat(seat, dealer_seat))
        if wind_number == viewer_wind_number:
            continue

        concealed_tiles = round_state.hand_tiles(engine_seat)
        meld_count = len(round_state.melds(engine_seat))
        if (
            len(concealed_tiles) + _MELD_EQUIVALENT_TILE_COUNT * meld_count
            != _STABLE_EQUIVALENT_TILE_COUNT
        ):
            return Phase05LabelResult(
                labels=None,
                exclusion_reason=(
                    Phase05LabelExclusionReason.UNSTABLE_OPPONENT_HAND_SIZE
                ),
            )

        counts = [0] * TILE_TYPE_COUNT
        for engine_tile in concealed_tiles:
            tile = tile_from_public_tile(public_tile(engine_tile))
            counts[tile_type_index(tile.tile_type)] += 1
        if any(count > _MAX_COPIES_PER_TILE_TYPE for count in counts):
            return Phase05LabelResult(
                labels=None,
                exclusion_reason=Phase05LabelExclusionReason.TILE_COUNT_OUT_OF_RANGE,
            )

        rows_by_wind_number[wind_number] = tuple(counts)
        sizes_by_wind_number[wind_number] = len(concealed_tiles)

    ordered_wind_numbers = sorted(rows_by_wind_number)
    return Phase05LabelResult(
        labels=Phase05Labels(
            opponent_winds=tuple(
                wind_from_index(wind_number) for wind_number in ordered_wind_numbers
            ),
            counts=tuple(
                rows_by_wind_number[wind_number] for wind_number in ordered_wind_numbers
            ),
            concealed_sizes=tuple(
                sizes_by_wind_number[wind_number]
                for wind_number in ordered_wind_numbers
            ),
        ),
        exclusion_reason=None,
    )
