"""Issue #432 局単位診断: 点数の正本、#364署名、聴牌、役牌ポン、重み付け、照合。"""

import unittest
from types import SimpleNamespace

from lisjong.policies import MinimalPolicy, ShantenPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.aabb_kyoku_diagnostic.accounting import (
    KyokuAccountingError,
    account_game,
    is_yakuhai,
)
from lisjong_arena.aabb_kyoku_diagnostic.replay import (
    replay_game,
    verify_against_comparison,
)
from lisjong_arena.aabb_kyoku_diagnostic.summary import SummaryError, summarize
from lisjong_arena.comparison import run_comparison
from lisjong_arena.model import ComparisonPlan, PolicySpec

# 各seatとも13枚で1p-4p待ちの聴牌形 / 明確なノーテン形。
TENPAI = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "2p", "3p", "E", "E"]
NOTEN = ["1m", "4m", "7m", "1p", "4p", "7p", "1s", "4s", "7s", "E", "S", "W", "N"]
START = [25000, 25000, 25000, 25000]


def start_kyoku(scores, *, kyotaku=0, honba=0, oya=0, bakaze="E", tehais=None):
    return {
        "type": "start_kyoku",
        "bakaze": bakaze,
        "kyoku": oya + 1,
        "honba": honba,
        "oya": oya,
        "kyotaku": kyotaku,
        "scores": list(scores),
        "dora_marker": "1s",
        "tehais": tehais or [list(TENPAI) for _ in range(4)],
    }


def reach(actor):
    return [
        {"type": "reach", "actor": actor},
        {"type": "reach_accepted", "actor": actor},
    ]


def game(*events):
    return [{"type": "start_game"}, *events, {"type": "end_game"}]


def ron(actor, target, deltas):
    return {"type": "hora", "actor": actor, "target": target, "deltas": list(deltas)}


def ryukyoku(deltas, reason="exhaustive_draw"):
    return {"type": "ryukyoku", "reason": reason, "deltas": list(deltas)}


class PointAuthorityTest(unittest.TestCase):
    def test_ron_decomposes_into_win_gain_and_deal_in(self):
        account = account_game(
            game(start_kyoku(START), ron(1, 2, [0, 3900, -3900, 0])),
            (25000, 28900, 21100, 25000),
        )
        (kyoku,) = account.kyokus
        self.assertEqual(kyoku.outcome, "hora")
        self.assertEqual(kyoku.seats[1].win_gain, 3900)
        self.assertTrue(kyoku.seats[2].dealt_in)
        self.assertEqual(kyoku.seats[2].deal_in_loss, 3900)
        self.assertEqual([s.point_change for s in kyoku.seats], [0, 3900, -3900, 0])

    def test_next_kyoku_boundary_is_authoritative(self):
        events = game(
            start_kyoku(START),
            ron(1, 2, [0, 3900, -3900, 0]),
            start_kyoku((25000, 28900, 21100, 25000), oya=1),
            ron(0, 1, [1000, -1000, 0, 0]),
        )
        account_game(events, (26000, 27900, 21100, 25000))
        broken = game(
            start_kyoku(START),
            ron(1, 2, [0, 3900, -3900, 0]),
            start_kyoku((25000, 29900, 20100, 25000), oya=1),
            ron(0, 1, [1000, -1000, 0, 0]),
        )
        with self.assertRaises(KyokuAccountingError):
            account_game(broken, (26000, 28900, 20100, 25000))

    def test_final_award_of_remaining_sticks_is_separated(self):
        events = game(start_kyoku(START, kyotaku=1), *reach(1), ryukyoku([0, 0, 0, 0]))
        # 全員聴牌(罰符なし)。残存2本を1 seatへ配分して終了。
        account = account_game(events, (27000, 24000, 25000, 25000))
        self.assertEqual(account.final_award_seat, 0)
        self.assertEqual(account.final_award, 2000)
        seat0 = account.kyokus[0].seats[0]
        self.assertEqual(seat0.final_award, 2000)
        self.assertEqual(seat0.end_points, 27000)
        for seat in account.kyokus[0].seats:
            self.assertTrue(seat.tenpai_at_exhaustive_draw)

    def test_award_split_across_seats_fails_closed(self):
        events = game(start_kyoku(START, kyotaku=1), *reach(1), ryukyoku([0, 0, 0, 0]))
        with self.assertRaises(KyokuAccountingError):
            account_game(events, (26000, 25000, 25000, 25000))


