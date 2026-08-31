"""保存済みABBB strength artifactを再集計してsummaryを表示するCLI。

正本の起動方法:

    python -m lisjong_arena.summarize_single_round_artifacts \\
        run-a.json run-b.json

1 artifactならそのrunのsummary、compatibleな複数artifactならcumulative
summaryを表示する。合成条件の検証と再集計は
``lisjong_arena.single_round_artifact.merge_single_round_artifacts()``、
metricのcanonical計算は``lisjong_arena.single_round_evaluation``、formatting
は``lisjong_arena.single_round_summary_format``が所有し、このmoduleは
argument parsingとpresentationのwiringだけを行う。

Policyを実行せず、artifactからexecutable planを復元しない。実行時の
``--workers``のようなexecution presentation fieldはartifactに存在しないため
表示しない。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from lisjong_arena.single_round_artifact import (
    CumulativeSingleRoundStrength,
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    merge_single_round_artifacts,
)
from lisjong_arena.single_round_summary_format import (
    describe_seeds,
    format_strength_body,
)


def build_arg_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="ARTIFACT",
        help="saved ABBB strength artifact path (repeatable, order preserved)",
    )
    return parser


def _format_sources(
    paths: Sequence[Path],
    artifacts: Sequence[SingleRoundStrengthArtifact],
) -> list[str]:
    lines = ["artifacts:"]
    for index, (path, artifact) in enumerate(zip(paths, artifacts), start=1):
        lines.append(
            f"  [{index}] {path}: seeds {describe_seeds(artifact.plan.seeds)}, "
            f"games {len(artifact.game_results)}"
        )
    return lines


def _format_provenance(cumulative: CumulativeSingleRoundStrength) -> list[str]:
    provenance = cumulative.provenance
    return [
        "provenance:",
        "",
        f"  {'execution environment:':<28}{provenance.execution_environment}",
        f"  {'lisjong-arena version:':<28}{provenance.lisjong_arena_version}",
        f"  {'lisjong version:':<28}{provenance.lisjong_version}",
        f"  {'lisjong revision:':<28}{provenance.lisjong_revision}",
        f"  {'lisjong-engine version:':<28}{provenance.lisjong_engine_version}",
        f"  {'lisjong-engine revision:':<28}{provenance.lisjong_engine_revision}",
        f"  {'RiichiEnv version:':<28}{provenance.riichienv_version}",
        f"  {'Python version:':<28}{provenance.python_version}",
    ]


def format_artifact_summary(
    paths: Sequence[Path],
    artifacts: Sequence[SingleRoundStrengthArtifact],
    cumulative: CumulativeSingleRoundStrength,
) -> str:
    """再集計済みcumulative strengthをhuman-readable summaryへ組み立てる。

    strength metricsの書式は``single_round_compare``と同じ共有seamを使う。
    artifactに存在しない実行時情報は表示しない。
    """
    plan = cumulative.plan
    lines = [
        "Single-round strength artifact summary",
        "",
        f"protocol:   ABBB / {plan.game_mode}",
        f"candidate:  {plan.candidate_identity}",
        f"baseline:   {plan.baseline_identity}",
        f"seeds:      {describe_seeds(plan.seeds)}",
        f"games:      {len(cumulative.game_results)}",
        f"artifacts:  {cumulative.artifact_count}",
        "",
        *_format_sources(paths, artifacts),
        "",
        *format_strength_body(cumulative.summary),
        "",
        *_format_provenance(cumulative),
    ]
    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.summarize_single_round_artifacts``のentry point。

    artifactのload失敗、schema / protocol不一致、合成不能な組み合わせは
    partial summaryを出さずnon-zero exitで終了する。
    """
    parser = build_arg_parser(
        prog="python -m lisjong_arena.summarize_single_round_artifacts"
    )
    args = parser.parse_args(argv)

    artifacts: list[SingleRoundStrengthArtifact] = []
    for path in args.paths:
        try:
            artifacts.append(load_single_round_artifact(path))
        except Exception as error:
            print(
                f"failed to load artifact {path}: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1

    try:
        cumulative = merge_single_round_artifacts(artifacts)
    except Exception as error:
        print(
            f"incompatible artifacts: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(format_artifact_summary(args.paths, artifacts, cumulative))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
