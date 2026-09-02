"""Fixtures for the Arena-local Learned Policy input schema."""

from dataclasses import replace

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


def tile(category: TileCategory, rank: int, *, red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red=red)


def manzu(rank: int, *, red: bool = False) -> Tile:
    return tile(TileCategory.MANZU, rank, red=red)


def pinzu(rank: int, *, red: bool = False) -> Tile:
    return tile(TileCategory.PINZU, rank, red=red)


def souzu(rank: int, *, red: bool = False) -> Tile:
    return tile(TileCategory.SOUZU, rank, red=red)


def honor(rank: int) -> Tile:
    return tile(TileCategory.HONOR, rank)


def player(
    *,
    score: int = 25_000,
    discards: tuple[Discard, ...] = (),
    melds: tuple[PublicMeld, ...] = (),
    riichi: RiichiState = RiichiState.NONE,
) -> PlayerPublicState:
    return PlayerPublicState(score, discards, melds, riichi)


def minimal_policy_input(
    *,
    self_seat: Seat = Seat.SEAT_0,
    players: tuple[PlayerPublicState, ...] | None = None,
    own_tiles: tuple[Tile, ...] = (),
    drawn_tile: Tile | None = None,
    dora_indicators: tuple[Tile, ...] = (),
    live_wall_tiles_remaining: int = 70,
) -> PolicyInput:
    return PolicyInput(
        self_seat=self_seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=dora_indicators,
            live_wall_tiles_remaining=live_wall_tiles_remaining,
        ),
        players=players or tuple(player() for _ in range(4)),
        own_hand=OwnHandState(own_tiles, drawn_tile),
    )


def complex_policy_input() -> PolicyInput:
    chi = PublicMeld(
        MeldKind.CHI,
        (manzu(3), manzu(1), manzu(2)),
        Seat.SEAT_3,
        manzu(1),
    )
    pon = PublicMeld(
        MeldKind.PON,
        (pinzu(5), pinzu(5, red=True), pinzu(5)),
        Seat.SEAT_2,
        pinzu(5, red=True),
    )
    daiminkan = PublicMeld(
        MeldKind.DAIMINKAN,
        (souzu(7), souzu(7), souzu(7), souzu(7)),
        Seat.SEAT_1,
        souzu(7),
    )
    ankan = PublicMeld(
        MeldKind.ANKAN,
        (honor(5), honor(5), honor(5), honor(5)),
        None,
        None,
    )
    kakan = PublicMeld(
        MeldKind.KAKAN,
        (manzu(5), manzu(5), manzu(5, red=True), manzu(5)),
        Seat.SEAT_0,
        manzu(5, red=True),
    )
    players = (
        player(
            score=-1_000,
            discards=(Discard(manzu(9), False, 0, None),),
            melds=(chi, pon, daiminkan, ankan),
            riichi=RiichiState.NONE,
        ),
        player(
            score=31_000,
            discards=(Discard(pinzu(1), True, 1, Seat.SEAT_2),),
            melds=(kakan,),
            riichi=RiichiState.DECLARED,
        ),
        player(
            score=40_000,
            discards=(Discard(souzu(5, red=True), True, 2, None),),
            riichi=RiichiState.ACCEPTED,
        ),
        player(
            score=29_000,
            discards=(Discard(honor(7), False, 3, None),),
            riichi=RiichiState.NONE,
        ),
    )
    own_tiles = (
        manzu(1),
        manzu(1),
        manzu(2),
        manzu(3),
        manzu(4),
        manzu(5, red=True),
        pinzu(2),
        pinzu(3),
        pinzu(4),
        souzu(1),
        souzu(2),
        souzu(3),
        honor(1),
        honor(7),
    )
    return PolicyInput(
        self_seat=Seat.SEAT_0,
        round=RoundState(
            round_wind=Wind.WEST,
            hand_number=4,
            dealer_seat=Seat.SEAT_1,
            honba=3,
            riichi_sticks=2,
            dora_indicators=(pinzu(5, red=True), honor(6)),
            live_wall_tiles_remaining=42,
        ),
        players=players,
        own_hand=OwnHandState(own_tiles, manzu(5, red=True)),
    )


def rotate_policy_input(value: PolicyInput, offset: int) -> PolicyInput:
    def rotate_seat(seat: Seat) -> Seat:
        return Seat((int(seat) + offset) % 4)

    rotated_players: list[PlayerPublicState | None] = [None] * 4
    for absolute_index, public in enumerate(value.players):
        old_seat = Seat(absolute_index)
        new_seat = rotate_seat(old_seat)
        discards = tuple(
            replace(
                discard,
                called_by=(
                    None
                    if discard.called_by is None
                    else rotate_seat(discard.called_by)
                ),
            )
            for discard in public.discards
        )
        melds = tuple(
            replace(
                meld,
                from_seat=(
                    None if meld.from_seat is None else rotate_seat(meld.from_seat)
                ),
            )
            for meld in public.melds
        )
        rotated_players[int(new_seat)] = replace(
            public,
            discards=discards,
            melds=melds,
        )
    return replace(
        value,
        self_seat=rotate_seat(value.self_seat),
        round=replace(
            value.round,
            dealer_seat=rotate_seat(value.round.dealer_seat),
        ),
        players=tuple(rotated_players),
    )


def discard_population(count: int) -> tuple[PlayerPublicState, ...]:
    by_seat: list[list[Discard]] = [[] for _ in range(4)]
    for order in range(count):
        seat = order % 4
        by_seat[seat].append(Discard(manzu((order % 9) + 1), False, order, None))
    return tuple(player(discards=tuple(values)) for values in by_seat)
