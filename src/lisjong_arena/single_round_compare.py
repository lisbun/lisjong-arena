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
    human-readable summary

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
from collections.abc import Sequence

from lisjong_arena.model import SingleRoundEvaluationPlan, SingleRoundEvaluationResult
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_evaluation import (
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)


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
    return parser


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


def format_summary(result: SingleRoundEvaluationResult, *, workers: int) -> str:
    """成功したsingle-round評価結果からhuman-readable summaryを組み立てる。

    baseline mean scoreとmean deltaは``SingleRoundEvaluationResult``へfield
    追加せず、raw``game_results``からこの関数で決定的に導出する。
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

    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.single_round_compare``のentry point。

    Policy名解決とseed解析はargparseの``choices`` / ``type``で行い、未知の
    Policy名やseed構文はargparse標準のfail-closed挙動(non-zero exit、
    usageをstderrへ出力)に委ねる。candidateとbaselineが同じidentityの場合は
    既存``SingleRoundEvaluationPlan``のvalidationをそのまま使い、このCLI側で
    重複したvalidation logicは持たない。

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

    try:
        if args.workers == 1:
            result = run_single_round_evaluation(plan)
        else:
            result = run_single_round_evaluation_parallel(
                plan, max_workers=args.workers
            )
    except Exception as error:
        print(
            f"single-round evaluation failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(format_summary(result, workers=args.workers))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
