"""first-party lisjong Policyのopt-in performance profiling CLI。

正本の起動方法:

    python -m lisjong_arena.policy_performance_profile \\
        --candidate finite-horizon \\
        --baseline two-step \\
        --seeds 0:24 \\
        --mode timing

    python -m lisjong_arena.policy_performance_profile \\
        --candidate combined \\
        --baseline two-step \\
        --seeds 0:24 \\
        --mode profile

このCLIの責務は次だけである。

    Policy名解決
        -> lisjong_arena.policy_catalog.POLICY_CATALOG (first-partyだけ、Mortal等は対象外)
    seed解析
        -> lisjong_arena.single_round_compare.parse_seeds
    既存PolicySpec
        -> 既存SingleRoundEvaluationPlan
    timing / profile計測
        -> lisjong_arena.policy_performance.run_policy_timing_profile() /
           run_policy_hotspot_profile()
    human-readable report出力

ABBB rotation、``4p-red-single``固定、Policy lifecycle、raw result
canonicalization、candidate metrics aggregationは
``lisjong_arena.single_round_evaluation``が所有する既存evaluation semantics
であり、ここでは再実装しない。

``--mode timing``と``--mode profile``は排他であり、1回の実行では一方だけを
計測する。timing modeはunprofiled wall-clock performance measurementの正本、
profile modeはinstrumented hotspot discovery専用であり、profile modeの
elapsed timeをtiming performance claimへ使用しない。

初期scopeはworkers=1のserial executionだけを正本とする。このCLIに
``--workers``optionはなく、常に既存``run_single_round_evaluation()``(serial)
だけを呼ぶ``lisjong_arena.policy_performance``を経由する。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from math import isinf

from lisjong_arena.model import SingleRoundEvaluationPlan
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.policy_performance import (
    PolicyHotspotProfileResult,
    PolicyTimingProfileResult,
    run_policy_hotspot_profile,
    run_policy_timing_profile,
)
from lisjong_arena.single_round_compare import parse_seeds

_DEFAULT_TOP_N = 25


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
        help="candidate Policy name (registered first-party POLICY_CATALOG entry)",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        choices=sorted(POLICY_CATALOG),
        help="baseline Policy name (registered first-party POLICY_CATALOG entry)",
    )
    parser.add_argument(
        "--seeds",
        required=True,
        type=parse_seeds,
        metavar="N|START:END",
        help="single seed (e.g. 42) or inclusive range (e.g. 0:99)",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("timing", "profile"),
        help=(
            "timing: unprofiled wall-clock decision latency measurement (the "
            "performance measurement of record). "
            "profile: cProfile-based hotspot discovery only; its elapsed time "
            "must not be used as a performance claim."
        ),
    )
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=_DEFAULT_TOP_N,
        help=f"number of hotspot rows to show in --mode profile (default: {_DEFAULT_TOP_N})",
    )
    return parser


def _describe_seeds(seeds: tuple[int, ...]) -> str:
    if len(seeds) == 1:
        return f"{seeds[0]} ({len(seeds)})"
    return f"{seeds[0]}..{seeds[-1]} ({len(seeds)})"


def _format_seconds(value_seconds: float) -> str:
    """秒単位floatを、桁数に応じて自然なunitのhuman-readable文字列へ変換する。"""
    if isinf(value_seconds):
        return "N/A"
    if value_seconds >= 1.0:
        return f"{value_seconds:.2f} s"
    millis = value_seconds * 1_000
    if millis >= 1.0:
        return f"{millis:.2f} ms"
    micros = value_seconds * 1_000_000
    if micros >= 1.0:
        return f"{micros:.2f} µs"
    return f"{value_seconds * 1_000_000_000:.0f} ns"


def _format_duration_ns(value_ns: float) -> str:
    return _format_seconds(value_ns / 1_000_000_000)


def _format_throughput(value: float, *, unit: str) -> str:
    if isinf(value):
        return "N/A"
    return f"{value:.1f} {unit}"


def format_timing_report(
    profile: PolicyTimingProfileResult, *, workers: int = 1
) -> str:
    """timing modeのhuman-readable summaryを組み立てる。

    表示するのはすでに``lisjong_arena.policy_performance``が計測・集計済みの
    valueだけであり、ここではformattingだけを行う。
    """
    plan = profile.result.plan
    metrics = profile.candidate_decision_metrics

    lines = [
        "Policy performance profile completed (timing mode)",
        "",
        "protocol:       ABBB / 4p-red-single",
        f"candidate:      {plan.candidate.identity}",
        f"baseline:       {plan.baseline.identity}",
        f"seeds:          {_describe_seeds(plan.seeds)}",
        f"games:          {len(profile.result.game_results)}",
        f"workers:        {workers}",
        "",
        "candidate decisions:",
        f"  count:        {metrics.decision_count}",
        f"  total time:   {_format_duration_ns(metrics.total_decision_time_ns)}",
        f"  mean:         {_format_duration_ns(metrics.mean_decision_latency_ns)}",
        f"  p50:          {_format_duration_ns(metrics.p50_decision_latency_ns)}",
        f"  p95:          {_format_duration_ns(metrics.p95_decision_latency_ns)}",
        f"  max:          {_format_duration_ns(metrics.max_decision_latency_ns)}",
        "  throughput:   "
        + _format_throughput(metrics.decisions_per_second, unit="decisions/s"),
        "",
        "evaluation:",
        f"  elapsed:      {_format_seconds(profile.evaluation_elapsed_seconds)}",
        "  throughput:   "
        + _format_throughput(profile.games_per_second, unit="games/s"),
    ]
    return "\n".join(lines)


def format_hotspot_report(profile: PolicyHotspotProfileResult, *, top_n: int) -> str:
    """profile modeのhuman-readable hotspot summaryを組み立てる。

    ``self_time_seconds``降順で``top_n``件を表示する。elapsed timeは
    instrumentation overheadを含むため、absolute latencyや
    before/after speedupの根拠として提示しない。
    """
    plan = profile.result.plan

    lines = [
        "Policy performance profile completed (profile mode)",
        "",
        "protocol:       ABBB / 4p-red-single",
        f"candidate:      {plan.candidate.identity}",
        f"baseline:       {plan.baseline.identity}",
        f"seeds:          {_describe_seeds(plan.seeds)}",
        f"games:          {len(profile.result.game_results)}",
        "workers:        1",
        "",
        (
            "profile mode measures instrumented hotspot discovery only; do "
            "not use this elapsed time as an absolute latency or "
            "before/after speedup claim (see --mode timing for that)."
        ),
        "",
        f"candidate Policy hotspots (top {top_n} by self time):",
        "",
        f"{'calls':>10}  {'self (s)':>10}  {'cumulative (s)':>14}  function",
        "-" * 72,
    ]
    for stat in profile.function_stats[:top_n]:
        lines.append(
            f"{stat.call_count:>10}  {stat.self_time_seconds:>10.3f}  "
            f"{stat.cumulative_time_seconds:>14.3f}  {stat.qualified_name}"
        )
    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.policy_performance_profile``のentry point。

    Policy名解決とseed解析はargparseの``choices`` / ``type``で行う。
    candidate/baselineが同じidentityの場合は既存``SingleRoundEvaluationPlan``
    のvalidationをそのまま使う。timing/profile実行が失敗した場合はpartial
    reportを出さず、non-zero exitで終了する。
    """
    parser = build_arg_parser(prog="python -m lisjong_arena.policy_performance_profile")
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
        if args.mode == "timing":
            timing_result = run_policy_timing_profile(plan)
            print(format_timing_report(timing_result))
        else:
            hotspot_result = run_policy_hotspot_profile(plan)
            print(format_hotspot_report(hotspot_result, top_n=args.top))
    except Exception as error:
        print(
            f"policy performance profile failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = [
    "build_arg_parser",
    "format_hotspot_report",
    "format_timing_report",
]
