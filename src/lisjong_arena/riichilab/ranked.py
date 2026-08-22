"""RiichiLab ranked 1半荘を起動するArena first-party entry point(Issue #15)。

RiichiLab implementation全体はまだ`lisjong`に物理的に存在するが、それを
組み合わせて実行する起点(composition / invocation)はArenaが所有する。

このmoduleはprofile定義、credential解決、trace path優先順位、transport、
`RankedSession`、possible-action validation等をコピー・再実装しない。
`lisjong.riichilab_client`のpublic helpers/primitivesをtemporaryに再利用
するだけの薄いcomposition layerである。

Usage:
    python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from lisjong.riichilab_client import RiichiLabClientError, run_ranked_game
from lisjong.riichilab_client.cli import build_arg_parser, resolve_trace_path
from lisjong.riichilab_client.profile import (
    ProfileError,
    build_runtime_summary,
    format_runtime_summary,
    resolve_credential,
    resolve_profile,
)


def _run_cli(argv: Sequence[str] | None = None) -> int:
    """`python -m lisjong_arena.riichilab.ranked --profile <name>`のentry point。

    `lisjong.riichilab_client.profile` / `cli`が解決したprofile・credential・
    trace pathをそのまま`run_ranked_game()`へ渡す。profile未指定・unknown
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
