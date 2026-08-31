"""Policyまたは明示的Mortal candidateでABBB single-round評価を実行するCLI。

正本の起動方法:

    python -m lisjong_arena.single_round_compare \\
        --candidate finite-horizon \\
        --baseline two-step \\
        --seeds 0:99 \\
        --workers 4

このCLIの責務は次だけである。

    Policy名解決
        -> lisjong_arena.policy_catalog.POLICY_CATALOG
    Mortal candidate
        -> concrete Docker mixed runner (serial only)
    既存PolicySpec
        -> 既存SingleRoundEvaluationPlan
    既存runner
        -> run_single_round_evaluation() / run_single_round_evaluation_parallel()
    human-readable summary / optional progress presentation
    opt-in artifact persistence
        -> lisjong_arena.single_round_artifact (--artifact-out)

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
from pathlib import Path
from time import monotonic
from typing import TextIO

from lisjong_arena.model import SingleRoundEvaluationPlan, SingleRoundEvaluationResult
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.mortal_single_round_evaluation import (
    MORTAL_IDENTITY,
    MortalSingleRoundEvaluationPlan,
    MortalSingleRoundEvaluationResult,
    run_mortal_single_round_evaluation,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_artifact import save_single_round_artifact
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
    summarize_single_round_strength,
)
from lisjong_arena.single_round_summary_format import (
    describe_seeds,
    format_strength_body,
)

_PROGRESS_BAR_WIDTH = 24
_CANDIDATE_CHOICES = sorted([*POLICY_CATALOG, MORTAL_IDENTITY])


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
        "--candidate",
        required=True,
        choices=_CANDIDATE_CHOICES,
        help="candidate name (registered Policy or mortal)",
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
    parser.add_argument(
        "--artifact-out",
        type=Path,
        metavar="PATH",
        help=(
            "save the successful evaluation as a new immutable JSON artifact "
            "(Policy candidates only; never overwrites an existing path)"
        ),
    )
    parser.add_argument(
        "--mortal-image",
        help="existing local Mortal Docker image identity (no implicit pull)",
    )
    parser.add_argument(
        "--mortal-revision",
        help="Mortal implementation revision/version represented by the image",
    )
    parser.add_argument(
        "--mortal-model",
        type=Path,
        help="path to the Mortal model file named mortal.pth",
    )
    parser.add_argument(
        "--mortal-response-timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="finite wait for each Mortal action response (default: 30)",
    )
    parser.add_argument(
        "--mortal-docker-executable",
        default="docker",
        metavar="PATH",
        help="Docker CLI executable used only for the Mortal candidate",
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


_SummaryResult = SingleRoundEvaluationResult | MortalSingleRoundEvaluationResult


def format_summary(result: _SummaryResult, *, workers: int) -> str:
    """成功したsingle-round評価結果からhuman-readable summaryを組み立てる。

    strength metricsのdomain aggregationは
    ``lisjong_arena.single_round_evaluation.summarize_single_round_strength()``
    が、formattingは``lisjong_arena.single_round_summary_format``が所有する。
    このCLIは実行条件のheaderとMortal provenanceだけを足す。保存済みartifactを
    再集計するCLIも同じseamを使うため、同じmetricが別の式・別の書式にならない。
    """
    plan = result.plan
    if isinstance(result, MortalSingleRoundEvaluationResult):
        candidate_identity = MORTAL_IDENTITY
        heading = "Single-round comparison completed"
    else:
        candidate_identity = plan.candidate.identity
        heading = "Policy comparison completed"

    summary = summarize_single_round_strength(
        result.candidate_metrics, result.game_results
    )
    lines = [
        heading,
        "",
        "protocol:   ABBB / 4p-red-single",
        f"candidate:  {candidate_identity}",
        f"baseline:   {plan.baseline.identity}",
        f"seeds:      {describe_seeds(plan.seeds)}",
        f"games:      {len(result.game_results)}",
        f"workers:    {workers}",
        "",
        *format_strength_body(summary),
    ]

    if isinstance(result, MortalSingleRoundEvaluationResult):
        config = result.plan.mortal_config
        lines.extend(
            [
                "",
                "Mortal provenance:",
                f"  Docker executable:        {config.docker_executable}",
                f"  Docker image:             {config.image}",
                f"  implementation revision:  {config.implementation_revision}",
                f"  model path:               {config.model_path}",
                f"  model SHA256:             {config.model_sha256}",
                "  action response timeout:  "
                f"{config.response_timeout_seconds:g} seconds",
            ]
        )

    return "\n".join(lines)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.single_round_compare``のentry point。

    Policy名解決とseed解析はargparseの``choices`` / ``type``で行う。Mortalは
    ``POLICY_CATALOG``へ登録せず、candidateの明示的な唯一の例外として扱う。
    未知の名前やseed構文はargparse標準のfail-closed挙動(non-zero exit、usageを
    stderrへ出力)に委ねる。Policy candidateとbaselineが同じidentityの場合は
    既存``SingleRoundEvaluationPlan``のvalidationをそのまま使い、このCLI側で
    重複したvalidation logicは持たない。

    ``--progress``指定時だけparent processのstderrへexecution progressを
    表示する。final summaryは従来どおりstdoutだけへ出し、未指定時の既存
    stdout/stderr behaviorは変更しない。

    ``run_single_round_evaluation()`` / ``run_single_round_evaluation_parallel()``
    が失敗した場合はpartial summaryを出さず、non-zero exitで終了する。

    ``--artifact-out``を指定した場合だけ、evaluation成功後にartifactを保存する。
    Mortal candidate、既存path、存在しない保存先directoryは、長時間のevaluationを
    実行する前にfail closedする。保存自体が失敗した場合はpartial fileを残さず
    non-zero exitで終了する。artifact保存の有無はevaluation semanticsへ影響しない。
    """
    parser = build_arg_parser(prog="python -m lisjong_arena.single_round_compare")
    args = parser.parse_args(argv)

    is_mortal = args.candidate == MORTAL_IDENTITY
    baseline = POLICY_CATALOG[args.baseline]

    artifact_path: Path | None = args.artifact_out
    if artifact_path is not None:
        if is_mortal:
            print(
                "invalid comparison: --artifact-out does not support the Mortal "
                "candidate; only ABBB Policy strength artifacts are supported",
                file=sys.stderr,
            )
            return 2
        if artifact_path.exists():
            print(
                "invalid comparison: --artifact-out path already exists: "
                f"{artifact_path}",
                file=sys.stderr,
            )
            return 2
        if not artifact_path.parent.is_dir():
            print(
                "invalid comparison: --artifact-out directory does not exist: "
                f"{artifact_path.parent}",
                file=sys.stderr,
            )
            return 2

    if is_mortal:
        if args.workers != 1:
            print(
                "invalid comparison: Mortal evaluation requires --workers 1",
                file=sys.stderr,
            )
            return 2
        missing = [
            option
            for option, value in (
                ("--mortal-image", args.mortal_image),
                ("--mortal-revision", args.mortal_revision),
                ("--mortal-model", args.mortal_model),
            )
            if value is None
        ]
        if missing:
            print(
                "invalid comparison: Mortal candidate requires " + ", ".join(missing),
                file=sys.stderr,
            )
            return 2
        try:
            mortal_config = MortalDockerConfig(
                image=args.mortal_image,
                implementation_revision=args.mortal_revision,
                model_path=args.mortal_model,
                response_timeout_seconds=args.mortal_response_timeout,
                docker_executable=args.mortal_docker_executable,
            )
            plan = MortalSingleRoundEvaluationPlan(
                baseline=baseline,
                seeds=args.seeds,
                mortal_config=mortal_config,
            )
        except (OSError, TypeError, ValueError) as error:
            print(f"invalid comparison: {error}", file=sys.stderr)
            return 2
    else:
        candidate = POLICY_CATALOG[args.candidate]
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
        if is_mortal:
            if progress_reporter is None:
                result = run_mortal_single_round_evaluation(plan)
            else:
                result = run_mortal_single_round_evaluation(
                    plan, progress_callback=progress_reporter
                )
        elif args.workers == 1:
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

    if artifact_path is not None:
        try:
            save_single_round_artifact(result, artifact_path)
        except Exception as error:
            print(
                f"artifact save failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
