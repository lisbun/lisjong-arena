"""lisjong#236 stage-1 diagnostic (scripts/diagnose_single_riichi_defense_236.py).

Pins the aggregation scope (exactly one opponent in riichi, self not in riichi,
>= 2 legal discards), the per-branch denominators, payments split by recipient
on a double ron, the all-last top-fold precedence over the parent fallback, the
per-seat-round outcome classification, and the RETIRED-seed guard.  Hanchan
execution is not run here.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind
from lisjong_engine.match_state import AbortiveDrawResult, ExhaustiveDrawResult
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.settlement import TransferReason
from lisjong_engine.win_context import WinMethod

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import diagnose_single_riichi_defense_236 as diagnostic  # noqa: E402

_CATEGORIES = {
    "m": TileCategory.MANZU,
    "p": TileCategory.PINZU,
    "s": TileCategory.SOUZU,
    "z": TileCategory.HONOR,
}


def _hand(spec: str) -> tuple[Tile, ...]:
    tiles, ranks = [], ""
    for character in spec:
        if character.isdigit():
            ranks += character
            continue
        tiles.extend(
            Tile(TileType(_CATEGORIES[character], int(rank))) for rank in ranks
        )
        ranks = ""
    return tuple(tiles)


def _input(concealed: str, *, all_last_top: bool, riichi_discards: str = "3z4z"):
    scores = (40000, 25000, 20000, 15000) if all_last_top else (25000,) * 4

    def player(index):
        riichi = index == 1
        return PlayerPublicState(
            score=scores[index],
            discards=tuple(
                Discard(tile=tile, tsumogiri=False, order=order, called_by=None)
                for order, tile in enumerate(_hand(riichi_discards if riichi else ""))
            ),
            melds=(),
            riichi=RiichiState.ACCEPTED if riichi else RiichiState.NONE,
        )

    tiles = _hand(concealed)
    policy_input = PolicyInput(
        self_seat=Seat.SEAT_0,
        round=RoundState(
            round_wind=Wind.SOUTH if all_last_top else Wind.EAST,
            hand_number=4 if all_last_top else 1,
            dealer_seat=Seat.SEAT_1,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(),
            live_wall_tiles_remaining=50,
        ),
        players=tuple(player(i) for i in range(4)),
        own_hand=OwnHandState(concealed_tiles=tiles, drawn_tile=tiles[-1]),
    )
    discards = tuple(
        DiscardAction(actor=Seat.SEAT_0, tile=tile, tsumogiri=False)
        for tile in dict.fromkeys(tiles)
    )
    return policy_input, discards


# 1-shanten that cannot keep tenpai under riichi, no common genbutsu (riichi
# discards 3z4z are not in hand): the parent reports OTHER_CURRENT_FALLBACK.
ONE_SHANTEN = "34m678p68p234s55s9s7z"


def _row(branch, *, riichi=(1,), own=False, legal=3, paid=0, to_riichi=None):
    return {
        "round": ("EAST", 1, 0),
        "own_riichi": own,
        "riichi_opponents": riichi,
        "legal_discards": legal,
        "branch": branch,
        "dealt_in": paid > 0,
        "deal_in_points": paid,
        "deal_in_points_to_riichi": paid if to_riichi is None else to_riichi,
    }


def _game(rows, episodes=(), rounds=9):
    return {"seed": 931000, "rounds": rounds, "rows": rows, "episodes": list(episodes)}


class AggregateTest(unittest.TestCase):
    def test_scope_excludes_other_states(self):
        rows = [
            _row("PUSH_TENPAI"),
            _row("PUSH_TENPAI", riichi=()),
            _row("PUSH_TENPAI", riichi=(1, 2)),
            _row("PUSH_TENPAI", own=True),
            _row("PUSH_TENPAI", legal=1),
        ]
        summary = diagnostic._aggregate([_game(rows)])
        self.assertEqual(summary["decisions"], 1)
        self.assertEqual(summary["by_branch"]["PUSH_TENPAI"]["decisions"], 1)

    def test_branch_denominators_and_points(self):
        rows = [
            _row("MECHANISM_DEFENSE_FILTERED"),
            _row("MECHANISM_DEFENSE_FILTERED", paid=8000),
            _row("FALLBACK_ALL_LEGAL_1_SHANTEN", paid=2000, to_riichi=0),
            _row("FOLD_COMMON_GENBUTSU"),
        ]
        summary = diagnostic._aggregate([_game(rows, rounds=8)])
        mechanism = summary["by_branch"]["MECHANISM_DEFENSE_FILTERED"]
        self.assertEqual(mechanism["decisions"], 2)
        self.assertEqual(mechanism["deal_ins"], 1)
        self.assertEqual(mechanism["deal_in_rate_per_decision"], 0.5)
        self.assertEqual(mechanism["deal_in_points_share"], 0.8)
        fallback = summary["by_branch"]["FALLBACK_ALL_LEGAL_1_SHANTEN"]
        self.assertEqual(fallback["deal_ins_to_riichi"], 0)
        self.assertEqual(fallback["deal_in_points_to_riichi"], 0)
        self.assertEqual(summary["deal_in_points_total"], 10000)
        self.assertIsNone(
            summary["by_branch"]["NO_ANALYSIS"]["mean_points_per_deal_in"]
        )
        self.assertEqual(summary["rounds"], 8)

    def test_double_ron_counts_only_the_riichi_payment_as_to_riichi(self):
        summary = diagnostic._aggregate(
            [_game([_row("PUSH_TENPAI", paid=10000, to_riichi=8000)])]
        )
        push = summary["by_branch"]["PUSH_TENPAI"]
        self.assertEqual(push["deal_in_points"], 10000)
        self.assertEqual(push["deal_in_points_to_riichi"], 8000)
        self.assertEqual(push["deal_ins_to_riichi"], 1)

    def test_episodes_count_once_per_branch(self):
        episodes = [
            {
                "branches": ["MECHANISM_DEFENSE_FILTERED", "PUSH_TENPAI"],
                "outcome": "WIN",
                "round_delta": 8000,
            },
            {
                "branches": ["MECHANISM_DEFENSE_FILTERED"],
                "outcome": "DRAW_NOTEN",
                "round_delta": -1000,
            },
        ]
        summary = diagnostic._aggregate([_game([], episodes)])
        self.assertEqual(summary["episodes_all"]["episodes"], 2)
        self.assertEqual(summary["episodes_all"]["round_delta_total"], 7000)
        mechanism = summary["episodes_by_branch"]["MECHANISM_DEFENSE_FILTERED"]
        self.assertEqual(mechanism["episodes"], 2)
        self.assertEqual(mechanism["WIN"], 1)
        self.assertEqual(mechanism["DRAW_NOTEN"], 1)
        self.assertEqual(mechanism["mean_round_delta"], 3500)
        self.assertEqual(summary["episodes_by_branch"]["PUSH_TENPAI"]["episodes"], 1)


class TopFoldTest(unittest.TestCase):
    def test_all_last_top_fold_is_detected(self):
        self.assertTrue(
            diagnostic._top_fold_prefiltered(*_input(ONE_SHANTEN, all_last_top=True))
        )

    def test_normal_situation_is_not_top_fold(self):
        self.assertFalse(
            diagnostic._top_fold_prefiltered(*_input(ONE_SHANTEN, all_last_top=False))
        )

    def test_common_genbutsu_disables_top_fold(self):
        self.assertFalse(
            diagnostic._top_fold_prefiltered(
                *_input(ONE_SHANTEN, all_last_top=True, riichi_discards="7z")
            )
        )

    def test_top_fold_takes_precedence_over_the_parent_branch(self):
        self.assertEqual(
            diagnostic._branch_name(None, top_fold=True), "TOP_FOLD_PREFILTERED"
        )
        self.assertEqual(diagnostic._branch_name(None, top_fold=False), "NO_ANALYSIS")


class SettlementTest(unittest.TestCase):
    def _transfer(self, payer, recipient, amount, reason=TransferReason.RON):
        return SimpleNamespace(
            payer=payer, recipient=recipient, amount=amount, reason=reason
        )

    def test_payments_are_split_by_recipient(self):
        completed = SimpleNamespace(
            settlement=SimpleNamespace(
                transfers=(
                    self._transfer(EngineSeat.EAST, EngineSeat.SOUTH, 8000),
                    self._transfer(
                        EngineSeat.EAST, EngineSeat.SOUTH, 300, TransferReason.HONBA
                    ),
                    self._transfer(EngineSeat.EAST, EngineSeat.WEST, 2000),
                    self._transfer(
                        EngineSeat.NORTH,
                        EngineSeat.SOUTH,
                        1000,
                        TransferReason.NOTEN_PENALTY,
                    ),
                )
            )
        )
        self.assertEqual(
            diagnostic._deal_in_payments(completed, EngineSeat.EAST),
            {"SOUTH": 8300, "WEST": 2000},
        )

    def test_round_outcomes(self):
        def win(method, winners, source=None):
            return SimpleNamespace(
                result=SimpleNamespace(
                    method=method,
                    winners=tuple(SimpleNamespace(seat=s) for s in winners),
                    source_seat=source,
                )
            )

        east = EngineSeat.EAST
        self.assertEqual(
            diagnostic._round_outcome(win(WinMethod.TSUMO, [east]), east), "WIN"
        )
        self.assertEqual(
            diagnostic._round_outcome(
                win(WinMethod.RON, [EngineSeat.SOUTH], east), east
            ),
            "DEAL_IN",
        )
        self.assertEqual(
            diagnostic._round_outcome(
                win(WinMethod.RON, [EngineSeat.SOUTH], EngineSeat.WEST), east
            ),
            "OTHER_RON",
        )
        self.assertEqual(
            diagnostic._round_outcome(win(WinMethod.TSUMO, [EngineSeat.WEST]), east),
            "TSUMO_BY_OTHER",
        )
        draw = SimpleNamespace(
            result=ExhaustiveDrawResult(
                tenpai_seats=frozenset({east}), nagashi_mangan_seats=frozenset()
            )
        )
        self.assertEqual(diagnostic._round_outcome(draw, east), "DRAW_TENPAI")
        self.assertEqual(
            diagnostic._round_outcome(draw, EngineSeat.SOUTH), "DRAW_NOTEN"
        )
        self.assertIsNotNone(AbortiveDrawResult)


class SeedGuardTest(unittest.TestCase):
    def test_seed_range_must_stay_retired(self):
        with self.assertRaises(SystemExit):
            diagnostic.main(["--games", "2", "--first-seed", "931999", "--output", "x"])
        with self.assertRaises(SystemExit):
            diagnostic.main(["--games", "1", "--first-seed", "930999", "--output", "x"])


if __name__ == "__main__":
    unittest.main()
