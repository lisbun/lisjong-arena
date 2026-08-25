"""Issue #61の7 Mahjong metrics集計(``aggregate_candidate_metrics``)のunit test。

synthetic複数gameのcandidate ``SeatRoundStats``から、7指標とその
numerator / denominator countsを手計算した値と突き合わせる。実RiichiEnvは
起動しない。RiichiEnv 0.4.8のevent semantics自体は
``tests/test_riichienv_round_stats_integration.py``が別途固定する。
"""

import unittest

from _round_stats_fixtures import neutral_seat_round_stats
from lisjong.policy_contract import Seat

from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_evaluation import (
    _aggregate_candidate_mahjong_metrics,
    aggregate_candidate_metrics,
)


def _game_result(
    *, rotation: int, candidate_stats: SeatRoundStats
) -> SingleRoundGameResult:
    """既存ABBB rotation契約どおり、``candidate_seat``は``Seat(rotation)``に
    なる。candidateのstatsはその``rotation``番目のseatへ置き、残り3 seatは
    中立statsにする。
    """
    neutral = neutral_seat_round_stats(start_score=25_000, end_score=25_000)
    seat_round_stats = tuple(
        candidate_stats if seat == rotation else neutral for seat in range(4)
    )
    scores = tuple(
        candidate_stats.end_score if seat == rotation else 25_000 for seat in range(4)
    )
    return SingleRoundGameResult(
        seed=1,
        rotation=rotation,
        game_mode="4p-red-single",
        candidate_seat=Seat(rotation),
        scores=scores,
        seat_round_stats=seat_round_stats,
    )


class SyntheticAggregationTest(unittest.TestCase):
    """4局分のsynthetic dataを手計算した値と突き合わせる。"""

    def setUp(self) -> None:
        # game0: ronで2000点和了、first tenpai turn=3
        game0 = _game_result(
            rotation=0,
            candidate_stats=SeatRoundStats(
                start_score=25_000,
                end_score=27_000,
                won=True,
                win_points=2_000,
                dealt_in=False,
                deal_in_loss=None,
                exhaustive_draw=False,
                tenpai_at_exhaustive_draw=None,
                first_tenpai_turn=3,
            ),
        )
        # game1: 放銃8000点、一度もtenpaiに到達しない
        game1 = _game_result(
            rotation=1,
            candidate_stats=SeatRoundStats(
                start_score=25_000,
                end_score=17_000,
                won=False,
                win_points=None,
                dealt_in=True,
                deal_in_loss=8_000,
                exhaustive_draw=False,
                tenpai_at_exhaustive_draw=None,
                first_tenpai_turn=None,
            ),
        )
        # game2: 通常荒牌流局でtenpai、turn0(配牌)でtenpai
        game2 = _game_result(
            rotation=2,
            candidate_stats=SeatRoundStats(
                start_score=25_000,
                end_score=25_000,
                won=False,
                win_points=None,
                dealt_in=False,
                deal_in_loss=None,
                exhaustive_draw=True,
                tenpai_at_exhaustive_draw=True,
                first_tenpai_turn=0,
            ),
        )
        # game3: 通常荒牌流局でnoten、turn5でtenpaiに到達していた
        game3 = _game_result(
            rotation=3,
            candidate_stats=SeatRoundStats(
                start_score=25_000,
                end_score=24_000,
                won=False,
                win_points=None,
                dealt_in=False,
                deal_in_loss=None,
                exhaustive_draw=True,
                tenpai_at_exhaustive_draw=False,
                first_tenpai_turn=5,
            ),
        )
        self.game_results = (game0, game1, game2, game3)
        self.metrics = aggregate_candidate_metrics(
            "candidate", self.game_results
        ).mahjong_metrics

    def test_round_count(self) -> None:
        self.assertEqual(self.metrics.round_count, 4)

    def test_mean_round_score_delta(self) -> None:
        # deltas: +2000, -8000, 0, -1000 -> mean = -7000/4 = -1750.0
        self.assertEqual(self.metrics.mean_round_score_delta, -1750.0)

    def test_win_rate_and_mean_win_points(self) -> None:
        self.assertEqual(self.metrics.win_count, 1)
        self.assertEqual(self.metrics.win_rate, 0.25)
        self.assertEqual(self.metrics.mean_win_points, 2_000.0)

    def test_deal_in_rate_and_mean_deal_in_loss(self) -> None:
        self.assertEqual(self.metrics.deal_in_count, 1)
        self.assertEqual(self.metrics.deal_in_rate, 0.25)
        self.assertEqual(self.metrics.mean_deal_in_loss, 8_000.0)

    def test_exhaustive_draw_tenpai_rate(self) -> None:
        self.assertEqual(self.metrics.exhaustive_draw_count, 2)
        self.assertEqual(self.metrics.exhaustive_draw_tenpai_count, 1)
        self.assertEqual(self.metrics.exhaustive_draw_tenpai_rate, 0.5)

    def test_mean_first_tenpai_turn(self) -> None:
        # tenpai到達: game0(turn3), game2(turn0), game3(turn5) -> game1は除外
        self.assertEqual(self.metrics.tenpai_reached_count, 3)
        self.assertAlmostEqual(self.metrics.mean_first_tenpai_turn, (3 + 0 + 5) / 3)


