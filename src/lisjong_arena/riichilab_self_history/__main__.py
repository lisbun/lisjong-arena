"""Operator CLI for the Issue #269 self-history acquisition path.

```powershell
python -m lisjong_arena.riichilab_self_history sync `
  --bot-id 313 `
  --max-games 1000 `
  --output-dir C:\\Dev\\lisjong-artifacts\\riichilab\\lisjong-dev-history
```

`--max-games`は必須である。server declared totalが上限を超えた場合、page 1以降と
MJAI acquisitionへ進まずfail closedする。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from lisjong_arena.riichilab_corpus.http import StdlibHttpTransport
from lisjong_arena.riichilab_corpus.persistence import read_json
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.persistence import (
    REPORT_FILENAME,
    resolve_output_root,
)
from lisjong_arena.riichilab_self_history.sync import sync_self_history


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Arena-owned RiichiLab self-history acquisition for one operator-owned bot"
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser(
        "sync",
        help=(
            "acquire the full paginated metadata snapshot and only the missing "
            "server-side MJAI logs"
        ),
    )
    sync.add_argument("--bot-id", type=int, required=True)
    sync.add_argument(
        "--max-games",
        type=int,
        required=True,
        help="refuse the acquisition when the server declares more games than this",
    )
    sync.add_argument("--output-dir", type=Path, required=True)
    sync.add_argument("--timeout", type=float, default=15.0)

    report = commands.add_parser(
        "report", help="print the most recent local acquisition report"
    )
    report.add_argument("--output-dir", type=Path, required=True)
    return parser


def _print(value: object) -> None:
    print(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    )


def report_exit_code(report: object) -> int:
    """Return the CLI exit code a completion report implies.

    `sync`と`report`は同じreportを見るので、exit code contractもここ1箇所で定義
    する。metadata completenessとMJAI coverageは独立fieldのままだが、process exit
    はその両方がCOMPLETEのときだけ成功とする。status fieldが欠落・不正なreportは
    成功として扱わずfail closedする。
    """
    if type(report) is not dict:
        raise SelfHistoryError("self-history acquisition report must be an object")
    metadata = report.get("metadata")
    mjai = report.get("mjai")
    if type(metadata) is not dict or type(mjai) is not dict:
        raise SelfHistoryError(
            "self-history acquisition report is missing its status sections"
        )
    statuses = (
        report.get("overall_status"),
        metadata.get("status"),
        mjai.get("status"),
    )
    if any(type(status) is not str or not status for status in statuses):
        raise SelfHistoryError(
            "self-history acquisition report status fields are invalid"
        )
    return 0 if all(status == "COMPLETE" for status in statuses) else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "sync":
        report = sync_self_history(
            StdlibHttpTransport(),
            bot_id=arguments.bot_id,
            max_games=arguments.max_games,
            output_dir=arguments.output_dir,
            timeout=arguments.timeout,
        )
        _print(report)
        return report_exit_code(report)

    root = resolve_output_root(arguments.output_dir)
    report = read_json(root / REPORT_FILENAME, "self-history acquisition report")
    _print(report)
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "report_exit_code"]