class KnownRiichiEnv364Test(unittest.TestCase):
    """#364 fixture値: 全員聴牌流局でdeltasが供託を重複計上する。"""

    START_364 = [32600, 23500, 17400, 26500]
    AFTER_364 = (31600, 23500, 16400, 25500)

    def _events(self, deltas, tehais=None):
        return game(
            start_kyoku(self.START_364, honba=2, tehais=tehais),
            *reach(3),
            *reach(2),
            *reach(0),
            ryukyoku(deltas),
            start_kyoku(self.AFTER_364, kyotaku=3, honba=3),
            ron(1, 3, [0, 7000, 0, -4000]),
        )

    def test_exact_signature_is_accepted_and_flagged(self):
        account = account_game(
            self._events([-1000, 0, -1000, -1000]), (31600, 30500, 16400, 21500)
        )
        first = account.kyokus[0]
        self.assertTrue(first.known_riichienv_364_signature)
        self.assertEqual([s.draw_transfer for s in first.seats], [0, 0, 0, 0])
        self.assertEqual(first.kyotaku_after, 3)
        self.assertTrue(all(s.tenpai_at_exhaustive_draw for s in first.seats))

    def test_other_mismatch_fails_closed(self):
        with self.assertRaises(KyokuAccountingError):
            account_game(
                self._events([-1000, 0, 0, -1000]), (31600, 30500, 16400, 21500)
            )

    def test_signature_with_a_noten_hand_fails_closed(self):
        tehais = [list(TENPAI), list(NOTEN), list(TENPAI), list(TENPAI)]
        with self.assertRaises(KyokuAccountingError):
            account_game(
                self._events([-1000, 0, -1000, -1000], tehais),
                (31600, 30500, 16400, 21500),
            )

    def test_consistent_zero_deltas_are_not_flagged(self):
        account = account_game(self._events([0, 0, 0, 0]), (31600, 30500, 16400, 21500))
        self.assertFalse(account.kyokus[0].known_riichienv_364_signature)


class ExhaustiveDrawTenpaiTest(unittest.TestCase):
    def _draw(self, deltas, tehais):
        after = tuple(25000 + d for d in deltas)
        return account_game(
            game(start_kyoku(START, tehais=tehais), ryukyoku(deltas)), after
        ).kyokus[0]

    def test_noten_payment_is_the_tenpai_authority(self):
        tehais = [list(TENPAI), list(TENPAI), list(NOTEN), list(NOTEN)]
        kyoku = self._draw([1500, 1500, -1500, -1500], tehais)
        self.assertEqual(
            [s.tenpai_at_exhaustive_draw for s in kyoku.seats],
            [True, True, False, False],
        )
        self.assertFalse(kyoku.tenpai_reconstruction_mismatch)

    def test_non_noten_payment_transfer_fails_closed(self):
        tehais = [list(TENPAI), list(TENPAI), list(NOTEN), list(NOTEN)]
        with self.assertRaises(KyokuAccountingError):
            self._draw([2000, 1000, -1500, -1500], tehais)

    def test_zero_transfer_with_mixed_hands_fails_closed(self):
        tehais = [list(TENPAI), list(NOTEN), list(NOTEN), list(NOTEN)]
        with self.assertRaises(KyokuAccountingError):
            self._draw([0, 0, 0, 0], tehais)

    def test_zero_transfer_all_noten(self):
        kyoku = self._draw([0, 0, 0, 0], [list(NOTEN) for _ in range(4)])
        self.assertEqual(
            [s.tenpai_at_exhaustive_draw for s in kyoku.seats], [False] * 4
        )


