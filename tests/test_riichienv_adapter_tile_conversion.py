import unittest

from lisjong.policy_contract.tile import Tile, TileCategory, TileType

from lisjong_arena.riichienv.adapter.tile_conversion import (
    tile_from_mjai,
    tile_from_physical_id,
)


class TileFromPhysicalIdTest(unittest.TestCase):
    def test_manzu_ids_map_to_expected_ranks(self) -> None:
        self.assertEqual(
            tile_from_physical_id(0), Tile(TileType(TileCategory.MANZU, 1))
        )
        self.assertEqual(
            tile_from_physical_id(15), Tile(TileType(TileCategory.MANZU, 4))
        )
        self.assertEqual(
            tile_from_physical_id(35), Tile(TileType(TileCategory.MANZU, 9))
        )

    def test_pinzu_and_souzu_blocks_follow_manzu(self) -> None:
        self.assertEqual(
            tile_from_physical_id(36), Tile(TileType(TileCategory.PINZU, 1))
        )
        self.assertEqual(
            tile_from_physical_id(71), Tile(TileType(TileCategory.PINZU, 9))
        )
        self.assertEqual(
            tile_from_physical_id(72), Tile(TileType(TileCategory.SOUZU, 1))
        )
        self.assertEqual(
            tile_from_physical_id(107), Tile(TileType(TileCategory.SOUZU, 9))
        )

    def test_honor_ids_follow_east_south_west_north_white_green_red_order(
        self,
    ) -> None:
        expected_ranks = {
            108: 1,
            112: 2,
            116: 3,
            120: 4,
            124: 5,
            128: 6,
            132: 7,
        }
        for tile_id, rank in expected_ranks.items():
            self.assertEqual(
                tile_from_physical_id(tile_id),
                Tile(TileType(TileCategory.HONOR, rank)),
            )

    def test_only_the_first_copy_of_each_suited_five_is_red(self) -> None:
        # RiichiEnv 0.4.8実測(lisbun/lisjong `docs/riichienv-investigation.md`)どおり、
        # rank5のcopy index 0だけが赤牌である。
        self.assertTrue(tile_from_physical_id(16).is_red)
        self.assertFalse(tile_from_physical_id(17).is_red)
        self.assertFalse(tile_from_physical_id(18).is_red)
        self.assertFalse(tile_from_physical_id(19).is_red)
        self.assertTrue(tile_from_physical_id(52).is_red)
        self.assertTrue(tile_from_physical_id(88).is_red)

    def test_honor_tiles_are_never_red(self) -> None:
        self.assertFalse(tile_from_physical_id(108).is_red)

    def test_rejects_out_of_range_id(self) -> None:
        with self.assertRaises(ValueError):
            tile_from_physical_id(-1)
        with self.assertRaises(ValueError):
            tile_from_physical_id(136)

    def test_rejects_non_int(self) -> None:
        with self.assertRaises(ValueError):
            tile_from_physical_id("12")


class TileFromMjaiTest(unittest.TestCase):
    def test_parses_numbered_tiles(self) -> None:
        self.assertEqual(tile_from_mjai("1p"), Tile(TileType(TileCategory.PINZU, 1)))
        self.assertEqual(tile_from_mjai("9s"), Tile(TileType(TileCategory.SOUZU, 9)))

    def test_parses_red_fives(self) -> None:
        red_5m = tile_from_mjai("5mr")
        self.assertEqual(red_5m.tile_type, TileType(TileCategory.MANZU, 5))
        self.assertTrue(red_5m.is_red)
        self.assertTrue(tile_from_mjai("5pr").is_red)
        self.assertTrue(tile_from_mjai("5sr").is_red)

    def test_normal_five_is_not_red(self) -> None:
        self.assertFalse(tile_from_mjai("5m").is_red)

    def test_parses_honor_tiles_using_mjai_letters(self) -> None:
        self.assertEqual(tile_from_mjai("E"), Tile(TileType(TileCategory.HONOR, 1)))
        self.assertEqual(tile_from_mjai("C"), Tile(TileType(TileCategory.HONOR, 7)))

    def test_rejects_masked_tile(self) -> None:
        with self.assertRaises(ValueError):
            tile_from_mjai("?")

    def test_rejects_unrecognized_string(self) -> None:
        for value in ("0m", "10m", "1x", "", "5mrr", "z"):
            with self.assertRaises(ValueError):
                tile_from_mjai(value)

    def test_rejects_non_str(self) -> None:
        with self.assertRaises(TypeError):
            tile_from_mjai(5)


if __name__ == "__main__":
    unittest.main()
