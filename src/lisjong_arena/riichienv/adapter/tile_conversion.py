"""RiichiEnv固有の牌表現からlisjong `Tile`への変換。

RiichiEnv 0.4.8は牌を2通りの方法で表現する。

- 物理牌ID(0-135の`int`): `Observation.hand`、`Observation.drawn_tile`、
  `Observation.melds`内`Meld.tiles` / `Meld.called_tile`が使用する
- MJAI牌文字列(例: `"5mr"`、`"1p"`、`"E"`): `Observation.new_events()`が返す
  MJAI event JSON内の`pai`等が使用する

どちらの変換規則も、lisbun/lisjong `docs/riichienv-investigation.md`の実測
(400+ seedにわたる`legal_actions()`のDiscard Action `tile` -> `to_mjai()`比較、
および複数kyokuにわたるdahai/kakan/dora event JSONの直接観測)で確認した
RiichiEnv 0.4.8の実装事実に基づく。未確認のtile表現は推測で変換しない。

字牌のrank割り当て(東=1、南=2、西=3、北=4、白=5、發=6、中=7)は、
`lisjong.policy_contract.tile`がすでにMJAI慣例と一致させているため、
物理ID側(108以降を7種×4枚でE,S,W,N,P,F,Cの順に割り当てる)もこの順序に
合わせるだけでよい。
"""

from lisjong.policy_contract.tile import Tile, TileCategory, TileType

_SUIT_CATEGORIES_BY_BLOCK = (
    TileCategory.MANZU,
    TileCategory.PINZU,
    TileCategory.SOUZU,
)

_MJAI_SUIT_CATEGORIES = {
    "m": TileCategory.MANZU,
    "p": TileCategory.PINZU,
    "s": TileCategory.SOUZU,
}

_MJAI_SUIT_LETTERS_BY_CATEGORY = {
    category: letter for letter, category in _MJAI_SUIT_CATEGORIES.items()
}

_MJAI_HONOR_TYPES = {
    "E": TileType(TileCategory.HONOR, 1),
    "S": TileType(TileCategory.HONOR, 2),
    "W": TileType(TileCategory.HONOR, 3),
    "N": TileType(TileCategory.HONOR, 4),
    "P": TileType(TileCategory.HONOR, 5),
    "F": TileType(TileCategory.HONOR, 6),
    "C": TileType(TileCategory.HONOR, 7),
}

_MJAI_HONOR_LETTERS = {
    tile_type: letter for letter, tile_type in _MJAI_HONOR_TYPES.items()
}


def tile_from_physical_id(tile_id: int) -> Tile:
    """RiichiEnvの物理牌ID(0-135)をlisjong `Tile`へ変換する。

    実測(400+ seedにわたるDiscard Actionの`tile`と`to_mjai()`比較)で確認した
    RiichiEnv 0.4.8のID割り当ては次のとおりである。

    - 0-107: 萬子・筒子・索子。各36 IDが9 rank * 4 copyのblockを成し、
      rank内の4 copyのうちcopy index 0だけが赤牌(赤5のみ)である
    - 108-135: 字牌7種 * 4枚。東南西北白發中の順に並ぶ

    physical copy identity自体(copy index)はlisjong `Tile`へ persist しない。
    """
    if type(tile_id) is not int or not 0 <= tile_id <= 135:
        raise ValueError("tile_id must be an int between 0 and 135")

    if tile_id < 108:
        suit_index, offset = divmod(tile_id, 36)
        rank_index, copy_index = divmod(offset, 4)
        tile_type = TileType(_SUIT_CATEGORIES_BY_BLOCK[suit_index], rank_index + 1)
        is_red = rank_index == 4 and copy_index == 0
        return Tile(tile_type, is_red=is_red)

    honor_index, _copy_index = divmod(tile_id - 108, 4)
    return Tile(TileType(TileCategory.HONOR, honor_index + 1))


def tile_from_mjai(pai: str) -> Tile:
    """RiichiEnvのMJAI牌表記(例: `"5mr"`、`"1p"`、`"E"`)をlisjong `Tile`へ変換する。

    実測したRiichiEnv 0.4.8のevent JSONでは、赤牌は末尾`"r"`付きの
    `"5mr"` / `"5pr"` / `"5sr"`で表現され、`"0m"`等の別表記は出現しなかった。
    `"?"`(他家の非公開牌のmask)を含む未確認・未対応の表記はfail closedで
    `ValueError`にする。
    """
    if type(pai) is not str:
        raise TypeError("pai must be a str")

    honor_type = _MJAI_HONOR_TYPES.get(pai)
    if honor_type is not None:
        return Tile(honor_type)

    is_red = pai.endswith("r")
    body = pai[:-1] if is_red else pai
    if len(body) != 2 or body[1] not in _MJAI_SUIT_CATEGORIES or not body[0].isdigit():
        raise ValueError(f"unrecognized MJAI tile: {pai!r}")

    rank = int(body[0])
    if not 1 <= rank <= 9:
        raise ValueError(f"unrecognized MJAI tile: {pai!r}")

    tile_type = TileType(_MJAI_SUIT_CATEGORIES[body[1]], rank)
    return Tile(tile_type, is_red=is_red)


def tile_to_mjai(tile: Tile) -> str:
    """lisjong `Tile`を、RiichiEnv 0.4.8実測のMJAI牌表記(`tile_from_mjai`の逆)へ変換する。

    `tile_from_mjai`が受理する表記だけを生成する厳密な逆変換であり、
    赤牌は末尾`"r"`付き(`"5mr"`等)、字牌は`"E"`/`"S"`/`"W"`/`"N"`/`"P"`/`"F"`/`"C"`で
    表す。RiichiLab送信用MJAI responseのtile field（`pai`等）を、resolve済みの
    canonical `InternalAction`が持つ牌semantic値から構築するために使う。
    """
    if not isinstance(tile, Tile):
        raise TypeError("tile must be a Tile")

    category = tile.tile_type.category
    if category is TileCategory.HONOR:
        return _MJAI_HONOR_LETTERS[tile.tile_type]

    letter = _MJAI_SUIT_LETTERS_BY_CATEGORY[category]
    suffix = "r" if tile.is_red else ""
    return f"{tile.tile_type.rank}{letter}{suffix}"
