"""player-safe reconstructionとは別channelのhidden-state truth。

Issue #203のSurface B（server-truth hidden-state supervision）は、player-safe
snapshotが完成したあとで、同じdecision identityへtraining-only / diagnostic
truthとしてjoinする。player-safe側へ書き戻さない。

```text
PlayerSafeDecisionSnapshot (viewer)     <-- player-safe channel
        +
join_hidden_state_truth(seat_states)    <-- training-only channel
        =
同一decision identityの2 channel
```

truthの正本は、全4 seat分の`PlayerSafeRoundState`が保持する「そのseat自身の
手牌」である。あるseatにとってplayer-safeな自手は、他seatから見ればhidden
truthであり、この非対称性をchannel分離の実装根拠にする。viewer自身のstateは
truth rowへ含めない（`OpponentIdentity`は3 opponentだけを表す）。

## semantic boundary

```text
realized hidden hand   != posterior
structural wait        != ron-legal wait != deal-in probability != hand EV
```

このmoduleが導出するのはrealized concealed truthとstructural truthだけである。
posterior、放銃確率、EVは計算しない。structural waitは
`phase2_training_anchor.training_labels.structural_wait_for_hand()`のcurrent
exact semanticsをそのまま再利用し、stable 13-equivalentでないstateを無理に
label化しない。
"""

from dataclasses import dataclass

from lisjong.belief import TILE_TYPE_COUNT, tile_type_index, wind_for_seat
from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.seat import Seat

from lisjong_arena.phase2_training_anchor.training_labels import (
    OPPONENT_COUNT,
    OpponentExpectedCounts,
    OpponentIdentity,
    OpponentStructuralWait,
    expected_counts_for_concealed_hand,
    structural_wait_for_hand,
)
from lisjong_arena.riichilab_downstream_qualification.player_safe import (
    PlayerSafeDecisionSnapshot,
    PlayerSafeRoundState,
)

_SEAT_COUNT = 4
_MAX_COPIES_PER_TILE_KIND = 4
_MELD_STRUCTURAL_EQUIVALENT_COUNT = 3
_STABLE_STRUCTURAL_SIZE = 13
_DRAWN_STRUCTURAL_SIZE = 14


@dataclass(frozen=True, slots=True)
class HiddenOpponentRow:
    """1 opponentのrealized hidden truth。

    `structural_tenpai`は`structural_wait`のmaskからのみ導出する。maskが
    unavailableな場合は`None`であり、「非聴牌」と混同しない。
    """

    expected_counts: OpponentExpectedCounts
    structural_wait: OpponentStructuralWait
    structural_tenpai: bool | None
    meld_count: int
    open_meld_count: int

    @property
    def identity(self) -> OpponentIdentity:
        return self.expected_counts.identity

    @property
    def concealed_size_is_consistent(self) -> bool:
        """meld調整後のconcealed sizeが13 / 14であること。

        chi / pon / いずれの槓もstructuralには3枚相当として数える。
        `training_labels.structural_wait_for_hand()`のstable 13-equivalent
        判定と同じ扱いであり、ankanもhand size調整の対象に含める。
        """
        return (
            self.expected_counts.concealed_size
            + _MELD_STRUCTURAL_EQUIVALENT_COUNT * self.meld_count
        ) in (_STABLE_STRUCTURAL_SIZE, _DRAWN_STRUCTURAL_SIZE)

    @property
    def holds_red_five(self) -> bool:
        return any(self.expected_counts.red_five_present)


@dataclass(frozen=True, slots=True)
class HiddenStateTruth:
    """1 decision pointへjoinされたhidden-state supervision truth。"""

    viewer_seat: Seat
    rows: tuple[HiddenOpponentRow, ...]
    concealed_size_consistent: bool
    tile_conservation_consistent: bool

    def __post_init__(self) -> None:
        if len(self.rows) != OPPONENT_COUNT:
            raise ValueError("hidden truth must contain exactly three opponent rows")
        offsets = tuple(row.identity.viewer_relative_offset for row in self.rows)
        if offsets != (1, 2, 3):
            raise ValueError("opponent rows must be ordered by viewer-relative offset")
        if self.viewer_seat in {row.identity.seat for row in self.rows}:
            raise ValueError("the viewer seat must not appear as a target opponent")

    @property
    def structural_wait_available_count(self) -> int:
        return sum(row.structural_wait.is_available for row in self.rows)

    @property
    def structural_tenpai_count(self) -> int:
        return sum(row.structural_tenpai is True for row in self.rows)


