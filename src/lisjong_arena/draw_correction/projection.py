"""#596の`pre_draw`から、lisjong #391のallowlist型（p用・V用）を作る射影。

- 入力は`read_live_draw_capture()`が検査済みで返す`LiveDrawRecord`だけ
- p用: 全席の手牌・副露・**鳴かれていない**河・公開表示牌を37カテゴリの枚数へ。
  鳴かれた河の牌（`called_by`あり）は鳴いた席の副露として数え、河からは除く
- V用: drawer席（`pre_draw.current_seat`）とdrawerの純手牌
- 物理牌ID（0..135）はカテゴリへ畳み込み、copy IDは渡さない。赤五は
  engineの`STANDARD_TILES[id].is_red`で判定し、34 / 35 / 36（赤5m / 5p / 5s）へ
- `identity`・`realized`・post digestはlisjongへ渡さない。実現牌のカテゴリは
  lookup用として別の値で返す
"""

from dataclasses import dataclass

from lisjong.draw_correction import (
    CATEGORY_COUNT,
    DrawProbabilityProjection,
    ShantenValueProjection,
)
from lisjong_engine.tile import STANDARD_TILES, TileCategory

from lisjong_arena.lisjong_engine.live_draw_store import LiveDrawRecord

_RED_CATEGORY = {
    TileCategory.MANZU: 34,
    TileCategory.PINZU: 35,
    TileCategory.SOUZU: 36,
}


class DrawProjectionError(ValueError):
    """射影の入力が契約を満たさない場合。"""


def tile_category(tile_id: int) -> int:
    """物理牌ID（0..135）を37カテゴリへ。赤五は34..36、それ以外は34牌種index。"""
    if type(tile_id) is not int or not 0 <= tile_id < len(STANDARD_TILES):
        raise DrawProjectionError("tile id must be an int in 0..135")
    tile = STANDARD_TILES[tile_id]
    if tile.is_red:
        return _RED_CATEGORY[tile.tile_type.category]
    return tile.tile_type.id


def _counts(tile_ids) -> list[int]:
    counts = [0] * CATEGORY_COUNT
    for tile_id in tile_ids:
        counts[tile_category(tile_id)] += 1
    return counts


@dataclass(frozen=True)
class ProjectedDraw:
    """1通常ツモの射影。identityは持たない（joinの鍵は呼び出し側が別に持つ）。"""

    probability: DrawProbabilityProjection
    value: ShantenValueProjection
    realized_category: int


def project_pre_draw(
    pre_draw: dict,
) -> tuple[DrawProbabilityProjection, ShantenValueProjection]:
    """`pre_draw`だけからp用・V用の射影を作る。"""
    checkpoint = pre_draw["checkpoint"]
    seats = checkpoint["seats"]
    drawer = pre_draw["current_seat"]
    probability = DrawProbabilityProjection.from_mapping(
        {
            "hand_counts": [_counts(seat["hand_tiles"]) for seat in seats],
            "meld_counts": [
                _counts(tile for meld in seat["melds"] for tile in meld["tiles"])
                for seat in seats
            ],
            "uncalled_discard_counts": [
                _counts(
                    discard["tile"]
                    for discard in seat["discards"]
                    if discard["called_by"] is None
                )
                for seat in seats
            ],
            "revealed_dora_indicator_counts": _counts(
                checkpoint["revealed_dora_indicators"]
            ),
            "live_tiles_remaining": checkpoint["live_tiles_remaining"],
        }
    )
    value = ShantenValueProjection.from_mapping(
        {
            "drawer_seat": drawer,
            "drawer_hand_counts": _counts(seats[drawer]["hand_tiles"]),
        }
    )
    return probability, value


def project_record(record: LiveDrawRecord) -> ProjectedDraw:
    """検査済みの1行から、p用・V用の射影と実現カテゴリを作る。"""
    if not isinstance(record, LiveDrawRecord):
        raise DrawProjectionError("record must be a LiveDrawRecord")
    probability, value = project_pre_draw(record.pre_draw)
    return ProjectedDraw(
        probability=probability,
        value=value,
        realized_category=tile_category(record.realized["tile"]),
    )


__all__ = [
    "DrawProjectionError",
    "ProjectedDraw",
    "project_pre_draw",
    "project_record",
    "tile_category",
]
