"""#472: RuleSetのfieldにない、engine実装が固定しているルールのfixture。

`docs/heuristic-candidate-engine-aabb-half.md` §6.3 の U2（赤牌の構成）と
U3（喰いタン・後付け）を、lisjong-engine `91af75e` で確認する。期待値は
engine `docs/rules.md` と通常の赤ドラ・喰いタンありルールから先に書いた値である。

- U2: 1局の牌山は標準136枚の並べ替えで、赤5は萬子・筒子・索子に1枚ずつ
- U3: 鳴いた断么九でロンできる（喰いタン、1翻）。役の有無は和了牌で判定し、
  役牌の後付けは和了でき、同じ待ちでも役のない和了牌ではロンできない

開発用seed `472000`〜`472006` だけを使う。
"""

import unittest

from lisjong_engine.dora import DoraIndicators
from lisjong_engine.meld import Pon
from lisjong_engine.riichi_event import RiichiStatus
from lisjong_engine.ron_legality import can_declare_ron
from lisjong_engine.round_allocation import (
    create_round_random_provenance,
    create_round_wall,
)
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat
from lisjong_engine.tile import STANDARD_TILES, TileCategory
from lisjong_engine.win_context import WinMethod, WinningContext, WinOrigin
from lisjong_engine.wind import Wind
from lisjong_engine.winning_score import evaluate_winning_scores
from lisjong_engine.yaku import Yaku

_RULES = RuleSet.default()
_DEVELOPMENT_SEEDS = range(472_000, 472_007)
_ROUND_ORDINALS = (1, 2, 3)

_MAN, _PIN, _SOU, _HONOR = 0, 9, 18, 27
_HAKU = _HONOR + 4


def _tile(base: int, rank: int, copy: int = 1):
    """`copy` 0の5は赤なので、通常牌はcopy 1以上を使う。"""
    return STANDARD_TILES[(base + rank - 1) * 4 + copy]


def _key(tile) -> tuple[int, bool]:
    return tile.id, tile.is_red


class RedFiveCompositionTest(unittest.TestCase):
    """U2: 赤牌の構成。"""

    def test_standard_tile_set_has_one_red_five_per_suit(self) -> None:
        self.assertEqual(len(STANDARD_TILES), 136)
        self.assertEqual(len({tile.id for tile in STANDARD_TILES}), 136)
        reds = sorted(
            (tile.tile_type.category.value, tile.tile_type.rank)
            for tile in STANDARD_TILES
            if tile.is_red
        )
        self.assertEqual(
            reds,
            sorted(
                [
                    (TileCategory.MANZU.value, 5),
                    (TileCategory.PINZU.value, 5),
                    (TileCategory.SOUZU.value, 5),
                ]
            ),
        )

    def test_every_round_wall_is_a_permutation_with_three_red_fives(self) -> None:
        expected = sorted(_key(tile) for tile in STANDARD_TILES)
        for seed in _DEVELOPMENT_SEEDS:
            for ordinal in _ROUND_ORDINALS:
                with self.subTest(seed=seed, ordinal=ordinal):
                    wall = create_round_wall(
                        create_round_random_provenance(seed, ordinal)
                    )
                    tiles = wall.remaining_tiles + wall.dead_wall_tiles
                    self.assertEqual(sorted(_key(tile) for tile in tiles), expected)
                    self.assertEqual(
                        sorted(
                            tile.tile_type.category.value
                            for tile in tiles
                            if tile.is_red
                        ),
                        sorted(
                            category.value
                            for category in (
                                TileCategory.MANZU,
                                TileCategory.PINZU,
                                TileCategory.SOUZU,
                            )
                        ),
                    )


