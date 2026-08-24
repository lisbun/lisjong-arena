"""`lisjong-engine`のpublic domain valueからlisjong契約valueへの明示的変換。

engineとlisjongはどちらも4人麻雀のdomain値を持つが、別repositoryが所有する
別契約である。enum value文字列やint値が偶然一致することへ依存せず、対応表を
明示する。未知のvalueは`UnsupportedEngineValueError`でfail closedし、`None`や
先頭候補へ丸めない。

変換方向はengine -> lisjongの一方向である。selector compositionはengine `Seat`を
そのままkeyとして扱うため、逆方向の復元は現時点で必要ない。
"""

from lisjong.policy_contract import (
    MeldKind,
    PublicMeld,
    RiichiState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)
from lisjong_engine.public_state import PublicMeld as EnginePublicMeld
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus, PublicTile
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import TileCategory as EngineTileCategory
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena.engine.errors import UnsupportedEngineValueError

# engineのSeatは`east`/`south`/`west`/`north`のstr enumであり、lisjongのSeatは
# 0..3のIntEnumである。両者にint値の共有はないため、席順の対応を明示する。
_SEAT_BY_ENGINE_SEAT = {
    EngineSeat.EAST: Seat.SEAT_0,
    EngineSeat.SOUTH: Seat.SEAT_1,
    EngineSeat.WEST: Seat.SEAT_2,
    EngineSeat.NORTH: Seat.SEAT_3,
}

_WIND_BY_ENGINE_WIND = {
    EngineWind.EAST: Wind.EAST,
    EngineWind.SOUTH: Wind.SOUTH,
    EngineWind.WEST: Wind.WEST,
    EngineWind.NORTH: Wind.NORTH,
}

_TILE_CATEGORY_BY_ENGINE_TILE_CATEGORY = {
    EngineTileCategory.MANZU: TileCategory.MANZU,
    EngineTileCategory.PINZU: TileCategory.PINZU,
    EngineTileCategory.SOUZU: TileCategory.SOUZU,
    EngineTileCategory.HONOR: TileCategory.HONOR,
}

_MELD_KIND_BY_ENGINE_MELD_TYPE = {
    PublicMeldType.CHI: MeldKind.CHI,
    PublicMeldType.PON: MeldKind.PON,
    PublicMeldType.DAIMINKAN: MeldKind.DAIMINKAN,
    PublicMeldType.ANKAN: MeldKind.ANKAN,
    PublicMeldType.KAKAN: MeldKind.KAKAN,
}

# engineの`PENDING`とlisjongの`DECLARED`は名称が一致しないが、どちらも
# 「立直を宣言済みで、まだ成立していない」段階を表す。名称一致ではなく
# semantic conversionとして固定する。
_RIICHI_STATE_BY_ENGINE_STATUS = {
    PublicRiichiStatus.NONE: RiichiState.NONE,
    PublicRiichiStatus.PENDING: RiichiState.DECLARED,
    PublicRiichiStatus.ESTABLISHED: RiichiState.ACCEPTED,
}

# 字牌rankの意味（1..4=東南西北、5..7=白發中）は、engine側の
# `yaku_evaluation._wind_tile_type()` / `_honor_tile_type()`と
# lisjong側の`EAST_WIND`..`RED_DRAGON`で一致することを確認済みである。
# rankはcategory変換後にそのまま引き継ぐ。


def _lookup(table: dict, value: object, field_name: str):
    try:
        converted = table[value]
    except KeyError, TypeError:
        raise UnsupportedEngineValueError(
            f"unsupported lisjong-engine {field_name}: {value!r}"
        ) from None
    return converted


def seat_from_engine_seat(seat: object) -> Seat:
    """engine `Seat`をlisjong `Seat`へ変換する。"""
    if not isinstance(seat, EngineSeat):
        raise TypeError("seat must be a lisjong-engine Seat")
    return _lookup(_SEAT_BY_ENGINE_SEAT, seat, "Seat")


def wind_from_engine_wind(wind: object) -> Wind:
    """engine `Wind`をlisjong `Wind`へ変換する。"""
    if not isinstance(wind, EngineWind):
        raise TypeError("wind must be a lisjong-engine Wind")
    return _lookup(_WIND_BY_ENGINE_WIND, wind, "Wind")


def tile_from_public_tile(tile: object) -> Tile:
    """engine `PublicTile`をlisjong `Tile`へ変換する。

    engineの`PublicTile`は既にphysical copy identityを持たないため、
    `TileCategory`の対応付けと赤牌区分の維持だけを行う。
    """
    if not isinstance(tile, PublicTile):
        raise TypeError("tile must be a lisjong-engine PublicTile")
    category = _lookup(
        _TILE_CATEGORY_BY_ENGINE_TILE_CATEGORY,
        tile.tile_type.category,
        "TileCategory",
    )
    return Tile(TileType(category, tile.tile_type.rank), tile.is_red)


def tiles_from_public_tiles(tiles: object) -> tuple[Tile, ...]:
    """engine `PublicTile`のsequenceを、順序を変えずlisjong `Tile`へ変換する。"""
    try:
        values = tuple(tiles)
    except TypeError:
        raise TypeError("tiles must be an iterable of PublicTile") from None
    return tuple(tile_from_public_tile(tile) for tile in values)


def meld_kind_from_engine_meld_type(meld_type: object) -> MeldKind:
    """engine `PublicMeldType`をlisjong `MeldKind`へ変換する。"""
    if not isinstance(meld_type, PublicMeldType):
        raise TypeError("meld_type must be a lisjong-engine PublicMeldType")
    return _lookup(_MELD_KIND_BY_ENGINE_MELD_TYPE, meld_type, "PublicMeldType")


def riichi_state_from_engine_status(status: object) -> RiichiState:
    """engine `PublicRiichiStatus`をlisjong `RiichiState`へ変換する。"""
    if not isinstance(status, PublicRiichiStatus):
        raise TypeError("status must be a lisjong-engine PublicRiichiStatus")
    return _lookup(_RIICHI_STATE_BY_ENGINE_STATUS, status, "PublicRiichiStatus")


def public_meld_from_engine_meld(meld: object) -> PublicMeld:
    """engine `PublicMeld`をlisjong `PublicMeld`へ変換する。

    ANKANのみ`from_seat` / `called_tile`が`None`である。KAKANは元Ponの
    `from_seat` / `called_tile`をengine側がそのまま保持しているため、
    Arena側で再解決せずそのまま引き継ぐ。
    """
    if not isinstance(meld, EnginePublicMeld):
        raise TypeError("meld must be a lisjong-engine PublicMeld")
    return PublicMeld(
        kind=meld_kind_from_engine_meld_type(meld.meld_type),
        tiles=tiles_from_public_tiles(meld.tiles),
        from_seat=(
            None if meld.from_seat is None else seat_from_engine_seat(meld.from_seat)
        ),
        called_tile=(
            None
            if meld.called_tile is None
            else tile_from_public_tile(meld.called_tile)
        ),
    )
