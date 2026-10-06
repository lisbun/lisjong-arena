"""Operator CLI for Issue #441 top-bot selection and windowed history acquisition.

```bash
OUT="$HOME/lisjong-artifacts/riichilab/i441-coplayer"
python -m lisjong_arena.riichilab_coplayer select --output-dir "$OUT/selection"
python -m lisjong_arena.riichilab_coplayer fetch \
  --selection "$OUT/selection/selection.json" \
  --played-from 2026-09-30T15:04:13 \
  --played-to 2026-10-01T07:19:36 \
  --max-pages 2000 \
  --output-dir "$OUT/windows"
```

credential-free public GETだけを使い、MJAI logは取得しない。出力先はGit worktree外の
存在しないか空のdirectoryに限る。`selection.json` / 各botの`window.json`は全検証後に
だけ書く。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena.riichilab_coplayer.selection import (
    fetch_leaderboard,
    select_top_bots,
    selected_bots_from_value,
    selection_value,
)
from lisjong_arena.riichilab_coplayer.window import (
    CoplayerWindowError,
    fetch_coplayer_window,
)
from lisjong_arena.riichilab_corpus.http import HttpTransport, StdlibHttpTransport
from lisjong_arena.riichilab_corpus.persistence import (
    ensure_outside_git_worktree,
    read_json,
    write_new_json,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.pagination import SelfHistoryPage

SELECTION_FILENAME = "selection.json"
LEADERBOARD_FILENAME = "leaderboard-pages.json"
WINDOW_FILENAME = "window.json"
PAGES_DIRECTORY = "pages"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #441 rule-based top-bot selection and window metadata"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="apply the fixed selection rule")
    select.add_argument("--output-dir", type=Path, required=True)
    select.add_argument("--timeout", type=float, default=15.0)

    fetch = commands.add_parser("fetch", help="acquire every selected bot's window")
    fetch.add_argument("--selection", type=Path, required=True)
    fetch.add_argument("--played-from", required=True)
    fetch.add_argument("--played-to", required=True)
    fetch.add_argument(
        "--max-pages",
        type=int,
        required=True,
        help="per bot: fail when the window start is not reached within this many",
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


def run_select(
    transport: HttpTransport,
    arguments: argparse.Namespace,
    *,
    pacer: RequestPacer,
    now: datetime | None = None,
) -> dict[str, object]:
    root = prepare_output_root(arguments.output_dir)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    raws, entries = fetch_leaderboard(transport, pacer=pacer, timeout=arguments.timeout)
    write_new_json(root / LEADERBOARD_FILENAME, raws)
    reference = moment.replace(tzinfo=None)
    value = selection_value(
        select_top_bots(entries, reference_utc=reference),
        retrieved_at=moment.isoformat().replace("+00:00", "Z"),
        reference_utc=reference,
    )
    write_new_json(root / SELECTION_FILENAME, value)
    return value


def run_fetch(
    transport: HttpTransport, arguments: argparse.Namespace, *, pacer: RequestPacer
) -> list[dict[str, object]]:
    selected = selected_bots_from_value(
        read_json(arguments.selection, "coplayer selection")
    )
    if not selected:
        raise CoplayerWindowError("selection contains no bot")
    root = prepare_output_root(arguments.output_dir)
    summaries = []
    for bot_id, label in selected.items():
        bot_root = root / f"bot-{bot_id}"

        def keep_page(page: SelfHistoryPage, bot_root: Path = bot_root) -> None:
            write_new_json(
                bot_root / PAGES_DIRECTORY / f"page-{page.index:05d}.json",
                page.to_value(),
            )

        window = fetch_coplayer_window(
            transport,
            bot_id,
            bot_label=label,
            played_from=arguments.played_from,
            played_to=arguments.played_to,
            max_pages=arguments.max_pages,
            pacer=pacer,
            timeout=arguments.timeout,
            on_page=keep_page,
        )
        value = window.to_value(
            retrieved_at=datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        write_new_json(bot_root / WINDOW_FILENAME, value)
        summaries.append({k: v for k, v in value.items() if k != "games"})
    return summaries


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    transport = StdlibHttpTransport()
    if arguments.command == "select":
        value = run_select(transport, arguments, pacer=RequestPacer())
        printed: object = {
            "selected_bot_ids": value["selected_bot_ids"],
            "candidates": value["candidates"],
        }
    else:
        printed = run_fetch(transport, arguments, pacer=RequestPacer())
    print(json.dumps(printed, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "prepare_output_root", "run_fetch", "run_select"]