class NoneSemanticsTest(unittest.TestCase):
    """該当eventが0件の指標は``0.0``ではなく``None``になることを確認する。

    ``aggregate_candidate_metrics()``自体が要求する「candidateが4 seat全部を
    最低1回ずつ担当する」というseat coverage契約は、この7 metrics集計とは
    無関係な既存final-score系metricsのための制約なので、ここでは内部の
    ``_aggregate_candidate_mahjong_metrics()``を直接使い、1局だけのsynthetic
    dataでNone semanticsを確認する。
    """

    def _aggregate(self, candidate_stats: SeatRoundStats):
        game_results = (_game_result(rotation=0, candidate_stats=candidate_stats),)
        return _aggregate_candidate_mahjong_metrics(game_results)

    def test_no_wins_gives_none_mean_win_points(self) -> None:
        metrics = self._aggregate(
            neutral_seat_round_stats(start_score=25_000, end_score=25_000)
        )
        self.assertEqual(metrics.win_count, 0)
        self.assertEqual(metrics.win_rate, 0.0)
        self.assertIsNone(metrics.mean_win_points)

    def test_no_deal_ins_gives_none_mean_deal_in_loss(self) -> None:
        metrics = self._aggregate(
            neutral_seat_round_stats(start_score=25_000, end_score=25_000)
        )
        self.assertEqual(metrics.deal_in_count, 0)
        self.assertEqual(metrics.deal_in_rate, 0.0)
        self.assertIsNone(metrics.mean_deal_in_loss)

    def test_no_exhaustive_draws_gives_none_rate(self) -> None:
        metrics = self._aggregate(
            neutral_seat_round_stats(start_score=25_000, end_score=25_000)
        )
        self.assertEqual(metrics.exhaustive_draw_count, 0)
        self.assertIsNone(metrics.exhaustive_draw_tenpai_rate)

    def test_no_tenpai_reached_gives_none_mean_turn(self) -> None:
        metrics = self._aggregate(
            neutral_seat_round_stats(start_score=25_000, end_score=25_000)
        )
        self.assertEqual(metrics.tenpai_reached_count, 0)
        self.assertIsNone(metrics.mean_first_tenpai_turn)


class BaselineStatsAreNotDiscardedTest(unittest.TestCase):
    """4席raw statsを保持したまま、集計はcandidate分だけを対象にすることを確認する。"""

    def test_baseline_seat_round_stats_survive_in_game_results(self) -> None:
        candidate_stats = SeatRoundStats(
            start_score=25_000,
            end_score=27_000,
            won=True,
            win_points=2_000,
            dealt_in=False,
            deal_in_loss=None,
            exhaustive_draw=False,
            tenpai_at_exhaustive_draw=None,
            first_tenpai_turn=3,
        )
        game_result = _game_result(rotation=0, candidate_stats=candidate_stats)

        self.assertEqual(len(game_result.seat_round_stats), 4)
        self.assertIs(game_result.seat_round_stats[0], candidate_stats)
        for seat in range(1, 4):
            self.assertFalse(game_result.seat_round_stats[seat].won)
            self.assertFalse(game_result.seat_round_stats[seat].dealt_in)

        # mahjong metrics集計を呼んでもgame_result自体のbaseline statsは
        # 変更されない。
        _aggregate_candidate_mahjong_metrics((game_result,))
        self.assertEqual(len(game_result.seat_round_stats), 4)
        self.assertIs(game_result.seat_round_stats[0], candidate_stats)


if __name__ == "__main__":
    unittest.main()
