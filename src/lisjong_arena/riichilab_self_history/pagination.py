"""Cursor + offset pagination over `/api/v1/bots/{bot_id}/games`.

cursorはopaque server tokenとして扱う。timestamp / game_id形式を独自parseして
再生成せず、受け取った`next_cursor`をそのままURL encodeして次requestへ渡す。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from lisjong_arena.riichilab_corpus.http import HttpTransport, get_with_bounded_retry
from lisjong_arena.riichilab_corpus.models import strict_json_loads
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    PAGE_SCHEMA_ID,
    REQUEST_LIMIT,
    SCHEMA_VERSION,
    SelfHistoryGame,
    self_history_api_url,
    strict_bool,
    strict_int,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer

_MAX_CURSOR_LENGTH = 512


def self_history_page_url(bot_id: int, *, offset: int, cursor: str | None) -> str:
    """Build one page URL, percent-encoding an opaque cursor without reshaping it."""
    if type(offset) is not int or offset < 0:
        raise SelfHistoryError("offset must be a non-negative integer")
    parameters: list[tuple[str, object]] = [
        ("limit", REQUEST_LIMIT),
        ("offset", offset),
    ]
    if cursor is not None:
        if type(cursor) is not str or not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise SelfHistoryError("cursor must be a non-empty bounded string")
        parameters.append(("cursor", cursor))
    # `quote_via=quote` keeps a space as `%20` rather than `+`, so the opaque
    # cursor survives any server-side query decoding unchanged.
    query = urlencode(parameters, quote_via=quote, safe="")
    return f"{self_history_api_url(bot_id)}?{query}"


@dataclass(frozen=True, slots=True)
class SelfHistoryPage:
    """One strictly parsed page plus its forward-compatible raw provenance."""

    index: int
    url: str
    requested_offset: int
    requested_cursor: str | None
    total: int
    offset: int
    limit: int
    has_more: bool
    next_cursor: str | None
    games: tuple[SelfHistoryGame, ...]
    raw: dict[str, object]

    def to_value(self) -> dict[str, object]:
        return {
            "index": self.index,
            "request": {
                "cursor": self.requested_cursor,
                "limit": REQUEST_LIMIT,
                "offset": self.requested_offset,
                "url": self.url,
            },
            "response": self.raw,
            "schema": PAGE_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
        }


def parse_self_history_page(
    payload: bytes,
    *,
    index: int,
    url: str,
    requested_offset: int,
    requested_cursor: str | None,
) -> SelfHistoryPage:
    """Strictly parse one page response and reject a request/response mismatch."""
    context = f"self-history page {index}"
    raw = strict_json_loads(payload, context)
    if type(raw) is not dict:
        raise SelfHistoryError(f"{context} must be a JSON object")
    if "ok" not in raw or strict_bool(raw["ok"], f"{context} ok") is not True:
        raise SelfHistoryError(f"{context} is not an ok response")
    data = raw.get("data")
    if type(data) is not dict:
        raise SelfHistoryError(f"{context} data must be an object")
    for key in ("total", "offset", "limit", "games", "has_more", "next_cursor"):
        if key not in data:
            raise SelfHistoryError(f"{context} data is missing {key}")

    total = strict_int(data["total"], f"{context} total")
    offset = strict_int(data["offset"], f"{context} offset")
    limit = strict_int(data["limit"], f"{context} limit")
    if total < 0 or offset < 0:
        raise SelfHistoryError(f"{context} declares a negative total or offset")
    if offset != requested_offset:
        raise SelfHistoryError(
            f"{context} returned offset {offset} for requested offset "
            f"{requested_offset}"
        )
    if limit != REQUEST_LIMIT:
        raise SelfHistoryError(
            f"{context} returned limit {limit} for requested limit {REQUEST_LIMIT}"
        )
    has_more = strict_bool(data["has_more"], f"{context} has_more")
    next_cursor = data["next_cursor"]
    if next_cursor is not None and (
        type(next_cursor) is not str
        or not next_cursor
        or len(next_cursor) > _MAX_CURSOR_LENGTH
    ):
        raise SelfHistoryError(f"{context} next_cursor must be null or a token")
    if has_more and next_cursor is None:
        raise SelfHistoryError(f"{context} declares has_more without a next_cursor")

    if type(data["games"]) is not list:
        raise SelfHistoryError(f"{context} games must be an array")
    if len(data["games"]) > REQUEST_LIMIT:
        raise SelfHistoryError(f"{context} returned more games than the request limit")
    games = tuple(
        SelfHistoryGame.from_api_value(item, f"{context} game {position}")
        for position, item in enumerate(data["games"])
    )
    page_ids = [game.game_id for game in games]
    if len(set(page_ids)) != len(page_ids):
        raise SelfHistoryError(f"{context} contains a duplicate game_id")
    if has_more and not games:
        raise SelfHistoryError(f"{context} is empty while declaring has_more")
    return SelfHistoryPage(
        index=index,
        url=url,
        requested_offset=requested_offset,
        requested_cursor=requested_cursor,
        total=total,
        offset=offset,
        limit=limit,
        has_more=has_more,
        next_cursor=next_cursor,
        games=games,
        raw=raw,
    )


class MaxGamesExceeded(SelfHistoryError):
    """The server declared more games than the mandatory `--max-games` bound."""


def fetch_self_history_pages(
    transport: HttpTransport,
    bot_id: int,
    *,
    max_games: int,
    pacer: RequestPacer,
    timeout: float = 15.0,
    on_page: Callable[[SelfHistoryPage], None] | None = None,
) -> tuple[SelfHistoryPage, ...]:
    """Walk every page serially and fail closed on any completeness violation.

    `max_games`はpage 0取得直後に評価する。declared totalが上限を超えた場合、
    page 1以降のrequestもMJAI acquisitionも開始しない。
    """
    if type(max_games) is not int or max_games < 1:
        raise SelfHistoryError("max_games must be a positive integer")

    pages: list[SelfHistoryPage] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    declared_total: int | None = None
    page_bound: int | None = None
    offset = 0
    cursor: str | None = None

    while True:
        index = len(pages)
        url = self_history_page_url(bot_id, offset=offset, cursor=cursor)
        pacer.before_request()
        response = get_with_bounded_retry(transport, url, timeout=timeout)
        page = parse_self_history_page(
            response.body,
            index=index,
            url=url,
            requested_offset=offset,
            requested_cursor=cursor,
        )

        if declared_total is None:
            declared_total = page.total
            if declared_total > max_games:
                raise MaxGamesExceeded(
                    f"bot {bot_id} declares {declared_total} games, above the "
                    f"required --max-games bound {max_games}; stopping before any "
                    "further page or MJAI acquisition"
                )
            page_bound = (
                1 if declared_total == 0 else -(-declared_total // REQUEST_LIMIT)
            )
        elif page.total != declared_total:
            raise SelfHistoryError(
                f"self-history total changed from {declared_total} to {page.total} "
                f"at page {index}; refusing to merge different snapshots"
            )

        duplicates = seen_ids & {game.game_id for game in page.games}
        if duplicates:
            raise SelfHistoryError(
                f"self-history page {index} repeats game_id "
                f"{sorted(duplicates)[0]} from an earlier page"
            )
        seen_ids.update(game.game_id for game in page.games)
        pages.append(page)
        if on_page is not None:
            on_page(page)

        if not page.has_more:
            break

        assert page.next_cursor is not None
        if page.next_cursor in seen_cursors:
            raise SelfHistoryError(
                f"self-history page {index} repeats a previous cursor; refusing to loop"
            )
        seen_cursors.add(page.next_cursor)
        assert page_bound is not None
        if len(pages) >= page_bound:
            raise SelfHistoryError(
                f"self-history declares has_more after {len(pages)} pages, above "
                f"the {page_bound} pages implied by total {declared_total}"
            )
        cursor = page.next_cursor
        offset += REQUEST_LIMIT

    assert declared_total is not None
    if len(seen_ids) != declared_total:
        raise SelfHistoryError(
            f"self-history collected {len(seen_ids)} unique games but the server "
            f"declared {declared_total}"
        )
    return tuple(pages)


__all__ = [
    "MaxGamesExceeded",
    "SelfHistoryPage",
    "fetch_self_history_pages",
    "parse_self_history_page",
    "self_history_page_url",
]