class OpenTanyaoAndAtozukeTest(unittest.TestCase):
    """U3: 喰いタンと後付け。和了者はSOUTH（子）、東場、立直なし。"""

    _PON_2P = Pon(
        called_tile=_tile(_PIN, 2, 0),
        consumed_tiles=(_tile(_PIN, 2, 1), _tile(_PIN, 2, 2)),
        source_seat=Seat.EAST,
    )

    def _can_ron(self, concealed, winning_tile) -> bool:
        return can_declare_ron(
            concealed_tiles=(*concealed, winning_tile),
            winning_tile=winning_tile,
            melds=(self._PON_2P,),
            seat_wind=Wind.SOUTH,
            prevailing_wind=Wind.EAST,
            riichi_status=RiichiStatus.NONE,
            is_ippatsu=False,
            is_furiten=False,
            origin=WinOrigin.DISCARD,
            is_last_tile=False,
            rules=_RULES,
        )

    def _yaku(self, concealed, winning_tile, method: WinMethod) -> set:
        context = WinningContext(
            concealed_tiles=(*concealed, winning_tile),
            winning_tile=winning_tile,
            method=method,
            origin=WinOrigin.DISCARD
            if method is WinMethod.RON
            else WinOrigin.LIVE_WALL,
            seat_wind=Wind.SOUTH,
            prevailing_wind=Wind.EAST,
            declared_melds=(self._PON_2P,),
        )
        selection = evaluate_winning_scores(
            context, dora_indicators=DoraIndicators(), rules=_RULES
        )
        hands = {
            frozenset(
                match.yaku for match in candidate.hand_value.yaku_evaluation.matches
            )
            for candidate in selection.max_score_candidates
        }
        self.assertEqual(len(hands), 1)
        han = {candidate.score.han for candidate in selection.max_score_candidates}
        return set(hands.pop()), han

    # 234m 678m 34s 88p + pon 222p、和了牌 5s（赤でない）: すべて中張牌
    _TANYAO_CONCEALED = (
        _tile(_MAN, 2),
        _tile(_MAN, 3),
        _tile(_MAN, 4),
        _tile(_MAN, 6),
        _tile(_MAN, 7),
        _tile(_MAN, 8),
        _tile(_SOU, 3),
        _tile(_SOU, 4),
        _tile(_PIN, 8, 0),
        _tile(_PIN, 8, 1),
    )

    def test_open_tanyao_can_ron_for_one_han(self) -> None:
        winning = _tile(_SOU, 5, 1)
        self.assertTrue(self._can_ron(self._TANYAO_CONCEALED, winning))
        yaku, han = self._yaku(self._TANYAO_CONCEALED, winning, WinMethod.RON)
        self.assertEqual(yaku, {Yaku.TANYAO})
        self.assertEqual(han, {1})

    def test_open_tanyao_tsumo_has_no_menzen_tsumo(self) -> None:
        yaku, han = self._yaku(
            self._TANYAO_CONCEALED, _tile(_SOU, 5, 1), WinMethod.TSUMO
        )
        self.assertEqual(yaku, {Yaku.TANYAO})
        self.assertEqual(han, {1})

    # 234m 678m 88p 白白 + pon 222p: 8p / 白 のシャンポン待ち
    _SHANPON_CONCEALED = (
        _tile(_MAN, 2),
        _tile(_MAN, 3),
        _tile(_MAN, 4),
        _tile(_MAN, 6),
        _tile(_MAN, 7),
        _tile(_MAN, 8),
        _tile(_PIN, 8, 0),
        _tile(_PIN, 8, 1),
        STANDARD_TILES[_HAKU * 4 + 0],
        STANDARD_TILES[_HAKU * 4 + 1],
    )

    def test_atozuke_yakuhai_can_ron(self) -> None:
        winning = STANDARD_TILES[_HAKU * 4 + 2]
        self.assertTrue(self._can_ron(self._SHANPON_CONCEALED, winning))
        yaku, han = self._yaku(self._SHANPON_CONCEALED, winning, WinMethod.RON)
        self.assertEqual(yaku, {Yaku.WHITE_DRAGON})
        self.assertEqual(han, {1})

    def test_same_wait_without_yaku_cannot_ron(self) -> None:
        self.assertFalse(self._can_ron(self._SHANPON_CONCEALED, _tile(_PIN, 8, 2)))


if __name__ == "__main__":
    unittest.main()
