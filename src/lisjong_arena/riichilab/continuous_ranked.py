"""RiichiLab ranked resilient / continuous participation runner (Issue #47)。

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
- 停止要求後は新しいgameへrequeueしない graceful shutdown。
  `asyncio.CancelledError`はretryせずcatchもせずそのまま伝播させ、
  標準のasyncio cancellation semanticsを維持する。Ctrl-Cを正常終了として
  扱うUXは`_run_cli()`の`asyncio.run()` boundaryだけが担う

generic daemon / scheduler / retry frameworkは導入しない。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from lisjong_arena.riichilab.cli import build_arg_parser, resolve_trace_path
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


async def run_continuous_ranked(
    profile: RuntimeProfile,
    token: str,
    *,
    url: str = DEFAULT_RANKED_URL,
    trace_path: str | os.PathLike | None = None,
    stop_requested: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    failure_budget: int = _FAILURE_BUDGET,
) -> ContinuousRunSummary:
    """`run_ranked_game()`を繰り返し呼び出すresilient / continuous loop。

    `profile` / `token` / `trace_path`はprocess開始時に一度resolveされた
    値をそのまま各`run_ranked_game()` invocationへ渡す(同一resolved
    contractを維持する)。gameごとに`profile.policy_factory()`から新しい
    Policy instanceを生成し、複数gameで使い回さない。

    停止要求(`stop_requested()`が`True`を返す)は次のgame開始前にだけ
    確認し、進行中のgameを中断しない。停止要求後は新しい`policy_factory()`
    を呼ばない。

    `asyncio.CancelledError`(Ctrl-C相当)はretry対象ではなく、catchも
    しない。標準のasyncio cancellation semanticsを維持するため、そのまま
    呼び出し元へpropagateさせる(cleanup後に再送出すべきという
    `asyncio`の一般原則に従う)。Ctrl-CをCLI利用者にとって正常終了として
    扱う必要がある場合は、`_run_cli()`の`asyncio.run()` boundaryで
    `KeyboardInterrupt`として扱う(engine/Policy側へshutdown semanticsを
    持ち込まない)。

    retryするのは`TransportError`(`UnexpectedDisconnectError`を含む)
    hierarchyだけである。それ以外の例外(`ProtocolError`、
    `ProtocolTraceError`、profile/credential failure、Policy/Adapter
    例外、`asyncio.CancelledError`を含むその他unexpected exception)は
    catch-allせずそのまま伝播させる。
    """
    completed_games = 0
    failed_games = 0
    consecutive_failures = 0
    last_failure_type: str | None = None
    stopped_reason = "stop_requested"

    while True:
        if stop_requested is not None and stop_requested():
            stopped_reason = "stop_requested"
            break

        policy = profile.policy_factory()
        try:
            await run_ranked_game(policy, token, url=url, trace_path=trace_path)
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
    )


def format_continuous_summary(summary: ContinuousRunSummary) -> str:
    """token / Authorization / credential値を含まないsummary文字列を作る。"""
    return "\n".join(
        [
            f"profile: {summary.profile}",
            f"completed games: {summary.completed_games}",
            f"failed games: {summary.failed_games}",
            f"consecutive failures: {summary.consecutive_failures}",
            f"last failure type: {summary.last_failure_type or 'none'}",
            f"stopped reason: {summary.stopped_reason}",
        ]
    )


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """`python -m lisjong_arena.riichilab.continuous_ranked --profile <name>`のentry point。

    profile / credential / trace pathはprocess開始時に一度だけresolveする。
    別profileへの暗黙fallbackは行わず、resolution failureはfail closed
    (retry loopへ入らず、non-zero exit)とする。

    `run_ranked_game()`自体のcontractは変更せず、continuous behaviorは
    `run_continuous_ranked()`だけが担う。

    `run_continuous_ranked()`は`asyncio.CancelledError`をcatchせず標準の
    asyncio cancellation semanticsのまま伝播させるため、Ctrl-CをCLI
    利用者にとって正常終了として扱うUXはこの関数(`asyncio.run()`の
    boundary)だけで担う。`asyncio.run()`はSIGINTによるtask cancellation
    を`KeyboardInterrupt`として呼び出し元へ再送出するため、ここでだけ
    それをsecret-safeなmessageとexit code 0へ変換する。
    """
    parser = build_arg_parser(
        prog="python -m lisjong_arena.riichilab.continuous_ranked"
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

    print(f"profile: {profile.name}")
    print("mode: ranked-continuous")
    print(f"trace: {'on' if trace_path is not None else 'off'}")
    if trace_path is not None:
        print(f"trace path: {trace_path}")

    try:
        summary = asyncio.run(
            run_continuous_ranked(profile, token, trace_path=trace_path)
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
