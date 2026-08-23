"""`seat_from_player_index`のpublic contract regression。

`lisjong_arena.riichienv.adapter`のpublic surfaceであり、Arena
consumer(`LocalGameRunner` / `RiichiLabSeatAdapter`等)から直接利用される。
`tests/test_riichienv_adapter_action_mapping.py`の
`SeatFromPlayerIndexTest`が基本的なvalid/invalid caseを固定しているが、
`bool`が`int`のsubclassであることに起因する誤受理を明示的に固定する
専用testがなかったため、ここで直接regressionを追加する。
"""

import unittest

from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichienv.adapter.seat_conversion import seat_from_player_index


class SeatFromPlayerIndexTest(unittest.TestCase):
    def test_valid_indices_map_to_corresponding_seat(self) -> None:
        for index in range(4):
            self.assertEqual(seat_from_player_index(index), Seat(index))

    def test_rejects_bool_despite_being_an_int_subclass(self) -> None:
        with self.assertRaises(TypeError):
            seat_from_player_index(True)
        with self.assertRaises(TypeError):
            seat_from_player_index(False)

    def test_rejects_non_int(self) -> None:
        with self.assertRaises(TypeError):
            seat_from_player_index("0")
        with self.assertRaises(TypeError):
            seat_from_player_index(None)

    def test_rejects_out_of_range_int(self) -> None:
        with self.assertRaises(ValueError):
            seat_from_player_index(4)
        with self.assertRaises(ValueError):
            seat_from_player_index(-1)


if __name__ == "__main__":
    unittest.main()
