"""RiichiLab ranked 1半荘のArena-owned orchestration APIとfirst-party CLI。

`RankedGameResult` / `run_ranked_game()` はIssue #17でArenaへcanonical
implementationを移す。`RankedSession`、transport、protocol trace、profile /
credential resolution、RiichiLab Adapter等のlower-level runtimeはまだ
`lisjong`に物理的に存在し、そのpublic APIをtemporaryに再利用する。

Usage:
    python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lisjong.policy_contract.policy import Policy
from lisjong.policy_contract.seat import Seat
from lisjong.riichilab_client import (
    DEFAULT_RANKED_URL,
    JsonlProtocolTraceWriter,
    ProtocolError,
    RankedSession,
    RiichiLabClientError,
    connect_ranked_transport,
    drive_ranked_session,
)
from lisjong.riichilab_client.cli import build_arg_parser, resolve_trace_path
from lisjong.riichilab_client.profile import (
    ProfileError,
    build_runtime_summary,
    format_runtime_summary,
    resolve_credential,
    resolve_profile,
)


@dataclass(frozen=True, slots=True)
class RankedGameResult:
    """1 ranked hanchanのsecret-safeな完走結果。"""

    end_game_received: bool
    seat: Seat
    requests_received: int
    responses_sent: int
    ack_history: Mapping[int, tuple[str, ...]]
    scores: tuple[int, int, int, int] | None


async def run_ranked_game(
    policy: Policy,
    token: str,
    *,
    url: str = DEFAULT_RANKED_URL,
    trace_path: str | os.PathLike | None = None,
) -> RankedGameResult:
    """ranked endpointへ1回接続し、1 full hanchanの`end_game`で終了する。"""
    if not isinstance(token, str) or not token:
        raise ValueError("token must be a non-empty string")

    session = RankedSession(policy)
    trace_writer = (
        JsonlProtocolTraceWriter(trace_path) if trace_path is not None else None
    )
    try:
        async with connect_ranked_transport(url, token) as transport:
            await drive_ranked_session(session, transport, trace=trace_writer)
    finally:
        if trace_writer is not None:
            trace_writer.close()

    status = session.status()
    if status.seat is None:
        raise ProtocolError("ranked game completed without a bound seat")

    return RankedGameResult(
        end_game_received=status.end_game_received,
        seat=status.seat,
        requests_received=status.requests_received,
        responses_sent=status.responses_sent,
        ack_history=status.ack_history,
        scores=status.scores,
    )


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """`python -m lisjong_arena.riichilab.ranked --profile <name>`のentry point。

    `lisjong.riichilab_client.profile` / `cli`が解決したprofile・credential・
    trace pathをArena-local `run_ranked_game()`へ渡す。profile未指定・unknown
    profile・credential未設定はいずれもfail closed(non-zero exit、secretを
    含まないメッセージ)とし、他profileへの暗黙fallbackは行わない。

    接続からend_gameまでの1 ranked hanchanで終了する。requeue、複数game、
    retry、reconnectはここでは扱わない(後続Issue)。
    """
    parser = build_arg_parser(prog="python -m lisjong_arena.riichilab.ranked")
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
    policy = profile.policy_factory()
    summary = build_runtime_summary(
        profile, mode="ranked", trace_path=trace_path, policy=policy
    )
    print(format_runtime_summary(summary))

    try:
        result = asyncio.run(run_ranked_game(policy, token, trace_path=trace_path))
    except RiichiLabClientError as error:
        print(
            f"RiichiLab ranked game failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print("RiichiLab ranked game completed")
    print(f"seat: {int(result.seat)}")
    print(f"requests: {result.requests_received}")
    print(f"responses: {result.responses_sent}")
    print(f"end_game: {'yes' if result.end_game_received else 'no'}")
    if result.scores is None:
        print("scores: unavailable")
    else:
        print("scores: " + ", ".join(str(score) for score in result.scores))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())


__all__ = ["RankedGameResult", "run_ranked_game"]
