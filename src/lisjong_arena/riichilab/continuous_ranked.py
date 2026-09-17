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
- opt-in durable ranked record acquisition(Issue #168)のper-game composition
- 停止要求後は新しいgameへrequeueしない graceful shutdown。
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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from lisjong.policy_contract.policy import Policy
from lisjong_arena.riichilab.cli import (
    build_arg_parser,
    resolve_ranked_record_path,
    resolve_trace_path,
)
from lisjong_arena.riichilab.errors import RiichiLabClientError, TransportError
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
        raise argparse.ArgumentTypeError("--games must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("--games must be a positive integer")
    return parsed


def _validate_max_completed_games(max_completed_games: int | None) -> None:
    """library APIでもbool-like / non-positive targetをfail closedにする。"""
    if max_completed_games is None:
        return
    if isinstance(max_completed_games, bool) or not isinstance(max_completed_games, int):
        raise ValueError("max_completed_games must be a positive integer or None")
    if max_completed_games < 1:
        raise ValueError("max_completed_games must be a positive integer or None")


async def _acquire_ranked_record(
    policy: Policy,
    token: str,
    *,
    destination: str | os.PathLike,
    url: str,
    profile_identity: str,
    policy_identity: str,
) -> object:
    """Issue #168 public acquisition contractをimport-cycleなしでcompositionする。"""
    # durable record moduleはranked moduleをimportするため、ranked.py自身のCLIと
    # 同様にcall boundaryでだけimportする。
    from lisjong_arena.riichilab.durable_ranked_game_record import (
        acquire_ranked_game_record,
    )

    return await acquire_ranked_game_record(
        policy,
        token,
        destination=destination,
        url=url,
        profile_identity=profile_identity,
        policy_identity=policy_identity,
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
    records_enabled: bool = False


async def run_continuous_ranked(
    profile: RuntimeProfile,
    token: str,
    *,
    url: str = DEFAULT_RANKED_URL,
    trace_path: str | os.PathLike | None = None,
    record_dir: str | os.PathLike | None = None,
    max_completed_games: int | None = None,
    stop_requested: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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

    `record_dir`とdiagnostic traceは同時利用しない。durable record自身が
    authoritativeなper-game protocol traceを持つため、二重trace semanticsを
    このrunnerへ導入しない。

    停止要求(`stop_requested()`が`True`を返す)は次のgame開始前にだけ
    確認し、進行中のgameを中断しない。停止要求後は新しい`policy_factory()`
    を呼ばない。

    retryするのは`TransportError`(`UnexpectedDisconnectError`を含む)
    hierarchyだけである。それ以外の例外(`ProtocolError`、
    `ProtocolTraceError`、durable record finalization/readback failure、
    profile/credential failure、Policy/Adapter例外、`asyncio.CancelledError`を
    含むその他unexpected exception)はcatch-allせずそのまま伝播させる。
    """
    _validate_max_completed_games(max_completed_games)
    if record_dir is not None and trace_path is not None:
        raise ValueError("durable record acquisition cannot be combined with trace output")
    if record_dir is not None and not os.fspath(record_dir):
        raise ValueError("record_dir must be a non-empty path or None")

    completed_games = 0
    failed_games = 0
    consecutive_failures = 0
    last_failure_type: str | None = None
    stopped_reason = "stop_requested"
    records_enabled = record_dir is not None

    while True:
        if (
            max_completed_games is not None
            and completed_games >= max_completed_games
        ):
            stopped_reason = "target_completed_games_reached"
            break
        if stop_requested is not None and stop_requested():
            stopped_reason = "stop_requested"
            break

        policy = profile.policy_factory()
        try:
            if record_dir is None:
                await run_ranked_game(policy, token, url=url, trace_path=trace_path)
            else:
                destination = resolve_ranked_record_path(os.fspath(record_dir))
                if destination is None:  # defensive: non-empty record_dir was validated above
                    raise ValueError("record_dir did not resolve to a destination")
                await _acquire_ranked_record(
                    policy,
                    token,
                    destination=destination,
                    url=url,
                    profile_identity=profile.name,
                    policy_identity=type(policy).__name__,
                )
        except TransportError as error:
            failed_games += 1
            consecutive_failures += 1
            last_failure_type = type(error).__name__
            if consecutive_failures >= failure_budget:
                stopped_reason = "failure_budget_exhausted"
                break
            await sleep(_backoff_seconds(consecutive_failures))
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
        records_enabled=records_enabled,
    )


def format_continuous_summary(summary: ContinuousRunSummary) -> str:
    """token / Authorization / credential値を含まないsummary文字列を作る。"""
    requested = (
        str(summary.requested_completed_games)
        if summary.requested_completed_games is not None
        else "unbounded"
    )
    return "\n".join(
        [
            f"profile: {summary.profile}",
            f"requested completed games: {requested}",
            f"completed games: {summary.completed_games}",
            f"failed games: {summary.failed_games}",
            f"consecutive failures: {summary.consecutive_failures}",
            f"last failure type: {summary.last_failure_type or 'none'}",
            f"records: {'on' if summary.records_enabled else 'off'}",
            f"stopped reason: {summary.stopped_reason}",
        ]
    )


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """continuous ranked CLI entry point。

    profile / credential / trace pathはprocess開始時に一度だけresolveする。
    別profileへの暗黙fallbackは行わず、resolution failureはfail closed
    (retry loopへ入らず、non-zero exit)とする。

    `--games N`未指定時はIssue #47のunbounded-until-stop behaviorを維持する。
    `--record-dir`有効時は各completed hanchanをIssue #168の独立durable
    recordとして保存し、diagnostic traceとの同時利用はfail closedにする。
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
    args = parser.parse_args(argv)

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

    try:
        summary = asyncio.run(
            run_continuous_ranked(
                profile,
                token,
                trace_path=trace_path,
                record_dir=args.record_dir,
                max_completed_games=args.games,
            )
        )
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


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = [
    "ContinuousRunSummary",
    "format_continuous_summary",
    "run_continuous_ranked",
]
