"""engine public domain valueからlisjong契約valueへの変換contract。"""

import unittest

from _lisjong_engine_fixtures import honor, manzu, pinzu, pon_meld, public_tile, souzu
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
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import TileCategory as EngineTileCategory
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena.lisjong_engine.domain_conversion import (
    _lookup,
    meld_kind_from_engine_meld_type,
    public_meld_from_engine_meld,
    riichi_state_from_engine_status,
    seat_from_engine_seat,
    tile_from_public_tile,
    tiles_from_public_tiles,
    wind_from_engine_wind,
)
from lisjong_arena.lisjong_engine.errors import UnsupportedEngineValueError


class SeatConversionTest(unittest.TestCase):
    def test_maps_each_engine_seat_to_its_lisjong_seat(self) -> None:
        self.assertEqual(seat_from_engine_seat(EngineSeat.EAST), Seat.SEAT_0)
        self.assertEqual(seat_from_engine_seat(EngineSeat.SOUTH), Seat.SEAT_1)
        self.assertEqual(seat_from_engine_seat(EngineSeat.WEST), Seat.SEAT_2)
        self.assertEqual(seat_from_engine_seat(EngineSeat.NORTH), Seat.SEAT_3)

    def test_covers_every_engine_seat(self) -> None:
        converted = {seat_from_engine_seat(seat) for seat in EngineSeat}
        self.assertEqual(converted, set(Seat))

    def test_the_seat_mapping_is_a_bijection(self) -> None:
        converted = [seat_from_engine_seat(seat) for seat in EngineSeat]
        self.assertEqual(len(set(converted)), len(converted))

    def test_shimocha_relation_is_preserved(self) -> None:
        """engineの`Seat.next()`(下家)がlisjongの(seat + 1) mod 4と一致する。"""
        for seat in EngineSeat:
            self.assertEqual(
                seat_from_engine_seat(seat.next()),
                Seat((int(seat_from_engine_seat(seat)) + 1) % 4),
            )

    def test_rejects_a_lisjong_seat_as_an_engine_seat(self) -> None:
        with self.assertRaises(TypeError):
            seat_from_engine_seat(Seat.SEAT_0)

    def test_rejects_a_bare_int(self) -> None:
        with self.assertRaises(TypeError):
            seat_from_engine_seat(0)


class WindConversionTest(unittest.TestCase):
    def test_maps_each_engine_wind(self) -> None:
        self.assertIs(wind_from_engine_wind(EngineWind.EAST), Wind.EAST)
        self.assertIs(wind_from_engine_wind(EngineWind.SOUTH), Wind.SOUTH)
        self.assertIs(wind_from_engine_wind(EngineWind.WEST), Wind.WEST)
        self.assertIs(wind_from_engine_wind(EngineWind.NORTH), Wind.NORTH)

    def test_rejects_an_engine_seat_with_the_same_value(self) -> None:
        with self.assertRaises(TypeError):
            wind_from_engine_wind(EngineSeat.EAST)


class TileConversionTest(unittest.TestCase):
    def test_maps_each_tile_category(self) -> None:
        expected = {
            EngineTileCategory.MANZU: TileCategory.MANZU,
            EngineTileCategory.PINZU: TileCategory.PINZU,
            EngineTileCategory.SOUZU: TileCategory.SOUZU,
            EngineTileCategory.HONOR: TileCategory.HONOR,
        }
        self.assertEqual(set(expected), set(EngineTileCategory))
        for engine_category, category in expected.items():
            rank = 7 if engine_category is EngineTileCategory.HONOR else 9
            converted = tile_from_public_tile(public_tile(engine_category, rank))
            self.assertEqual(converted, Tile(TileType(category, rank), False))

    def test_preserves_red_distinction(self) -> None:
        self.assertEqual(
            tile_from_public_tile(souzu(5, is_red=True)),
            Tile(TileType(TileCategory.SOUZU, 5), True),
        )
        self.assertEqual(
            tile_from_public_tile(souzu(5)),
            Tile(TileType(TileCategory.SOUZU, 5), False),
        )

    def test_honor_ranks_keep_the_shared_wind_and_dragon_meaning(self) -> None:
        """engineとlisjongで字牌rankの意味(1..4=東南西北、5..7=白發中)が一致する。"""
        for rank in range(1, 8):
            self.assertEqual(
                tile_from_public_tile(honor(rank)),
                Tile(TileType(TileCategory.HONOR, rank), False),
            )

    def test_preserves_sequence_order(self) -> None:
        tiles = (pinzu(9), manzu(1), souzu(5, is_red=True))
        self.assertEqual(
            tiles_from_public_tiles(tiles),
            tuple(tile_from_public_tile(tile) for tile in tiles),
        )

    def test_rejects_a_lisjong_tile(self) -> None:
        with self.assertRaises(TypeError):
            tile_from_public_tile(Tile(TileType(TileCategory.MANZU, 1), False))


class MeldKindConversionTest(unittest.TestCase):
    def test_maps_every_engine_meld_type(self) -> None:
        expected = {
            PublicMeldType.CHI: MeldKind.CHI,
            PublicMeldType.PON: MeldKind.PON,
            PublicMeldType.DAIMINKAN: MeldKind.DAIMINKAN,
            PublicMeldType.ANKAN: MeldKind.ANKAN,
            PublicMeldType.KAKAN: MeldKind.KAKAN,
        }
        self.assertEqual(set(expected), set(PublicMeldType))
        for engine_meld_type, kind in expected.items():
            self.assertIs(meld_kind_from_engine_meld_type(engine_meld_type), kind)

    def test_rejects_a_lisjong_meld_kind(self) -> None:
        with self.assertRaises(TypeError):
            meld_kind_from_engine_meld_type(MeldKind.PON)


