"""Policyまたは明示的Mortal candidateでABBB single-round評価を実行するCLI。

正本の起動方法:

    python -m lisjong_arena.single_round_compare \\
        --candidate finite-horizon \\
        --baseline two-step \\
        --seeds 0:99 \\
        --workers 4

このCLIの責務は次だけである。

    Policy reference解決
        -> curated POLICY_CATALOG alias / explicit import reference
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
from lisjong_arena.open_hand_call_diagnostic_artifact import (
    load_open_hand_diagnostic_artifact,
    save_open_hand_diagnostic_artifact,
)
from lisjong_arena.open_hand_call_diagnostics import (
    OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY,
    OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY,
    OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION,
    OpenHandDiagnosticEvaluationResult,
    OpenHandDiagnosticSummary,
    run_open_hand_diagnostic_evaluation,
    run_open_hand_diagnostic_evaluation_parallel,
)
from lisjong_arena.policy_reference import (
    PolicyReferenceError,
    resolve_policy_reference,
)
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    save_single_round_artifact,
)
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
        metavar="ALIAS|MODULE:ATTRIBUTE|mortal",
        help="candidate curated alias, explicit lisjong reference, or mortal",
    )
    parser.add_argument(
        "--candidate-id",
        metavar="IDENTITY",
        help="required semantic identity for an explicit candidate reference",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        metavar="ALIAS|MODULE:ATTRIBUTE",
        help="baseline curated alias or explicit lisjong reference",
    )
    parser.add_argument(
        "--baseline-id",
        metavar="IDENTITY",
        help="required semantic identity for an explicit baseline reference",
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
        "--open-hand-call-diagnostics-out",
        type=Path,
        metavar="PATH",
        help=(
            "save Issue #196 OpenHandYakuAwareCallPolicy vs yakuhai-call "
            "decision diagnostics as a strength-artifact-bound sidecar"
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


def format_open_hand_diagnostic_summary(
    summary: OpenHandDiagnosticSummary,
) -> str:
    """Issue #196 diagnosticsをstrength summaryとは別sectionで表示する。"""
    mean = summary.mean_divergences_per_divergent_game
    mean_text = "n/a" if mean is None else f"{mean:.3f}"
    return "\n".join(
        (
            "Open-hand call decision diagnostics (not a strength metric):",
            f"  candidate-seat decisions: {summary.total_candidate_seat_decisions}",
            f"  same actions: {summary.same_action_decisions}",
            f"  divergent actions: {summary.divergent_action_decisions}",
            f"  action divergence rate: {summary.action_divergence_rate:.3%}",
            "  shared-prefix initial-call opportunities: "
            f"{summary.shared_prefix_initial_call_opportunities}",
            "  baseline Pass -> candidate Chi: "
            f"{summary.baseline_pass_to_candidate_chi}",
            "  baseline Pass -> candidate Pon: "
            f"{summary.baseline_pass_to_candidate_pon}",
            f"  candidate-only Chi: {summary.candidate_only_chi_count}",
            f"  candidate-only Pon: {summary.candidate_only_pon_count}",
            f"  games with divergence: {summary.divergent_game_count}",
            f"  seed blocks with divergence: {summary.divergent_seed_block_count}",
            "  seed blocks with nonzero score delta: "
            f"{summary.nonzero_score_delta_seed_block_count}",
            "  divergent seed blocks (positive / zero / negative): "
            f"{summary.divergent_positive_score_delta_seed_blocks} / "
            f"{summary.divergent_zero_score_delta_seed_blocks} / "
            f"{summary.divergent_negative_score_delta_seed_blocks}",
            f"  mean divergences per divergent game: {mean_text}",
            "  divergent decisions by candidate seat (E/S/W/N): "
            + " / ".join(
                str(value) for value in summary.divergent_decisions_by_candidate_seat
            ),
        )
    )


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """``python -m lisjong_arena.single_round_compare``のentry point。

    Policy referenceはcandidate / baseline共通の``resolve_policy_reference()``で
    解決する。Mortalは``POLICY_CATALOG``へ登録せず、candidateの明示的な唯一の
    例外として扱う。未知alias、invalid import reference、missing identityは
    fallbackせずfail closedする。Policy candidateとbaselineが同じidentityの場合は
    既存``SingleRoundEvaluationPlan``のvalidationをそのまま使い、このCLI側で
    重複したvalidation logicは持たない。

    ``--progress``指定時だけparent processのstderrへexecution progressを
    表示する。final summaryは従来どおりstdoutだけへ出し、未指定時の既存
    stdout/stderr behaviorは変更しない。

    ``run_single_round_evaluation()`` / ``run_single_round_evaluation_parallel()``
    が失敗した場合はpartial summaryを出さず、non-zero exitで終了する。

    ``--artifact-out``を指定した場合だけ、evaluation成功後にartifactを保存する。
    Mortal candidate、既存path、存在しない保存先directory、検証不能なexecution
    provenanceは、長時間のevaluationを実行する前にfail closedする。保存自体が
    失敗した場合はpartial fileを残さずnon-zero exitで終了する。artifact保存の
    有無はevaluation semanticsへ影響しない。
    """
    parser = build_arg_parser(prog="python -m lisjong_arena.single_round_compare")
    args = parser.parse_args(argv)

    is_mortal = args.candidate == MORTAL_IDENTITY
    try:
        baseline = resolve_policy_reference(
            args.baseline, explicit_identity=args.baseline_id
        )
        if is_mortal:
            if args.candidate_id is not None:
                raise PolicyReferenceError(
                    "--candidate-id is only valid for an explicit Policy reference, "
                    "not the Mortal candidate"
                )
            candidate = None
        else:
            candidate = resolve_policy_reference(
                args.candidate, explicit_identity=args.candidate_id
            )
    except (PolicyReferenceError, TypeError) as error:
        print(f"invalid comparison: {error}", file=sys.stderr)
        return 2

    artifact_path: Path | None = args.artifact_out
    diagnostic_path: Path | None = args.open_hand_call_diagnostics_out
    if diagnostic_path is not None:
        if artifact_path is None:
            print(
                "invalid comparison: --open-hand-call-diagnostics-out requires "
                "--artifact-out",
                file=sys.stderr,
            )
            return 2
        if is_mortal:
            print(
                "invalid comparison: open-hand call diagnostics do not support "
                "the Mortal candidate",
                file=sys.stderr,
            )
            return 2
        if (
            args.candidate != "lisjong.policies:OpenHandYakuAwareCallPolicy"
            or args.candidate_id != OPEN_HAND_DIAGNOSTIC_CANDIDATE_IDENTITY
            or args.baseline != OPEN_HAND_DIAGNOSTIC_BASELINE_IDENTITY
            or args.baseline_id is not None
        ):
            print(
                "invalid comparison: open-hand call diagnostics require exact "
                "candidate lisjong.policies:OpenHandYakuAwareCallPolicy with "
                "--candidate-id open-hand-yaku-aware-call and baseline "
                "yakuhai-call",
                file=sys.stderr,
            )
            return 2
        if diagnostic_path == artifact_path:
            print(
                "invalid comparison: strength artifact and diagnostic sidecar "
                "paths must differ",
                file=sys.stderr,
            )
            return 2
        if diagnostic_path.exists():
            print(
                "invalid comparison: diagnostic sidecar path already exists: "
                f"{diagnostic_path}",
                file=sys.stderr,
            )
            return 2
        if not diagnostic_path.parent.is_dir():
            print(
                "invalid comparison: diagnostic sidecar directory does not exist: "
                f"{diagnostic_path.parent}",
                file=sys.stderr,
            )
            return 2
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

        try:
            preflight_provenance = collect_execution_provenance()
        except Exception as error:
            print(
                "artifact provenance preflight failed: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
        if diagnostic_path is not None:
            if (
                preflight_provenance.lisjong_revision
                != OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION
            ):
                print(
                    "artifact provenance preflight failed: open-hand call "
                    "diagnostics require exact lisjong revision "
                    f"{OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION}, got "
                    f"{preflight_provenance.lisjong_revision}",
                    file=sys.stderr,
                )
                return 1

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
        assert candidate is not None
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

    diagnostic_result: OpenHandDiagnosticEvaluationResult | None = None
    try:
        if is_mortal:
            if progress_reporter is None:
                result = run_mortal_single_round_evaluation(plan)
            else:
                result = run_mortal_single_round_evaluation(
                    plan, progress_callback=progress_reporter
                )
        elif diagnostic_path is not None and args.workers == 1:
            if progress_reporter is None:
                diagnostic_result = run_open_hand_diagnostic_evaluation(plan)
            else:
                diagnostic_result = run_open_hand_diagnostic_evaluation(
                    plan, progress_callback=progress_reporter
                )
            result = diagnostic_result.evaluation_result
        elif diagnostic_path is not None:
            if progress_reporter is None:
                diagnostic_result = run_open_hand_diagnostic_evaluation_parallel(
                    plan, max_workers=args.workers
                )
            else:
                diagnostic_result = run_open_hand_diagnostic_evaluation_parallel(
                    plan,
                    max_workers=args.workers,
                    progress_callback=progress_reporter,
                )
            result = diagnostic_result.evaluation_result
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
    if diagnostic_result is not None:
        print()
        print(format_open_hand_diagnostic_summary(diagnostic_result.summary))

    if artifact_path is not None:
        try:
            save_single_round_artifact(result, artifact_path)
        except Exception as error:
            print(
                f"artifact save failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
    if diagnostic_path is not None:
        assert artifact_path is not None
        assert diagnostic_result is not None
        try:
            save_open_hand_diagnostic_artifact(
                diagnostic_result,
                strength_artifact_path=artifact_path,
                path=diagnostic_path,
            )
            load_open_hand_diagnostic_artifact(
                diagnostic_path,
                strength_artifact_path=artifact_path,
            )
        except Exception as error:
            print(
                f"diagnostic sidecar save failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
