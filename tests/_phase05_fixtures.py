"""lisjong-project #22 Phase 0.5 test用のexperiment-local fixture builders。"""

from lisjong.belief import tile_type_from_index
from lisjong.policy_contract import (
    Discard,
    MeldKind,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    PublicMeld,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.phase05_belief_slice.feature import (
    OpponentDiscardBucket,
    Phase05AnchorFeatures,
    Phase05Feature,
    TurnBucket,
)
from lisjong_arena.phase05_belief_slice.label import Phase05Labels
from lisjong_arena.phase05_belief_slice.sample import Phase05Partition, Phase05Sample

TILE_TYPE_COUNT = 34


def tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red=is_red)


def manzu(rank: int, *, is_red: bool = False) -> Tile:
    return tile(TileCategory.MANZU, rank, is_red=is_red)


def discard(target: Tile, order: int) -> Discard:
    return Discard(tile=target, tsumogiri=False, order=order, called_by=None)


def pon(target: Tile, *, from_seat: Seat) -> PublicMeld:
    return PublicMeld(
        kind=MeldKind.PON,
        tiles=(target, target, target),
        from_seat=from_seat,
        called_tile=target,
    )


def player(
    *,
    discards: tuple[Discard, ...] = (),
    melds: tuple[PublicMeld, ...] = (),
    riichi: RiichiState = RiichiState.NONE,
) -> PlayerPublicState:
    return PlayerPublicState(
        score=25000,
        discards=discards,
        melds=melds,
        riichi=riichi,
    )


def policy_input(
    *,
    self_seat: Seat = Seat.SEAT_0,
    dealer_seat: Seat = Seat.SEAT_0,
    players: tuple[PlayerPublicState, ...] | None = None,
    own_tiles: tuple[Tile, ...] = (),
    dora_indicators: tuple[Tile, ...] = (),
    live_wall_tiles_remaining: int = 70,
) -> PolicyInput:
    return PolicyInput(
        self_seat=self_seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=dealer_seat,
            honba=0,
            riichi_sticks=0,
            dora_indicators=dora_indicators,
            live_wall_tiles_remaining=live_wall_tiles_remaining,
        ),
        players=players or tuple(player() for _ in range(4)),
        own_hand=OwnHandState(concealed_tiles=own_tiles, drawn_tile=None),
    )


def anchor_features(
    *,
    viewer_wind: Wind = Wind.EAST,
    opponent_winds: tuple[Wind, Wind, Wind] = (Wind.SOUTH, Wind.WEST, Wind.NORTH),
    opponent_meld_counts: tuple[int, int, int] = (0, 0, 0),
    remaining_tile_counts: tuple[int, ...] | None = None,
    feature_factory=None,
) -> Phase05AnchorFeatures:
    remaining = remaining_tile_counts or (4,) * TILE_TYPE_COUNT

    def default_feature(opponent_wind: Wind, tile_index: int) -> Phase05Feature:
        offset = opponent_winds.index(opponent_wind)
        return Phase05Feature(
            viewer_wind=viewer_wind,
            opponent_wind=opponent_wind,
            tile_type=tile_type_from_index(tile_index),
            remaining_tile_count=remaining[tile_index],
            opponent_meld_count=opponent_meld_counts[offset],
            opponent_riichi_state=RiichiState.NONE,
            turn_bucket=TurnBucket.EARLY,
            opponent_discard_bucket=OpponentDiscardBucket.NONE,
        )

    build = feature_factory or default_feature
    return Phase05AnchorFeatures(
        viewer_wind=viewer_wind,
        opponent_winds=opponent_winds,
        opponent_meld_counts=opponent_meld_counts,
        remaining_tile_counts=remaining,
        features=tuple(
            build(opponent_wind, tile_index)
            for opponent_wind in opponent_winds
            for tile_index in range(TILE_TYPE_COUNT)
        ),
    )


def labels(
    counts: tuple[tuple[int, ...], ...],
    *,
    opponent_winds: tuple[Wind, Wind, Wind] = (Wind.SOUTH, Wind.WEST, Wind.NORTH),
) -> Phase05Labels:
    return Phase05Labels(
        opponent_winds=opponent_winds,
        counts=counts,
        concealed_sizes=tuple(sum(row) for row in counts),
    )


def row(*, thirteen_of: int = 0) -> tuple[int, ...]:
    """指定tile indexへ4枚まで、残りを次のindexへ配る合計13枚のlabel row。"""
    values = [0] * TILE_TYPE_COUNT
    remaining = 13
    index = thirteen_of
    while remaining > 0:
        take = min(4, remaining)
        values[index % TILE_TYPE_COUNT] = take
        remaining -= take
        index += 1
    return tuple(values)


def sample(
    *,
    seed: int = 100,
    partition: Phase05Partition = Phase05Partition.TRAIN,
    anchor_index: int = 0,
    features: Phase05AnchorFeatures | None = None,
    label_counts: tuple[tuple[int, ...], ...] | None = None,
    baseline: tuple[tuple[float, ...], ...] | None = None,
) -> Phase05Sample:
    resolved_features = features or anchor_features()
    resolved_counts = label_counts or (row(), row(thirteen_of=4), row(thirteen_of=8))
    return Phase05Sample(
        seed=seed,
        partition=partition,
        anchor_index=anchor_index,
        features=resolved_features,
        labels=labels(
            resolved_counts,
            opponent_winds=resolved_features.opponent_winds,
        ),
        baseline_expected_counts=baseline
        or tuple((0.0,) * TILE_TYPE_COUNT for _ in range(3)),
    )
