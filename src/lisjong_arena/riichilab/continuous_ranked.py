"""RiichiLab ranked resilient / continuous participation runner (Issues #47 / #232).

`lisjong_arena.riichilab.ranked.run_ranked_game()` は意図的に

    1 connection -> 1 ranked hanchan -> end_game -> return / disconnect

だけを担当するone-game primitiveであり、本moduleはこのcontractを変更しない
まま、その上位layerとして次を追加する。

- successful completion後のautomatic requeue(新しい`run_ranked_game()`
  invocation = 新しいconnectionとして扱う。same-game resumeは行わない)
- `TransportError`(`UnexpectedDisconnectError`を含む)hierarchyだけを対象
  にしたbounded backoff付きretry。`ProtocolError` / `ProtocolTraceError` /
  profile・credential failure / Policy・Adapter例外 / その他unexpected
  exceptionはcatch-allせず、そのまま伝播させてfail closedする
- 連続failureを追跡するfailure budget(到達後は追加requeueしない)
- 各gameごとに`RuntimeProfile.policy_factory()`から生成したfresh Policy
  instance(cross-game reuseはしない)
- `--games N`相当のcompleted-hanchan数によるbounded stop
- monotonic elapsed timeによるgraceful duration bound。cutoff時に進行中の
  hanchanは中断せず、完了後は新しいgameへrequeueしない
- opt-in durable ranked record acquisition(Issue #168)のper-game composition
- opt-in live presentation(Issue #381)。game attemptごとに
  `ContinuousRankedPresentationFeed`から新しいbufferを開いてone-game
  primitiveへ渡すだけで、presentationの有無でexecution semanticsは変わらない
- 停止要求後は新しいgameへrequeueしない graceful shutdown。CLIの
  `--stop-file PATH`は、そのpathが存在することを停止要求として扱う
  (Issue #383。AWS運用で外部から「今の半荘を終えたら止める」を指示する)。
  `asyncio.CancelledError`はretryせずcatchもせずそのまま伝播させ、
  標準のasyncio cancellation semanticsを維持する。Ctrl-Cを正常終了として
  扱うUXは`_run_cli()`の`asyncio.run()` boundaryだけが担う

generic daemon / scheduler / retry frameworkは導入しない。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from lisjong.policy_contract.policy import Policy

from lisjong_arena.riichilab.cli import (
    build_arg_parser,
    resolve_ranked_record_path,
    resolve_trace_path,
)
from lisjong_arena.riichilab.durable_ranked_game_record import (
    DurableRankedGameRecordError,
)
from lisjong_arena.riichilab.errors import RiichiLabClientError, TransportError
from lisjong_arena.riichilab.live_presentation import (
    BoundedRankedPresentationBuffer,
    ContinuousRankedPresentationFeed,
)
from lisjong_arena.riichilab.profile import (
    ProfileError,
    RuntimeProfile,
    resolve_credential,
    resolve_profile,
)
from lisjong_arena.riichilab.ranked import run_ranked_game
from lisjong_arena.riichilab.transport import DEFAULT_RANKED_URL

#: backoff baseline (実装前レビュー): 5s -> 10s -> 20s -> 40s -> 60s cap。
_INITIAL_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 60.0
#: 連続failureがこの回数へ到達したら追加requeueせずfail closedする。
_FAILURE_BUDGET = 5


def _backoff_seconds(consecutive_failures: int) -> float:
    """`consecutive_failures`(1始まり)からbounded backoff秒数を求める。

    `min(5 * 2 ** (consecutive_failures - 1), 60)`。upper boundを持ち、
    zero-delay retryにはならない。
    """
    return min(
        _INITIAL_BACKOFF_SECONDS * (2 ** (consecutive_failures - 1)),
        _MAX_BACKOFF_SECONDS,
    )


def _positive_completed_games(value: str) -> int:
    """CLIの`--games`をstrictなpositive integerへ変換する。"""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--games must be a positive integer"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("--games must be a positive integer")
    return parsed


def _validate_max_completed_games(max_completed_games: int | None) -> None:
    """library APIでもbool-like / non-positive targetをfail closedにする。"""
    if max_completed_games is None:
        return
    if isinstance(max_completed_games, bool) or not isinstance(
        max_completed_games, int
    ):
        raise ValueError("max_completed_games must be a positive integer or None")
    if max_completed_games < 1:
        raise ValueError("max_completed_games must be a positive integer or None")


def _positive_duration_seconds(value: str) -> int:
    """CLIの`--duration-seconds`をstrictなpositive integerへ変換する。"""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--duration-seconds must be a positive integer"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "--duration-seconds must be a positive integer"
        )
    return parsed


def _validate_max_duration_seconds(max_duration_seconds: int | None) -> None:
    """library APIでもbool-like / non-positive durationをfail closedにする。"""
    if max_duration_seconds is None:
        return
    if isinstance(max_duration_seconds, bool) or not isinstance(
        max_duration_seconds, int
    ):
        raise ValueError("max_duration_seconds must be a positive integer or None")
    if max_duration_seconds < 1:
        raise ValueError("max_duration_seconds must be a positive integer or None")


async def _acquire_ranked_record(
    policy: Policy,
    token: str,
    *,
    destination: str | os.PathLike,
    url: str,
    profile_identity: str,
    policy_identity: str,
    presentation: BoundedRankedPresentationBuffer | None = None,
) -> object:
    """Issue #168 public acquisition contractをimport-cycleなしでcompositionする。"""
    # durable record moduleはranked moduleをimportするため、ranked.py自身のCLIと
    # 同様にcall boundaryでだけimportする。
    from lisjong_arena.riichilab.durable_ranked_game_record import (
        acquire_ranked_game_record,
    )

    presentation_kwargs = {} if presentation is None else {"presentation": presentation}
    return await acquire_ranked_game_record(
        policy,
        token,
        destination=destination,
        url=url,
        profile_identity=profile_identity,
        policy_identity=policy_identity,
        **presentation_kwargs,
    )


