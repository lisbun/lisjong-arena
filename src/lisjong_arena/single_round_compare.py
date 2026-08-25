"""登録済みPolicyを名前指定して既存ABBB single-round評価を実行するCLI。

正本の起動方法:

    python -m lisjong_arena.single_round_compare \\
        --candidate finite-horizon \\
        --baseline two-step \\
        --seeds 0:99 \\
        --workers 4

このCLIの責務は次だけである。

    Policy名解決
        -> lisjong_arena.policy_catalog.POLICY_CATALOG
    既存PolicySpec
        -> 既存SingleRoundEvaluationPlan
    既存runner
        -> run_single_round_evaluation() / run_single_round_evaluation_parallel()
    human-readable summary / optional progress presentation

ABBB rotation、``4p-red-single``固定、Policy lifecycle、raw result
canonicalization、candidate metrics aggregation、fail-closed semanticsは
``lisjong_arena.single_round_evaluation``が所有する既存evaluation semantics
であり、ここでは再実装しない。evaluation protocol自体を変更する
``--protocol`` / ``--game-mode`` / ``--rotation-count``のようなoptionも
追加しない。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from time import monotonic
from typing import TextIO

from lisjong_arena.model import SingleRoundEvaluationPlan, SingleRoundEvaluationResult
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)

_PROGRESS_BAR_WIDTH = 24


def parse_seeds(raw: str) -> tuple[int, ...]:
    """``"42"``(単一seed)または``"START:END"``(inclusive range)を解析する。

    comma listや複数rangeはこのCLIのscope外であり、サポートしない。
    seedの値域そのものはここで新たに制約しない。``SingleRoundEvaluationPlan``
    / ``ComparisonPlan``が共有する既存``_normalize_seeds()``契約は型が
    ``int``であることだけを要求し、負値を禁止していないため、この関数でも
    符号を追加検証しない。
    """
    if ":" not in raw:
        try:
            return (int(raw),)
        except ValueError:
            raise ValueError(f"invalid seed: {raw!r}") from None

    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid seed range: {raw!r}")
    start_text, end_text = parts
    if not start_text or not end_text:
        raise ValueError(f"invalid seed range: {raw!r}")
    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError:
        raise ValueError(f"invalid seed range: {raw!r}") from None
    if end < start:
        raise ValueError(f"invalid seed range: {raw!r} (end must be >= start)")
    return tuple(range(start, end + 1))


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {raw!r}")
    return value


def build_arg_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "--candidate",
        required=True,
        choices=sorted(POLICY_CATALOG),
        help="candidate policy name",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        choices=sorted(POLICY_CATALOG),
        help="baseline policy name",
    )
    parser.add_argument(
        "--seeds",
        required=True,
        type=parse_seeds,
        metavar="N|START:END",
        help="single seed (e.g. 42) or inclusive range (e.g. 0:99)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=1,
        help="local process worker count (default: 1; 1=serial, >1=parallel)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="show completed games, elapsed time, and ETA on stderr",
    )
    return parser


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, second = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minute:02d}:{second:02d}"
    return f"{minute:02d}:{second:02d}"


class _ProgressReporter:
    """ABBB game completionを1行のstderr progressとして表示する。

    evaluation semanticsやresultには一切関与せず、runnerから受け取る
    ``(completed, total)``だけをwall-clock表示へ変換する。worker processから
    直接出力せず、このobjectはCLIのparent processだけで使う。
    """

    __slots__ = ("_clock", "_finished", "_started_at", "_stream", "_total")

    def __init__(
        self,
        total: int,
        *,
        stream: TextIO,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if type(total) is not int or total <= 0:
            raise ValueError("progress total must be a positive int")
        self._total = total
        self._stream = stream
        self._clock = clock
        self._started_at = clock()
        self._finished = False
        self._write(completed=0, elapsed=0.0)

    def __call__(self, completed: int, total: int) -> None:
        if total != self._total:
            raise ValueError(f"progress total changed from {self._total} to {total}")
        if type(completed) is not int or not 0 <= completed <= total:
            raise ValueError("progress completed must be between 0 and total")
        elapsed = max(0.0, self._clock() - self._started_at)
        self._write(completed=completed, elapsed=elapsed)

    def _write(self, *, completed: int, elapsed: float) -> None:
        fraction = completed / self._total
        filled = int(_PROGRESS_BAR_WIDTH * fraction)
        bar = "#" * filled + "-" * (_PROGRESS_BAR_WIDTH - filled)
        percentage = fraction * 100.0
        eta_text = "calculating"
        if completed > 0:
            eta = elapsed / completed * (self._total - completed)
            eta_text = f"{_format_duration(eta):>11}"
        line = (
            f"\r[{bar}] {completed}/{self._total} ({percentage:5.1f}%) "
            f"elapsed {_format_duration(elapsed)} ETA {eta_text}"
        )
        self._stream.write(line)
        if completed == self._total:
            self._stream.write("\n")
            self._finished = True
        self._stream.flush()

    def close(self) -> None:
        """途中failure等でも後続stderr/stdoutがprogress行と重ならないよう改行する。"""
        if self._finished:
            return
        self._stream.write("\n")
        self._stream.flush()
        self._finished = True


def _describe_seeds(seeds: tuple[int, ...]) -> str:
    if len(seeds) == 1:
        return f"{seeds[0]} ({len(seeds)})"
    return f"{seeds[0]}..{seeds[-1]} ({len(seeds)})"


def _baseline_mean_score(result: SingleRoundEvaluationResult) -> float:
    """全gameのbaseline 3 seat(candidate以外)final scoreの平均。"""
    baseline_scores = [
        score
        for game_result in result.game_results
        for seat, score in enumerate(game_result.scores)
        if seat != game_result.candidate_seat
    ]
    return sum(baseline_scores) / len(baseline_scores)


def _mean_delta(result: SingleRoundEvaluationResult) -> float:
    """game単位の``candidate score - baseline 3 seat平均``をgame平均したdescriptive metric。"""
    deltas = []
    for game_result in result.game_results:
        baseline_scores = [
            score
            for seat, score in enumerate(game_result.scores)
            if seat != game_result.candidate_seat
        ]
        deltas.append(
            game_result.candidate_score - sum(baseline_scores) / len(baseline_scores)
        )
    return sum(deltas) / len(deltas)


def _format_rate(count: int, total: int) -> str:
    """``count / total``を``NN.N% (count/total)``、``total``が0なら``N/A``。

    rate自体の計算はここで行わず、``total``が0でも``0.0%``へ誤表示しない
    ためのformattingだけを担当する。
    """
    if total == 0:
        return "N/A"
    return f"{count / total * 100:.1f}% ({count}/{total})"


def _format_mean(value: float | None) -> str:
    """``None``を``0.0``へ丸めず``N/A``としてformatする。"""
    return "N/A" if value is None else f"{value:.1f}"


def _format_mahjong_metrics(result: SingleRoundEvaluationResult) -> list[str]:
    """candidateのIssue #61 Mahjong metricsをformatする。domain aggregation
    自体はすでに``SingleRoundCandidateMahjongMetrics``へ計算済みであり、ここは
    表示のためのformattingだけを行う。
    """
    m = result.candidate_metrics.mahjong_metrics
    return [
        "mahjong metrics:",
        "",
        f"  mean round score delta:       {m.mean_round_score_delta:+.1f}",
        "",
        f"  win rate:                     {_format_rate(m.win_count, m.round_count)}",
        f"  mean win points:              {_format_mean(m.mean_win_points)}",
        "",
        f"  deal-in rate:                 "
        f"{_format_rate(m.deal_in_count, m.round_count)}",
        f"  mean deal-in loss:            {_format_mean(m.mean_deal_in_loss)}",
        "",
        f"  exhaustive-draw tenpai rate:  "
        f"{_format_rate(m.exhaustive_draw_tenpai_count, m.exhaustive_draw_count)}",
        f"  mean first-tenpai turn:       {_format_mean(m.mean_first_tenpai_turn)}",
    ]


def format_summary(result: SingleRoundEvaluationResult, *, workers: int) -> str:
    """成功したsingle-round評価結果からhuman-readable summaryを組み立てる。

    baseline mean scoreとmean deltaは``SingleRoundEvaluationResult``へfield
    追加せず、raw``game_results``からこの関数で決定的に導出する。7 Mahjong
    metrics(Issue #61)のdomain aggregationは``SingleRoundCandidateMahjongMetrics``
    がすでに担っており、ここではformattingだけを行う。
    """
    plan = result.plan
    metrics = result.candidate_metrics

    lines = [
        "Policy comparison completed",
        "",
        "protocol:   ABBB / 4p-red-single",
        f"candidate:  {plan.candidate.identity}",
        f"baseline:   {plan.baseline.identity}",
        f"seeds:      {_describe_seeds(plan.seeds)}",
        f"games:      {len(result.game_results)}",
        f"workers:    {workers}",
        "",
        f"candidate mean score: {metrics.mean_candidate_score:.1f}",
        f"baseline mean score:  {_baseline_mean_score(result):.1f}",
        f"mean delta:            {_mean_delta(result):+.1f}",
        "",
        "candidate seat means:",
    ]
    for seat, seat_mean_score in enumerate(metrics.seat_mean_scores):
        lines.append(f"  seat {seat}: {seat_mean_score:.1f}")

    lines.append("")
    lines.extend(_format_mahjong_metrics(result))

    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.single_round_compare``のentry point。

    Policy名解決とseed解析はargparseの``choices`` / ``type``で行い、未知の
    Policy名やseed構文はargparse標準のfail-closed挙動(non-zero exit、
    usageをstderrへ出力)に委ねる。candidateとbaselineが同じidentityの場合は
    既存``SingleRoundEvaluationPlan``のvalidationをそのまま使い、このCLI側で
    重複したvalidation logicは持たない。

    ``--progress``指定時だけparent processのstderrへexecution progressを
    表示する。final summaryは従来どおりstdoutだけへ出し、未指定時の既存
    stdout/stderr behaviorは変更しない。

    ``run_single_round_evaluation()`` / ``run_single_round_evaluation_parallel()``
    が失敗した場合はpartial summaryを出さず、non-zero exitで終了する。
    """
    parser = build_arg_parser(prog="python -m lisjong_arena.single_round_compare")
    args = parser.parse_args(argv)

    candidate = POLICY_CATALOG[args.candidate]
    baseline = POLICY_CATALOG[args.baseline]

    try:
        plan = SingleRoundEvaluationPlan(
            candidate=candidate, baseline=baseline, seeds=args.seeds
        )
    except ValueError as error:
        print(f"invalid comparison: {error}", file=sys.stderr)
        return 2

    progress_reporter = None
    if args.progress:
        progress_reporter = _ProgressReporter(
            ROTATION_COUNT * len(plan.seeds), stream=sys.stderr
        )

    try:
        if args.workers == 1:
            if progress_reporter is None:
                result = run_single_round_evaluation(plan)
            else:
                result = run_single_round_evaluation(
                    plan, progress_callback=progress_reporter
                )
        elif progress_reporter is None:
            result = run_single_round_evaluation_parallel(
                plan, max_workers=args.workers
            )
        else:
            result = run_single_round_evaluation_parallel(
                plan,
                max_workers=args.workers,
                progress_callback=progress_reporter,
            )
    except Exception as error:
        if progress_reporter is not None:
            progress_reporter.close()
        print(
            f"single-round evaluation failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    if progress_reporter is not None:
        progress_reporter.close()
    print(format_summary(result, workers=args.workers))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