def _tile_kind_counts(
    seat_states: tuple[PlayerSafeRoundState, ...],
) -> list[int]:
    """concealed hand / meld / 未鳴きdiscard / dora indicatorのbase kind別枚数。

    鳴かれたdiscardはmeldへ移動するため二重計上しない（`called_by`が
    設定されたdiscardを除外する）。wallと王牌はserver logに現れないため
    数えない。
    """
    counts = [0] * TILE_TYPE_COUNT
    public = seat_states[0].public_view()
    for state in seat_states:
        for tile in state.own_concealed_tiles:
            counts[tile_type_index(tile.tile_type)] += 1
    for melds in public.melds:
        for meld in melds:
            for tile in meld.tiles:
                counts[tile_type_index(tile.tile_type)] += 1
    for discards in public.discards:
        for discard in discards:
            if discard.called_by is None:
                counts[tile_type_index(discard.tile.tile_type)] += 1
    for indicator in public.dora_indicators:
        counts[tile_type_index(indicator.tile_type)] += 1
    return counts


def tile_conservation_is_consistent(
    seat_states: tuple[PlayerSafeRoundState, ...],
) -> bool:
    """既知位置の同一base kindが物理上限の4枚を超えないこと。"""
    return all(
        count <= _MAX_COPIES_PER_TILE_KIND for count in _tile_kind_counts(seat_states)
    )


def concealed_size_is_consistent(
    seat_states: tuple[PlayerSafeRoundState, ...],
) -> bool:
    """全seatのopen-meld調整後hand sizeが13 / 14で、14が高々1 seatであること。"""
    sizes = [state.structural_hand_size() for state in seat_states]
    if any(
        size not in (_STABLE_STRUCTURAL_SIZE, _DRAWN_STRUCTURAL_SIZE) for size in sizes
    ):
        return False
    return sizes.count(_DRAWN_STRUCTURAL_SIZE) <= 1


def _open_meld_count(melds: tuple[PublicMeld, ...]) -> int:
    return sum(meld.kind is not MeldKind.ANKAN for meld in melds)


def join_hidden_state_truth(
    seat_states: tuple[PlayerSafeRoundState, ...],
    snapshot: PlayerSafeDecisionSnapshot,
) -> HiddenStateTruth:
    """完成済みplayer-safe snapshotへ、同じdecision時点のhidden truthをjoinする。

    `seat_states`はこのdecision時点の4 seat分のstateである。viewer側の
    snapshotは既にfreeze済みであり、この関数はsnapshotを変更しない。
    """
    if not isinstance(snapshot, PlayerSafeDecisionSnapshot):
        raise TypeError("snapshot must be a PlayerSafeDecisionSnapshot")
    if len(seat_states) != _SEAT_COUNT:
        raise ValueError("seat_states must contain exactly four seat states")
    viewer = snapshot.viewer_seat
    if seat_states[int(viewer)].viewer_seat != viewer:
        raise ValueError("seat state ordering does not match the viewer seat")

    rows = []
    for index in range(_SEAT_COUNT):
        seat = Seat(index)
        if seat == viewer:
            continue
        state = seat_states[index]
        identity = OpponentIdentity(
            seat=seat,
            wind=wind_for_seat(seat, snapshot.dealer_seat),
            viewer_relative_offset=(int(seat) - int(viewer)) % _SEAT_COUNT,
        )
        concealed = state.own_concealed_tiles
        melds = state.own_melds
        structural_wait = structural_wait_for_hand(identity, concealed, melds)
        rows.append(
            HiddenOpponentRow(
                expected_counts=expected_counts_for_concealed_hand(identity, concealed),
                structural_wait=structural_wait,
                structural_tenpai=None
                if structural_wait.mask is None
                else any(structural_wait.mask),
                meld_count=len(melds),
                open_meld_count=_open_meld_count(melds),
            )
        )
    rows.sort(key=lambda row: row.identity.viewer_relative_offset)

    return HiddenStateTruth(
        viewer_seat=viewer,
        rows=tuple(rows),
        concealed_size_consistent=concealed_size_is_consistent(seat_states),
        tile_conservation_consistent=tile_conservation_is_consistent(seat_states),
    )


__all__ = [
    "HiddenOpponentRow",
    "HiddenStateTruth",
    "concealed_size_is_consistent",
    "join_hidden_state_truth",
    "tile_conservation_is_consistent",
]