@dataclass(frozen=True, slots=True)
class ContinuousRunSummary:
    """continuous runner終了時のsecret-safeな運用summary。

    token / Authorization / credential値・fingerprintは一切含まない。
    """

    profile: str
    completed_games: int
    failed_games: int
    consecutive_failures: int
    last_failure_type: str | None
    stopped_reason: str
    requested_completed_games: int | None = None
    requested_duration_seconds: int | None = None
    records_enabled: bool = False


async def run_continuous_ranked(
    profile: RuntimeProfile,
    token: str,
    *,
    url: str = DEFAULT_RANKED_URL,
    trace_path: str | os.PathLike | None = None,
    record_dir: str | os.PathLike | None = None,
    max_completed_games: int | None = None,
    max_duration_seconds: int | None = None,
    stop_requested: Callable[[], bool] | None = None,
    presentation: ContinuousRankedPresentationFeed | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    failure_budget: int = _FAILURE_BUDGET,
) -> ContinuousRunSummary:
    """one-game ranked primitiveを繰り返すresilient / bounded-capable loop。

    `profile` / `token` / `trace_path`はprocess開始時に一度resolveされた
    値をそのまま各one-game invocationへ渡す(同一resolved contractを維持する)。
    gameごとに`profile.policy_factory()`から新しいPolicy instanceを生成し、
    複数gameで使い回さない。

    `max_completed_games`はcompleted hanchan数を表す。Transport failureはこの
    countへ含めない。`record_dir`有効時はIssue #168のacquisitionがrecordを
    finalizeしてstrict-readbackまで成功した後だけcompletedへcountする。

    `max_duration_seconds`はrunner開始時点からのmonotonic elapsed timeによる
    boundである。cutoffは進行中のone-game primitiveをcancelせず、完了または
    failureでcontrolがrunnerへ戻った後に新しいgame / retryを開始しない。
    retry backoffよりdeadlineまでのremaining timeが短い場合はsleepをremaining
    timeへcapして、deadline到達後にretryしない。

    `record_dir`とdiagnostic traceは同時利用しない。durable record自身が
    authoritativeなper-game protocol traceを持つため、二重trace semanticsを
    このrunnerへ導入しない。

    停止要求(`stop_requested()`が`True`を返す)は次のgame開始前にだけ
    確認し、進行中のgameを中断しない。停止要求後は新しい`policy_factory()`
    を呼ばない。

    `presentation`(default `None`・opt-in、Issue #381)を渡した場合は、
    game attemptごとに`presentation.open_game()`で新しいbufferを開き、その
    one-game primitiveへだけ渡す。bufferをgame間で使い回さない。未指定時は
    one-game primitiveの呼び出し引数も含めて従来どおりである。

    retryするのは`TransportError`(`UnexpectedDisconnectError`を含む)
    hierarchyだけである。それ以外の例外(`ProtocolError`、
    `ProtocolTraceError`、durable record finalization/readback failure、
    profile/credential failure、Policy/Adapter例外、`asyncio.CancelledError`を
    含むその他unexpected exception)はcatch-allせずそのまま伝播させる。
    """
    _validate_max_completed_games(max_completed_games)
    _validate_max_duration_seconds(max_duration_seconds)
    if record_dir is not None and trace_path is not None:
        raise ValueError(
            "durable record acquisition cannot be combined with trace output"
        )
    if record_dir is not None and not os.fspath(record_dir):
        raise ValueError("record_dir must be a non-empty path or None")
    if presentation is not None and not isinstance(
        presentation, ContinuousRankedPresentationFeed
    ):
        raise TypeError("presentation must be a ContinuousRankedPresentationFeed")

    completed_games = 0
    failed_games = 0
    consecutive_failures = 0
    last_failure_type: str | None = None
    stopped_reason = "stop_requested"
    records_enabled = record_dir is not None
    deadline = (
        monotonic() + max_duration_seconds if max_duration_seconds is not None else None
    )

    while True:
        if max_completed_games is not None and completed_games >= max_completed_games:
            stopped_reason = "target_completed_games_reached"
            break
        if deadline is not None and monotonic() >= deadline:
            stopped_reason = "duration_reached"
            break
        if stop_requested is not None and stop_requested():
            stopped_reason = "stop_requested"
            break

        policy = profile.policy_factory()
        # presentationの有無でone-game primitiveの呼び出し引数を変えない。
        presentation_kwargs = (
            {} if presentation is None else {"presentation": presentation.open_game()}
        )
        try:
            if record_dir is None:
                await run_ranked_game(
                    policy,
                    token,
                    url=url,
                    trace_path=trace_path,
                    **presentation_kwargs,
                )
            else:
                destination = resolve_ranked_record_path(os.fspath(record_dir))
                if (
                    destination is None
                ):  # defensive: non-empty record_dir was validated above
                    raise ValueError("record_dir did not resolve to a destination")
                await _acquire_ranked_record(
                    policy,
                    token,
                    destination=destination,
                    url=url,
                    profile_identity=profile.name,
                    policy_identity=type(policy).__name__,
                    **presentation_kwargs,
                )
        except TransportError as error:
            failed_games += 1
            consecutive_failures += 1
            last_failure_type = type(error).__name__
            if consecutive_failures >= failure_budget:
                stopped_reason = "failure_budget_exhausted"
                break

            backoff = _backoff_seconds(consecutive_failures)
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    stopped_reason = "duration_reached"
                    break
                if remaining < backoff:
                    await sleep(remaining)
                    stopped_reason = "duration_reached"
                    break
            await sleep(backoff)
            continue

        completed_games += 1
        consecutive_failures = 0

    return ContinuousRunSummary(
        profile=profile.name,
        completed_games=completed_games,
        failed_games=failed_games,
        consecutive_failures=consecutive_failures,
        last_failure_type=last_failure_type,
        stopped_reason=stopped_reason,
        requested_completed_games=max_completed_games,
        requested_duration_seconds=max_duration_seconds,
        records_enabled=records_enabled,
    )


