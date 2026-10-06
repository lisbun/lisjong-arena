"""Bounded played_at-window metadata acquisition for the fixed Issue #170 top bots.

Issue #441の上位bot別比較では、自bot履歴にない同卓者identityを、上位bot側の
公開`/api/v1/bots/{bot_id}/games`から逆引きする。上位botは対局数が多く、Issue
#269の全履歴syncは`--max-games`と「取得中total不変」の前提に合わないため、
ここでは新しい順にpageを読み、指定windowより古い対局へ到達した時点で止める。

- 対象botは#170で固定した`TARGET_BOTS`だけに限る（bot一覧の総当たりをしない）
- metadataだけを取得し、MJAI logは取得しない
- 取得中に新しい対局が先頭へ追加されると、offset pagingでは同じ行がpage境界で
  重複し得る。完全に同じ内容の重複だけを許容し、内容の異なる重複はfail closed
- totalの減少（削除による取りこぼしの可能性）はfail closed
- `played_at`の非増加順を検証する。順序が崩れたら停止条件が成立しないためfail closed
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from lisjong_arena.riichilab_corpus.http import HttpTransport, get_with_bounded_retry
from lisjong_arena.riichilab_corpus.models import (
    TARGET_BOTS,
    canonical_json_bytes,
)
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    canonical_order,
    is_naive_played_at,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.pagination import (
    SelfHistoryPage,
    parse_self_history_page,
    self_history_page_url,
)

WINDOW_SCHEMA_ID = "lisjong-arena-riichilab-coplayer-window"
WINDOW_SCHEMA_VERSION = 1
TARGET_BOT_IDS = tuple(bot_id for bot_id, _ in TARGET_BOTS)


class CoplayerWindowError(SelfHistoryError):
    """The windowed acquisition input or server responses violate the contract."""


def parse_window_bound(value: str, context: str) -> datetime:
    """Parse one timezone-naive bound in the same wall clock as `played_at`."""
    if type(value) is not str or not value:
        raise CoplayerWindowError(f"{context} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoplayerWindowError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is not None:
        # RiichiLab played_at is timezone-naive; never guess a conversion.
        raise CoplayerWindowError(f"{context} must be timezone-naive like played_at")
    return parsed


def _played_at(game: SelfHistoryGame) -> datetime:
    if not is_naive_played_at(game.played_at):
        raise CoplayerWindowError(
            f"game {game.game_id} played_at is timezone-aware; refusing to compare "
            "it with a naive window"
        )
    return datetime.fromisoformat(game.played_at)


@dataclass(frozen=True, slots=True)
class CoplayerWindow:
    bot_id: int
    played_from: str
    played_to: str
    games: tuple[SelfHistoryGame, ...]
    page_count: int
    rows_scanned: int
    duplicate_rows: int
    first_total: int
    last_total: int
    reached_history_start: bool

    def identity(self) -> str:
        payload = canonical_json_bytes(
            {
                "bot_id": self.bot_id,
                "games": [game.to_value() for game in self.games],
                "played_from": self.played_from,
                "played_to": self.played_to,
            }
        )
        return hashlib.sha256(payload).hexdigest()

    def to_value(self, *, retrieved_at: str) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "bot_label": dict(TARGET_BOTS)[self.bot_id],
            "duplicate_rows": self.duplicate_rows,
            "first_total": self.first_total,
            "game_count": len(self.games),
            "games": [game.to_value() for game in self.games],
            "last_total": self.last_total,
            "page_count": self.page_count,
            "played_from": self.played_from,
            "played_to": self.played_to,
            "reached_history_start": self.reached_history_start,
            "retrieved_at": retrieved_at,
            "rows_scanned": self.rows_scanned,
            "schema": WINDOW_SCHEMA_ID,
            "schema_version": WINDOW_SCHEMA_VERSION,
            "window_identity": self.identity(),
        }


def fetch_coplayer_window(
    transport: HttpTransport,
    bot_id: int,
    *,
    played_from: str,
    played_to: str,
    max_pages: int,
    pacer: RequestPacer,
    timeout: float = 15.0,
    on_page: Callable[[SelfHistoryPage], None] | None = None,
) -> CoplayerWindow:
    """Walk newest-first pages until one row is older than `played_from`.

    The page that crosses the window start is read completely; nothing earlier
    is requested. Exceeding `max_pages` before that point fails closed.
    """
    if bot_id not in TARGET_BOT_IDS:
        raise CoplayerWindowError(
            f"bot {bot_id} is not one of the fixed Issue #170 target bots"
        )
    if type(max_pages) is not int or max_pages < 1:
        raise CoplayerWindowError("max_pages must be a positive integer")
    start = parse_window_bound(played_from, "played_from")
    end = parse_window_bound(played_to, "played_to")
    if start > end:
        raise CoplayerWindowError("played_from must not be after played_to")

    seen: dict[str, SelfHistoryGame] = {}
    selected: list[SelfHistoryGame] = []
    previous: datetime | None = None
    first_total: int | None = None
    last_total = 0
    rows_scanned = 0
    duplicate_rows = 0
    offset = 0
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    reached_start = False
    crossed = False

    while not crossed:
        if pages >= max_pages:
            raise CoplayerWindowError(
                f"bot {bot_id} window start {played_from} not reached within "
                f"{max_pages} pages; raise --max-pages explicitly after checking "
                "the declared total"
            )
        url = self_history_page_url(bot_id, offset=offset, cursor=cursor)
        pacer.before_request()
        response = get_with_bounded_retry(transport, url, timeout=timeout)
        page = parse_self_history_page(
            response.body,
            index=pages,
            url=url,
            requested_offset=offset,
            requested_cursor=cursor,
        )
        pages += 1
        if on_page is not None:
            on_page(page)

        if first_total is None:
            first_total = page.total
        elif page.total < last_total:
            raise CoplayerWindowError(
                f"bot {bot_id} total decreased from {last_total} to {page.total} "
                f"at page {page.index}; rows may have been skipped"
            )
        last_total = page.total

        for game in page.games:
            rows_scanned += 1
            earlier = seen.get(game.game_id)
            if earlier is not None:
                if earlier != game:
                    raise CoplayerWindowError(
                        f"bot {bot_id} game {game.game_id} reappeared with "
                        "different content"
                    )
                duplicate_rows += 1
                continue
            moment = _played_at(game)
            if previous is not None and moment > previous:
                raise CoplayerWindowError(
                    f"bot {bot_id} page {page.index} is not ordered newest-first "
                    f"at game {game.game_id}"
                )
            previous = moment
            seen[game.game_id] = game
            if moment < start:
                crossed = True
            elif moment <= end:
                selected.append(game)

        if crossed:
            break
        if not page.has_more:
            reached_start = True
            break
        assert page.next_cursor is not None
        if page.next_cursor in seen_cursors:
            raise CoplayerWindowError(
                f"bot {bot_id} page {page.index} repeats a previous cursor"
            )
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
        offset += page.limit

    assert first_total is not None
    return CoplayerWindow(
        bot_id=bot_id,
        played_from=start.isoformat(),
        played_to=end.isoformat(),
        games=canonical_order(tuple(selected)),
        page_count=pages,
        rows_scanned=rows_scanned,
        duplicate_rows=duplicate_rows,
        first_total=first_total,
        last_total=last_total,
        reached_history_start=reached_start,
    )


__all__ = [
    "CoplayerWindow",
    "CoplayerWindowError",
    "TARGET_BOT_IDS",
    "WINDOW_SCHEMA_ID",
    "fetch_coplayer_window",
    "parse_window_bound",
]