class YakuhaiPonTest(unittest.TestCase):
    def test_yakuhai_classification(self):
        self.assertTrue(is_yakuhai("P", bakaze="E", seat=2, oya=0))
        self.assertTrue(is_yakuhai("E", bakaze="E", seat=2, oya=0))  # 場風
        self.assertTrue(is_yakuhai("W", bakaze="E", seat=2, oya=0))  # 自風
        self.assertFalse(is_yakuhai("N", bakaze="E", seat=2, oya=0))
        self.assertFalse(is_yakuhai("5m", bakaze="E", seat=2, oya=0))

    def _calls(self, *calls):
        tehai = ["E", "E", "E", "P", "P", "P", "1m", "1m", "5m", "5m", "9s", "9s", "9s"]
        tehais = [list(tehai)] + [list(NOTEN) for _ in range(3)]
        events = game(
            start_kyoku(START, tehais=tehais),
            *calls,
            ron(1, 2, [0, 1000, -1000, 0]),
        )
        return account_game(events, (25000, 26000, 24000, 25000)).kyokus[0].seats[0]

    def test_double_wind_pon_counts_once_and_kakan_is_not_a_pon(self):
        seat = self._calls(
            {
                "type": "pon",
                "actor": 0,
                "target": 3,
                "pai": "E",
                "consumed": ["E", "E"],
            },
            {"type": "tsumo", "actor": 0, "pai": "E"},
            {"type": "kakan", "actor": 0, "pai": "E", "consumed": ["E", "E", "E"]},
        )
        self.assertEqual(seat.yakuhai_pon_count, 1)
        self.assertEqual(seat.kan_count, 1)
        self.assertEqual(seat.open_call_count, 1)

    def test_yakuhai_daiminkan_is_a_kan_not_a_pon(self):
        seat = self._calls(
            {
                "type": "daiminkan",
                "actor": 0,
                "target": 3,
                "pai": "P",
                "consumed": ["P", "P", "P"],
            },
        )
        self.assertEqual(seat.yakuhai_pon_count, 0)
        self.assertEqual(seat.kan_count, 1)
        self.assertEqual(seat.open_call_count, 1)

    def test_non_yakuhai_pon_is_not_counted(self):
        seat = self._calls(
            {
                "type": "pon",
                "actor": 0,
                "target": 3,
                "pai": "1m",
                "consumed": ["1m", "1m"],
            },
        )
        self.assertEqual(seat.yakuhai_pon_count, 0)
        self.assertEqual(seat.open_call_count, 1)


def _record(seed, rotation, kyoku_outcomes, scores, ranks):
    """AABB rotationのseat配置で、各局``kyoku_outcomes``のseatだけ和了する合成記録。"""
    rot = (
        ["a", "a", "b", "b"],
        ["b", "a", "a", "b"],
        ["b", "b", "a", "a"],
        ["a", "b", "b", "a"],
    )
    kyokus = []
    for index, won_seat in enumerate(kyoku_outcomes):
        seats = []
        for seat in range(4):
            seats.append(
                {
                    "seat": seat,
                    "won": seat == won_seat,
                    "win_count": int(seat == won_seat),
                    "dealt_in": False,
                    "deal_in_count": 0,
                    "win_gain": 0,
                    "deal_in_loss": 0,
                    "tsumo_loss": 0,
                    "draw_transfer": 0,
                    "riichi_deposit": 0,
                    "final_award": 0,
                    "riichi_accepted": False,
                    "riichi_turn": None,
                    "open_call_count": 0,
                    "yakuhai_pon_count": 0,
                    "tenpai_at_exhaustive_draw": None,
                }
            )
        kyokus.append(
            {
                "index": index,
                "outcome": "hora",
                "known_riichienv_364_signature": False,
                "tenpai_reconstruction_mismatch": False,
                "seats": seats,
            }
        )
    # 点数内訳を最終点と整合させるため、差分をseat 0の最終局win_gainへ置く。
    for seat in range(4):
        kyokus[-1]["seats"][seat]["win_gain"] = scores[seat] - 25000
    return {
        "seed": seed,
        "rotation": rotation,
        "seat_identities": rot[rotation],
        "scores": list(scores),
        "ranks": list(ranks),
        "kyokus": kyokus,
    }