def format_continuous_summary(summary: ContinuousRunSummary) -> str:
    """token / Authorization / credential値を含まないsummary文字列を作る。"""
    requested = (
        str(summary.requested_completed_games)
        if summary.requested_completed_games is not None
        else "unbounded"
    )
    requested_duration = (
        str(summary.requested_duration_seconds)
        if summary.requested_duration_seconds is not None
        else "unbounded"
    )
    return "\n".join(
        [
            f"profile: {summary.profile}",
            f"requested completed games: {requested}",
            f"requested duration seconds: {requested_duration}",
            f"completed games: {summary.completed_games}",
            f"failed games: {summary.failed_games}",
            f"consecutive failures: {summary.consecutive_failures}",
            f"last failure type: {summary.last_failure_type or 'none'}",
            f"records: {'on' if summary.records_enabled else 'off'}",
            f"stopped reason: {summary.stopped_reason}",
        ]
    )


def _combine_stop_requested(
    stop_requested: Callable[[], bool] | None,
    stop_file: str | None,
) -> Callable[[], bool] | None:
    """injectされた停止要求と`--stop-file`の存在をORで合成する。"""
    if stop_file is None:
        return stop_requested

    def requested() -> bool:
        if stop_requested is not None and stop_requested():
            return True
        return os.path.lexists(stop_file)

    return requested


