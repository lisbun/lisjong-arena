"""lisjong-arena #493: Mortal decision analysis artifactは宣言牌の位置を黙って落とさない。

このartifactのschemaはリーチ宣言牌の位置を持たず、移行の対象外とする。値を持つ入力は
書き出す前に拒否する。
"""

import unittest
from dataclasses import replace
from unittest import mock

import _mortal_decision_analysis_fixtures as fixtures
from lisjong.policy_contract.seat import Seat
from test_mortal_decision_analysis_artifact import _ArtifactTestCase

from lisjong_arena.mortal_decision_analysis_artifact import (
    MortalDecisionAnalysisArtifactError,
    _policy_input_to_dict,
    save_mortal_decision_analysis,
)

FIELD = "riichi_declaration_discard_order"
_ORIGINAL_POLICY_INPUT = fixtures.policy_input


def _with_declaration(seat):
    """seat 1（リーチ成立済み、order 1の打牌を持つ）に宣言牌の位置を持たせる。"""
    value = _ORIGINAL_POLICY_INPUT(seat)
    players = list(value.players)
    players[1] = replace(players[1], riichi_declaration_discard_order=1)
    return replace(value, players=tuple(players))


class ProjectionTest(unittest.TestCase):
    def test_rejects_an_input_with_a_declaration_order(self) -> None:
        for self_seat in (Seat.SEAT_0, Seat.SEAT_1):
            with self.subTest(self_seat=self_seat):
                with self.assertRaises(MortalDecisionAnalysisArtifactError) as raised:
                    _policy_input_to_dict(_with_declaration(self_seat))
                self.assertIn(f"policy_input.players[1].{FIELD}", str(raised.exception))

    def test_input_without_declaration_orders_is_projected_as_before(self) -> None:
        value = _policy_input_to_dict(_ORIGINAL_POLICY_INPUT(Seat.SEAT_0))

        for player in value["players"]:
            self.assertNotIn(FIELD, player)


class ExportTest(_ArtifactTestCase):
    def test_export_propagates_the_rejection_and_writes_nothing(self) -> None:
        with mock.patch.object(fixtures, "policy_input", side_effect=_with_declaration):
            result = self.result()

        with (
            fixtures.patched_provenance(),
            self.assertRaises(MortalDecisionAnalysisArtifactError),
        ):
            save_mortal_decision_analysis(result, self.directory)

        self.assertFalse(self.directory.exists())
        self.assertEqual(
            sorted(item.name for item in self.workspace.iterdir()), ["mortal.pth"]
        )


if __name__ == "__main__":
    unittest.main()
