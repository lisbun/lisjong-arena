"""Issue #61のSeatRoundStats / Mahjong metricsをtest全体で使う共有fixture。

生成するvalueはcontentを気にしないtestのための最小限のvalidな中立値であり、
本Issueのsemantics自体は``tests/test_riichienv_round_stats.py``で個別に検証
する。
"""

from lisjong_arena.model import SingleRoundCandidateMahjongMetrics
from lisjong_arena.riichienv.round_stats import SeatRoundStats


def neutral_seat_round_stats(*, start_score: int, end_score: int) -> SeatRoundStats:
    """和了・放銃・流局のいずれも起きなかった中立なraw round stats。"""
    return SeatRoundStats(
        start_score=start_score,
        end_score=end_score,
        won=False,
        win_points=None,
        dealt_in=False,
        deal_in_loss=None,
        exhaustive_draw=False,
        tenpai_at_exhaustive_draw=None,
        first_tenpai_turn=None,
    )


def neutral_seat_round_stats_tuple(
    end_scores: tuple[int, int, int, int],
    *,
    start_scores: tuple[int, int, int, int] = (25000, 25000, 25000, 25000),
) -> tuple[SeatRoundStats, SeatRoundStats, SeatRoundStats, SeatRoundStats]:
    """``end_scores``と整合する4 seat分の中立raw round statsを返す。"""
    return tuple(
        neutral_seat_round_stats(
            start_score=start_scores[seat], end_score=end_scores[seat]
        )
        for seat in range(4)
    )


def neutral_candidate_mahjong_metrics(
    *, round_count: int
) -> SingleRoundCandidateMahjongMetrics:
    """testがcontentを気にしない場合のvalidな中立mahjong metrics。"""
    return SingleRoundCandidateMahjongMetrics(
        round_count=round_count,
        mean_round_score_delta=0.0,
        win_count=0,
        win_rate=0.0,
        mean_win_points=None,
        deal_in_count=0,
        deal_in_rate=0.0,
        mean_deal_in_loss=None,
        exhaustive_draw_count=0,
        exhaustive_draw_tenpai_count=0,
        exhaustive_draw_tenpai_rate=None,
        tenpai_reached_count=0,
        mean_first_tenpai_turn=None,
    )
