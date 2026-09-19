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
        return 0 if report["overall_status"] == "COMPLETE" else 1

    root = resolve_output_root(arguments.output_dir)
    _print(read_json(root / REPORT_FILENAME, "self-history acquisition report"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
