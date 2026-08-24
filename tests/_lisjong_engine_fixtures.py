"""first-party engine bridge testで共有する`SeatObservation` fixture helper。

engineのRoundStateを実際に進行させず、`SeatObservation`と
`ActionDescriptor`だけをその場で構築する。Arena bridgeはこの2つを
そのdecisionのsource of truthとして扱うため、boundary testに実対局は不要である。
"""

from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.public_state import (
    PublicDiscard,
    PublicMeld,
    PublicMeldType,
    PublicRiichiStatus,
    PublicTile,
    SeatDiscards,
    SeatMelds,
    SeatRiichiState,
    SeatScore,
)
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import TileCategory as EngineTileCategory
from lisjong_engine.tile import TileType as EngineTileType
from lisjong_engine.wind import Wind as EngineWind


def public_tile(
    category: EngineTileCategory,
    rank: int,
    *,
    is_red: bool = False,
) -> PublicTile:
    return PublicTile(EngineTileType(category, rank), is_red)


def manzu(rank: int, *, is_red: bool = False) -> PublicTile:
    return public_tile(EngineTileCategory.MANZU, rank, is_red=is_red)


def pinzu(rank: int, *, is_red: bool = False) -> PublicTile:
    return public_tile(EngineTileCategory.PINZU, rank, is_red=is_red)


def souzu(rank: int, *, is_red: bool = False) -> PublicTile:
    return public_tile(EngineTileCategory.SOUZU, rank, is_red=is_red)


def honor(rank: int) -> PublicTile:
    return public_tile(EngineTileCategory.HONOR, rank)


def pon_meld(
    tile: PublicTile, from_seat: EngineSeat, *, called_tile=None
) -> PublicMeld:
    """同一tile typeのPon。`called_tile`を指定しない場合は`tile`自身を使う。"""
    resolved_called_tile = tile if called_tile is None else called_tile
    other = PublicTile(tile.tile_type, False)
    return PublicMeld(
        meld_type=PublicMeldType.PON,
        tiles=(resolved_called_tile, other, other),
        from_seat=from_seat,
        called_tile=resolved_called_tile,
    )


def observation(**overrides) -> SeatObservation:
    """最小限の有効な`SeatObservation`を、overrideつきで構築する。"""
    hand_tile = manzu(1)
    values = {
        "viewer_seat": EngineSeat.EAST,
        "decision_kind": ObservationDecisionKind.TURN,
        "hand_number": 1,
        "honba": 0,
        "riichi_sticks": 0,
        "hand_tiles": (hand_tile,),
        "drawn_tile": hand_tile,
        "discards": tuple(SeatDiscards(seat, ()) for seat in EngineSeat),
        "melds": tuple(SeatMelds(seat, ()) for seat in EngineSeat),
        "dora_indicators": (pinzu(3),),
        "remaining_live_wall_count": 70,
        "scores": tuple(SeatScore(seat, 25_000) for seat in EngineSeat),
        "dealer_seat": EngineSeat.EAST,
        "prevailing_wind": EngineWind.EAST,
        "riichi_states": tuple(
            SeatRiichiState(seat, PublicRiichiStatus.NONE) for seat in EngineSeat
        ),
    }
    values.update(overrides)
    return SeatObservation(**values)


def seat_melds(**melds_by_seat) -> tuple[SeatMelds, ...]:
    """seat名(lowercase)をkeyに、指定seatだけmeldを持つseat別tupleを構築する。"""
    return tuple(
        SeatMelds(seat, tuple(melds_by_seat.get(seat.value, ()))) for seat in EngineSeat
    )


def seat_discards(**discards_by_seat) -> tuple[SeatDiscards, ...]:
    """seat名(lowercase)をkeyに、指定seatだけdiscardを持つseat別tupleを構築する。"""
    return tuple(
        SeatDiscards(seat, tuple(discards_by_seat.get(seat.value, ())))
        for seat in EngineSeat
    )


def discard(
    tile: PublicTile,
    order: int,
    *,
    is_tsumogiri: bool = False,
    is_riichi_declaration: bool = False,
    called_by: EngineSeat | None = None,
) -> PublicDiscard:
    return PublicDiscard(
        tile=tile,
        is_tsumogiri=is_tsumogiri,
        order=order,
        is_riichi_declaration=is_riichi_declaration,
        called_by=called_by,
    )