def run_continuous_ranked_cli(
    argv: Sequence[str] | None = None,
    *,
    presentation: ContinuousRankedPresentationFeed | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """continuous ranked CLI entry point。

    `python -m lisjong_arena.riichilab.continuous_ranked`と同じprofile /
    credential解決、出力、exit codeを、live presentation consumer
    (`lisjong-play`等)が同じprocess内から再利用するためのpublic関数である
    (Issue #381)。`presentation` / `stop_requested`はそのまま
    `run_continuous_ranked()`へ渡し、未指定時の出力とbehaviorは従来どおり。

    profile / credential / trace pathはprocess開始時に一度だけresolveする。
    別profileへの暗黙fallbackは行わず、resolution failureはfail closed
    (retry loopへ入らず、non-zero exit)とする。

    `--games N` / `--duration-seconds N`未指定時はIssue #47の
    unbounded-until-stop behaviorを維持する。
    `--record-dir`有効時は各completed hanchanをIssue #168の独立durable
    recordとして保存し、diagnostic traceとの同時利用はfail closedにする。
    `--stop-file PATH`指定時はPATHの存在を停止要求として扱い、injectされた
    `stop_requested`とORで合成する。
    """
    parser = build_arg_parser(
        prog="python -m lisjong_arena.riichilab.continuous_ranked",
        ranked_record=True,
    )
    parser.add_argument(
        "--games",
        type=_positive_completed_games,
        default=None,
        metavar="N",
        help=(
            "N completed hanchanで正常終了する。未指定時は既存どおり"
            "stop要求までcontinuous participationを継続する"
        ),
    )
    parser.add_argument(
        "--duration-seconds",
        type=_positive_duration_seconds,
        default=None,
        metavar="N",
        help=(
            "runner開始からN秒後、新しいhanchanへrequeueせず正常終了する。"
            "cutoff時に進行中のhanchanは完了してから停止する"
        ),
    )
    parser.add_argument(
        "--stop-file",
        default=None,
        metavar="PATH",
        help=(
            "PATHが存在したら停止要求として扱う。次のhanchan開始前にだけ確認し、"
            "進行中のhanchanは完了してから停止する"
        ),
    )
    args = parser.parse_args(argv)
    if args.stop_file is not None and not args.stop_file:
        print("--stop-file must be a non-empty path", file=sys.stderr)
        return 2

    try:
        profile = resolve_profile(args.profile)
        token = resolve_credential(profile)
    except ProfileError as error:
        print(str(error), file=sys.stderr)
        return 2

    trace_path = resolve_trace_path(
        profile, trace_flag=args.trace, trace_path_arg=args.trace_path
    )
    if args.record_dir is not None and trace_path is not None:
        print(
            "--record-dir cannot be combined with --trace, --trace-path, or "
            "RIICHILAB_TRACE_PATH",
            file=sys.stderr,
        )
        return 2

    print(f"profile: {profile.name}")
    print("mode: ranked-continuous")
    print(f"trace: {'on' if trace_path is not None else 'off'}")
    if trace_path is not None:
        print(f"trace path: {trace_path}")
    print(f"records: {'on' if args.record_dir is not None else 'off'}")
    print(f"requested completed games: {args.games or 'unbounded'}")
    print(f"requested duration seconds: {args.duration_seconds or 'unbounded'}")
    print(f"stop file: {'on' if args.stop_file is not None else 'off'}")

    optional_kwargs: dict[str, object] = {}
    if presentation is not None:
        optional_kwargs["presentation"] = presentation
    effective_stop_requested = _combine_stop_requested(stop_requested, args.stop_file)
    if effective_stop_requested is not None:
        optional_kwargs["stop_requested"] = effective_stop_requested

    try:
        summary = asyncio.run(
            run_continuous_ranked(
                profile,
                token,
                trace_path=trace_path,
                record_dir=args.record_dir,
                max_completed_games=args.games,
                max_duration_seconds=args.duration_seconds,
                **optional_kwargs,
            )
        )
    except DurableRankedGameRecordError as error:
        print(
            "RiichiLab continuous ranked durable record was not finalized: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        if args.record_dir is None:
            raise
        print(
            "RiichiLab continuous ranked durable record was not finalized: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except RiichiLabClientError as error:
        print(
            "RiichiLab continuous ranked runner failed: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("RiichiLab continuous ranked runner stopped by user", file=sys.stderr)
        return 0

    print(format_continuous_summary(summary))
    return 1 if summary.stopped_reason == "failure_budget_exhausted" else 0


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """module実行用entry point。`run_continuous_ranked_cli()`と同一実装。"""
    return run_continuous_ranked_cli(argv)


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = [
    "ContinuousRunSummary",
    "format_continuous_summary",
    "run_continuous_ranked",
    "run_continuous_ranked_cli",
]
