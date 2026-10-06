"""Operator CLI for Issue #441 windowed top-bot history acquisition.

```bash
python -m lisjong_arena.riichilab_coplayer fetch \
  --bot-id 120 \
  --played-from 2026-09-30T15:04:13 \
  --played-to 2026-10-01T07:19:36 \
  --max-pages 2000 \
  --output-dir "$HOME/lisjong-artifacts/riichilab/i441-coplayer/bot-120"
```

credential-free public GETだけを使い、MJAI logは取得しない。出力先はGit worktree外の
存在しないか空のdirectoryに限る。`window.json`は全pageの検証後にだけ書く。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from lisjong_arena.riichilab_coplayer.window import (
    CoplayerWindowError,
    fetch_coplayer_window,
)
from lisjong_arena.riichilab_corpus.http import HttpTransport, StdlibHttpTransport
from lisjong_arena.riichilab_corpus.models import utc_now_text
from lisjong_arena.riichilab_corpus.persistence import (
    ensure_outside_git_worktree,
    write_new_json,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.pagination import SelfHistoryPage

WINDOW_FILENAME = "window.json"
PAGES_DIRECTORY = "pages"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #441 played_at-window metadata for one fixed top bot"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="acquire one bot's window metadata")
    fetch.add_argument("--bot-id", type=int, required=True)
    fetch.add_argument("--played-from", required=True)
    fetch.add_argument("--played-to", required=True)
    fetch.add_argument(
        "--max-pages",
        type=int,
        required=True,
        help="fail when the window start is not reached within this many pages",
    )
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch.add_argument("--timeout", type=float, default=15.0)
    return parser


def prepare_output_root(output_dir: Path) -> Path:
    root = ensure_outside_git_worktree(output_dir)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise CoplayerWindowError(f"output directory must be absent or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_fetch(
    transport: HttpTransport, arguments: argparse.Namespace, *, pacer: RequestPacer
) -> dict[str, object]:
    root = prepare_output_root(arguments.output_dir)

    def keep_page(page: SelfHistoryPage) -> None:
        write_new_json(
            root / PAGES_DIRECTORY / f"page-{page.index:05d}.json", page.to_value()
        )

    window = fetch_coplayer_window(
        transport,
        arguments.bot_id,
        played_from=arguments.played_from,
        played_to=arguments.played_to,
        max_pages=arguments.max_pages,
        pacer=pacer,
        timeout=arguments.timeout,
        on_page=keep_page,
    )
    value = window.to_value(retrieved_at=utc_now_text())
    write_new_json(root / WINDOW_FILENAME, value)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    value = run_fetch(StdlibHttpTransport(), arguments, pacer=RequestPacer())
    summary = {key: item for key, item in value.items() if key != "games"}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "prepare_output_root", "run_fetch"]