class WeightingTest(unittest.TestCase):
    def test_rates_pool_seat_kyoku_within_a_seed(self):
        even = ((25000, 25000, 25000, 25000), (1, 2, 3, 4))
        records = [
            # rotation 0: 1局だけ、seat 0(a)が和了 -> a: 1/2, b: 0/2
            _record(7, 0, [0], *even),
            # rotation 1..3: 3局ずつ、seat 0が毎局和了
            _record(7, 1, [0, 0, 0], *even),  # seat0 = b
            _record(7, 2, [0, 0, 0], *even),  # seat0 = b
            _record(7, 3, [0, 0, 0], *even),  # seat0 = a
        ]
        summary = summarize(records, policy_a="a", policy_b="b")
        row = summary["seed_rows"][0]
        # 分母: 局数 x 2 seat = 1*2 + 3*2*3 = 20
        self.assertEqual(row["denominator"], {"A": 20, "B": 20})
        self.assertEqual(row["numerators"]["A"]["win_rate"], 4)
        self.assertEqual(row["numerators"]["B"]["win_rate"], 6)
        self.assertAlmostEqual(row["paired_differences"]["win_rate"], 4 / 20 - 6 / 20)
        # 半荘ごとの率の平均(1/2, 0, 0, 3/6の平均 = 0.25)とは異なる(0.2)。
        per_game_mean = (1 / 2 + 0 + 0 + 3 / 6) / 4
        self.assertNotAlmostEqual(
            row["numerators"]["A"]["win_rate"] / 20, per_game_mean
        )

    def test_primary_and_point_decomposition_reconcile(self):
        records = [
            _record(9, r, [0], (40000, 30000, 20000, 10000), (1, 2, 3, 4))
            for r in range(4)
        ]
        summary = summarize(records, policy_a="a", policy_b="b")
        row = summary["seed_rows"][0]
        self.assertAlmostEqual(
            row["primary_d"], row["raw_point_component"] + row["rank_component"]
        )
        self.assertAlmostEqual(
            sum(row["point_decomposition"].values()), row["raw_point_component"]
        )

    def test_missing_rotation_fails_closed(self):
        even = ((25000, 25000, 25000, 25000), (1, 2, 3, 4))
        with self.assertRaises(SummaryError):
            summarize(
                [_record(1, r, [0], *even) for r in range(3)],
                policy_a="a",
                policy_b="b",
            )


class VerificationTest(unittest.TestCase):
    def _comparison(self, score):
        seat_results = tuple(
            SimpleNamespace(
                seed=1,
                rotation=0,
                seat=Seat(seat),
                policy_identity=["a", "a", "b", "b"][seat],
                score=score if seat == 0 else 25000,
                rank=seat + 1,
            )
            for seat in range(4)
        )
        plan = SimpleNamespace(
            game_mode="4p-red-half",
            policy_a_identity="a",
            policy_b_identity="b",
            seeds=(1,),
        )
        return SimpleNamespace(plan=plan, seat_results=seat_results)

    def _records(self):
        return [
            {
                "seed": 1,
                "rotation": 0,
                "game_mode": "4p-red-half",
                "seat_identities": ["a", "a", "b", "b"],
                "scores": [25000, 25000, 25000, 25000],
                "ranks": [1, 2, 3, 4],
            }
        ]

    def test_exact_match_passes(self):
        result = verify_against_comparison(self._records(), self._comparison(25000))
        self.assertEqual(result["status"], "PASS")

    def test_any_score_difference_stops(self):
        result = verify_against_comparison(self._records(), self._comparison(25100))
        self.assertEqual(result["status"], "STOP")
        self.assertEqual(result["mismatched_count"], 1)


class RealReplayTest(unittest.TestCase):
    """実RiichiEnv: trace記録ありの再生が、正式経路の結果と一致する。"""

    def test_replay_matches_comparison_and_reconciles(self):
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
            policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
            seeds=(12345,),
            game_mode="4p-red-half",
            max_steps=10_000,
        )
        expected = run_comparison(plan).seat_results
        for rotation in range(4):
            record = replay_game(plan, 12345, rotation)
            rows = [r for r in expected if r.rotation == rotation]
            self.assertEqual(record["scores"], [r.score for r in rows])
            self.assertEqual(record["ranks"], [r.rank for r in rows])
            self.assertEqual(
                record["seat_identities"], [r.policy_identity for r in rows]
            )
            self.assertTrue(record["kyokus"])


if __name__ == "__main__":
    unittest.main()