class RiichiStateConversionTest(unittest.TestCase):
    def test_maps_every_engine_status(self) -> None:
        expected = {
            PublicRiichiStatus.NONE: RiichiState.NONE,
            PublicRiichiStatus.PENDING: RiichiState.DECLARED,
            PublicRiichiStatus.ESTABLISHED: RiichiState.ACCEPTED,
        }
        self.assertEqual(set(expected), set(PublicRiichiStatus))
        for status, state in expected.items():
            self.assertIs(riichi_state_from_engine_status(status), state)

    def test_pending_is_a_semantic_conversion_not_a_name_match(self) -> None:
        self.assertIs(
            riichi_state_from_engine_status(PublicRiichiStatus.PENDING),
            RiichiState.DECLARED,
        )

    def test_rejects_a_lisjong_riichi_state(self) -> None:
        with self.assertRaises(TypeError):
            riichi_state_from_engine_status(RiichiState.DECLARED)


class PublicMeldConversionTest(unittest.TestCase):
    def test_converts_chi(self) -> None:
        meld = EnginePublicMeld(
            meld_type=PublicMeldType.CHI,
            tiles=(souzu(4), souzu(5, is_red=True), souzu(6)),
            from_seat=EngineSeat.NORTH,
            called_tile=souzu(5, is_red=True),
        )
        converted = public_meld_from_engine_meld(meld)
        self.assertEqual(
            converted,
            PublicMeld(
                kind=MeldKind.CHI,
                tiles=(
                    Tile(TileType(TileCategory.SOUZU, 4), False),
                    Tile(TileType(TileCategory.SOUZU, 5), True),
                    Tile(TileType(TileCategory.SOUZU, 6), False),
                ),
                from_seat=Seat.SEAT_3,
                called_tile=Tile(TileType(TileCategory.SOUZU, 5), True),
            ),
        )

    def test_converts_pon(self) -> None:
        converted = public_meld_from_engine_meld(pon_meld(manzu(3), EngineSeat.SOUTH))
        self.assertIs(converted.kind, MeldKind.PON)
        self.assertIs(converted.from_seat, Seat.SEAT_1)
        self.assertEqual(
            converted.called_tile, Tile(TileType(TileCategory.MANZU, 3), False)
        )

    def test_converts_daiminkan(self) -> None:
        meld = EnginePublicMeld(
            meld_type=PublicMeldType.DAIMINKAN,
            tiles=(pinzu(7),) * 4,
            from_seat=EngineSeat.WEST,
            called_tile=pinzu(7),
        )
        converted = public_meld_from_engine_meld(meld)
        self.assertIs(converted.kind, MeldKind.DAIMINKAN)
        self.assertIs(converted.from_seat, Seat.SEAT_2)
        self.assertEqual(len(converted.tiles), 4)

    def test_converts_ankan_without_call_provenance(self) -> None:
        meld = EnginePublicMeld(
            meld_type=PublicMeldType.ANKAN,
            tiles=(honor(1),) * 4,
            from_seat=None,
            called_tile=None,
        )
        converted = public_meld_from_engine_meld(meld)
        self.assertIs(converted.kind, MeldKind.ANKAN)
        self.assertIsNone(converted.from_seat)
        self.assertIsNone(converted.called_tile)

    def test_converts_kakan_and_keeps_the_original_pon_provenance(self) -> None:
        meld = EnginePublicMeld(
            meld_type=PublicMeldType.KAKAN,
            tiles=(pinzu(5), pinzu(5), pinzu(5), pinzu(5, is_red=True)),
            from_seat=EngineSeat.NORTH,
            called_tile=pinzu(5),
        )
        converted = public_meld_from_engine_meld(meld)
        self.assertIs(converted.kind, MeldKind.KAKAN)
        self.assertIs(converted.from_seat, Seat.SEAT_3)
        self.assertEqual(
            converted.called_tile, Tile(TileType(TileCategory.PINZU, 5), False)
        )

    def test_rejects_a_lisjong_public_meld(self) -> None:
        with self.assertRaises(TypeError):
            public_meld_from_engine_meld(
                PublicMeld(
                    kind=MeldKind.ANKAN,
                    tiles=(Tile(TileType(TileCategory.MANZU, 1), False),) * 4,
                    from_seat=None,
                    called_tile=None,
                )
            )


class UnsupportedEngineValueTest(unittest.TestCase):
    def test_value_missing_from_the_table_fails_closed_instead_of_defaulting(
        self,
    ) -> None:
        """実在しないenum memberは作れないため、不完全な対応表で固定する。"""
        with self.assertRaises(UnsupportedEngineValueError):
            _lookup({EngineSeat.EAST: Seat.SEAT_0}, EngineSeat.SOUTH, "Seat")

    def test_unhashable_value_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedEngineValueError):
            _lookup({EngineSeat.EAST: Seat.SEAT_0}, ["east"], "Seat")


if __name__ == "__main__":
    unittest.main()
