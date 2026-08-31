"""ABBB strength summaryのhuman-readable formatting seam。

``single_round_compare``(実行直後のsummary)と
``summarize_single_round_artifacts``(保存済みartifactの再集計summary)が、
同じmetricを別の書式・別の意味で表示しないための共有presentation層である。

このmoduleはaggregationを一切行わない。metricのcanonical計算は
``lisjong_arena.single_round_evaluation``が所有し、ここは計算済みの
``SingleRoundStrengthSummary``をtextへ変換するだけである。
"""

from __future__ import annotations

from lisjong_arena.model import SingleRoundCandidateMahjongMetrics
from lisjong_arena.single_round_evaluation import (
    SeedBlockStatistics,
    SingleRoundStrengthSummary,
)


def describe_seeds(seeds: tuple[int, ...]) -> str:
    """ordered seedsを``first..last (count)``として表示する。"""
    if len(seeds) == 1:
        return f"{seeds[0]} ({len(seeds)})"
    return f"{seeds[0]}..{seeds[-1]} ({len(seeds)})"


def format_rate(count: int, total: int) -> str:
    """``count / total``を``NN.N% (count/total)``、``total``が0なら``N/A``。

    rate自体の計算はここで行わず、``total``が0でも``0.0%``へ誤表示しない
    ためのformattingだけを担当する。
    """
    if total == 0:
        return "N/A"
    return f"{count / total * 100:.1f}% ({count}/{total})"


def format_mean(value: float | None) -> str:
    """``None``を``0.0``へ丸めず``N/A``としてformatする。"""
    return "N/A" if value is None else f"{value:.1f}"


def format_mahjong_metrics(
    metrics: SingleRoundCandidateMahjongMetrics,
) -> list[str]:
    """candidateのIssue #61 Mahjong metricsをformatする。

    domain aggregation自体はすでに``SingleRoundCandidateMahjongMetrics``へ
    計算済みであり、ここは表示のためのformattingだけを行う。
    """
    m = metrics
    return [
        "mahjong metrics:",
        "",
        f"  mean round score delta:       {m.mean_round_score_delta:+.1f}",
        "",
        f"  win rate:                     {format_rate(m.win_count, m.round_count)}",
        f"  mean win points:              {format_mean(m.mean_win_points)}",
        "",
        f"  deal-in rate:                 "
        f"{format_rate(m.deal_in_count, m.round_count)}",
        f"  mean deal-in loss:            {format_mean(m.mean_deal_in_loss)}",
        "",
        f"  exhaustive-draw tenpai rate:  "
        f"{format_rate(m.exhaustive_draw_tenpai_count, m.exhaustive_draw_count)}",
        f"  mean first-tenpai turn:       {format_mean(m.mean_first_tenpai_turn)}",
    ]


def format_seed_block_statistics(statistics: SeedBlockStatistics) -> list[str]:
    """evaluation側で導出済みのseed-block statisticsを表示する。"""
    if statistics.sample_standard_deviation is None:
        standard_deviation = "N/A"
        standard_error = "N/A"
        interval = "N/A"
    else:
        standard_deviation = f"{statistics.sample_standard_deviation:.1f}"
        standard_error = f"{statistics.standard_error:.1f}"
        interval = (
            f"[{statistics.normal_approx_95_interval_lower:+.1f}, "
            f"{statistics.normal_approx_95_interval_upper:+.1f}]"
        )

    return [
        "seed-block statistics:",
        "",
        f"  {'seed blocks:':<32}{statistics.seed_block_count:>8}",
        f"  {'mean delta:':<32}{statistics.mean_seed_block_delta:>+8.1f}",
        f"  {'standard deviation:':<32}{standard_deviation:>8}",
        f"  {'standard error:':<32}{standard_error:>8}",
        f"  {'normal-approx 95% interval:':<32}{interval:>8}",
        "",
        f"  {'positive seed blocks:':<32}{statistics.positive_seed_block_count:>8}",
        f"  {'zero seed blocks:':<32}{statistics.zero_seed_block_count:>8}",
        f"  {'negative seed blocks:':<32}{statistics.negative_seed_block_count:>8}",
    ]


def format_strength_body(summary: SingleRoundStrengthSummary) -> list[str]:
    """strength summary本体(scores / seat means / mahjong / seed blocks)を返す。

    実行直後のsummaryでも、保存済みartifactの再集計summaryでも、同じ
    ``SingleRoundStrengthSummary``から同じ書式で表示する。
    """
    metrics = summary.candidate_metrics
    lines = [
        f"candidate mean score: {metrics.mean_candidate_score:.1f}",
        f"baseline mean score:  {summary.mean_baseline_score:.1f}",
        f"mean delta:            {summary.mean_candidate_game_delta:+.1f}",
        "",
        "candidate seat means:",
    ]
    for seat, seat_mean_score in enumerate(metrics.seat_mean_scores):
        lines.append(f"  seat {seat}: {seat_mean_score:.1f}")
    lines.append("")
    lines.extend(format_mahjong_metrics(metrics.mahjong_metrics))
    lines.append("")
    lines.extend(format_seed_block_statistics(summary.seed_block_statistics))
    return lines


__all__ = [
    "describe_seeds",
    "format_mahjong_metrics",
    "format_mean",
    "format_rate",
    "format_seed_block_statistics",
    "format_strength_body",
]
