"""Mortal vs lisjong same-state decision diagnostic CLI。

MortalだけをRiichiEnvへ適用し、selected lisjong Policyは同じMortal-seat
Observation上でshadow実行する。これはstrength benchmarkとは独立したdiagnosticである。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from lisjong_arena.mortal_decision_evaluation import (
    MortalDecisionEvaluationPlan,
    MortalDecisionEvaluationResult,
    run_mortal_decision_evaluation,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.policy_reference import (
    PolicyReferenceError,
    resolve_policy_reference,
)
from lisjong_arena.single_round_compare import parse_seeds
from lisjong_arena.single_round_summary_format import describe_seeds


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {raw!r}")
    return value


def build_arg_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "--policy",
        required=True,
        metavar="ALIAS|MODULE:ATTRIBUTE",
        help=(
            "lisjong Policy used as three actual opponents and one independent "
            "Mortal-seat shadow"
        ),
    )
    parser.add_argument(
        "--policy-id",
        metavar="IDENTITY",
        help="required semantic identity for an explicit Policy reference",
    )
    parser.add_argument(
        "--seeds",
        required=True,
        type=parse_seeds,
        metavar="N|START:END",
        help="single seed or inclusive range",
    )
    parser.add_argument("--mortal-image", required=True)
    parser.add_argument("--mortal-revision", required=True)
    parser.add_argument("--mortal-model", required=True, type=Path)
    parser.add_argument(
        "--mortal-response-timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
    )
    parser.add_argument("--mortal-docker-executable", default="docker", metavar="PATH")
    return parser


def format_summary(result: MortalDecisionEvaluationResult) -> str:
    """大量のdecision detailを混ぜず、diagnostic aggregateだけを表示する。"""
    if not isinstance(result, MortalDecisionEvaluationResult):
        raise TypeError("result must be a MortalDecisionEvaluationResult")
    summary = result.summary
    config = result.plan.mortal_config
    lines = [
        "Mortal same-state decision diagnostic completed",
        "",
        "protocol:       Mortal driver / lisjong shadow / 4p-red-single",
        f"shadow policy:  {result.plan.policy.identity}",
        f"seeds:          {describe_seeds(result.plan.seeds)}",
        f"games:          {len(result.game_results)}",
        "",
        "paired decisions:",
        f"  total:          {summary.total_paired_decisions}",
        f"  agreements:     {summary.agreements}",
        f"  disagreements:  {summary.disagreements_count}",
        f"  agreement rate: {summary.agreement_rate:.2%}",
        "",
        "action-kind pairs (Mortal driver / lisjong shadow):",
    ]
    lines.extend(
        "  "
        f"{pair.driver_mortal_kind.value} / {pair.shadow_policy_kind.value}: "
        f"{pair.count}"
        for pair in summary.action_kind_pairs
    )
    lines.extend(
        [
            "",
            "Mortal provenance:",
            f"  Docker executable:        {config.docker_executable}",
            f"  Docker image:             {config.image}",
            f"  implementation revision:  {config.implementation_revision}",
            f"  model path:               {config.model_path}",
            f"  model SHA256:             {config.model_sha256}",
            f"  action response timeout:  {config.response_timeout_seconds:g} seconds",
            "",
            "Interpretation: disagreement is diagnostic, not an error or ground truth.",
            "Only Mortal actions drove the recorded games; lisjong actions were shadow-only.",
        ]
    )
    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser(prog="python -m lisjong_arena.mortal_decision_compare")
    args = parser.parse_args(argv)
    try:
        policy = resolve_policy_reference(args.policy, explicit_identity=args.policy_id)
        config = MortalDockerConfig(
            image=args.mortal_image,
            implementation_revision=args.mortal_revision,
            model_path=args.mortal_model,
            response_timeout_seconds=args.mortal_response_timeout,
            docker_executable=args.mortal_docker_executable,
        )
        plan = MortalDecisionEvaluationPlan(
            policy=policy,
            seeds=args.seeds,
            mortal_config=config,
        )
    except (OSError, PolicyReferenceError, TypeError, ValueError) as error:
        print(f"invalid diagnostic: {error}", file=sys.stderr)
        return 2
    try:
        result = run_mortal_decision_evaluation(plan)
    except Exception as error:
        print(
            f"Mortal decision diagnostic failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
