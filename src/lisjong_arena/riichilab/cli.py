"""profile CLI(`validation.py` / `ranked.py`)が共有する引数解析とtrace path解決。

`profile.py`のprofile/credential解決を、CLI引数の形へ橋渡しするだけの薄い
layerとする。Policy契約、Session、Transportへ責務を持ち込まない。

本moduleは、Issue #44でlisjongへ実装されたcontract(`lisjong.riichilab_client.cli`)
をbehavior-preservingにArenaへcanonical migrationしたものである。引数形式、
trace path優先順位はmigration元contractを維持する。

ranked durable record acquisition(Issue #168)はranked CLIだけのoptionとして
opt-inで組み立てる。validation CLIやcontinuous ranked CLIへranked-only optionを
漏らさないため、`build_arg_parser()`の既定では`--record-dir`を追加しない。
"""

from __future__ import annotations

import argparse
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from lisjong_arena.riichilab.profile import (
    PROFILE_NAMES,
    RuntimeProfile,
    default_trace_path,
)

TRACE_PATH_ENV_VAR = "RIICHILAB_TRACE_PATH"


def build_arg_parser(
    *, prog: str, ranked_record: bool = False
) -> argparse.ArgumentParser:
    """`--profile`を必須とするCLI parserを作る。

    profile未指定はargparseの標準fail-closed挙動(non-zero exit、usageを
    stderrへ出力)へ委ねる。production等への暗黙fallbackはしない。
    """
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        required=True,
        help="起動するbot profile(lisjong-dev / lisjong-baseline / lisjong)",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "profileのruntime namespace配下(OSユーザーローカル領域)へ、"
            "secret-safeなprotocol traceを既定pathで保存する(既定OFF)"
        ),
    )
    parser.add_argument(
        "--trace-path",
        default=None,
        metavar="PATH",
        help=(
            "protocol traceの保存先を明示的に指定する。"
            f"{TRACE_PATH_ENV_VAR}環境変数、--traceより優先する"
        ),
    )
    if ranked_record:
        parser.add_argument(
            "--record-dir",
            default=None,
            metavar="PATH",
            help=(
                "completeした1半荘をdurable ranked record bundleとして"
                "このdirectory配下の新しいrecord directoryへ保存する"
                "(diagnostic traceとは別のcompleted research raw source)"
            ),
        )
    return parser


def resolve_ranked_record_path(record_dir_arg: str | None) -> Path | None:
    """`--record-dir`配下に、この1半荘専用のrecord directory pathを決める。

    `1 durable record = 1 RiichiLab ranked hanchan`を守るため、実行ごとに
    まだ存在しないpathを選ぶ。既存recordへのsilent overwrite / appendは
    行わない(実際の生成時にもdestinationの存在をfail closedで確認する)。

    filenameへはsecret-freeなUTC timestampとUUID4だけを使い、credential値・
    断片・machine usernameを含めない。
    """
    if not record_dir_arg:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(record_dir_arg) / f"{timestamp}-{uuid.uuid4().hex}"


def resolve_trace_path(
    profile: RuntimeProfile,
    *,
    trace_flag: bool,
    trace_path_arg: str | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """trace pathを次の優先順位で解決する。

    1. `--trace-path`による明示指定
    2. 既存`RIICHILAB_TRACE_PATH`環境変数(Issue #45との後方互換)
    3. `--trace`指定時のprofile既定path(secret-freeなtimestamp+UUID4)
    4. どれも指定がなければ`None`(trace無効、Issue #45のopt-in原則を維持)
    """
    if trace_path_arg:
        return trace_path_arg

    source = env if env is not None else os.environ
    env_value = source.get(TRACE_PATH_ENV_VAR)
    if env_value:
        return env_value

    if trace_flag:
        return str(default_trace_path(profile, env=source))

    return None


def parse_args(argv: Sequence[str] | None, *, prog: str) -> argparse.Namespace:
    parser = build_arg_parser(prog=prog)
    return parser.parse_args(argv)


__all__ = [
    "TRACE_PATH_ENV_VAR",
    "build_arg_parser",
    "parse_args",
    "resolve_ranked_record_path",
    "resolve_trace_path",
]
