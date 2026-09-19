"""Wholly synthetic Issue #269 self-history fixtures; no live RiichiLab access."""

from __future__ import annotations

import gzip
import json
from urllib.parse import parse_qs, urlsplit

from lisjong_arena.riichilab_corpus.http import HttpResponse

BOT_ID = 313


def api_game(
    game_id: str,
    played_at: str,
    *,
    seat: int = 0,
    rank: int = 1,
    score: int = 32000,
    game_type: str = "ranked",
    player_count: int = 4,
    rating_before: object = 1500.5,
    rating_delta: object = 12.25,
    mu_before: object = 25.0,
    is_disconnected: bool = False,
    is_penalized: bool = False,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one raw API game object, including forward-compatible extra fields."""
    value: dict[str, object] = {
        "game_id": game_id,
        "game_type": game_type,
        "player_count": player_count,
        "played_at": played_at,
        "seat": seat,
        "rank": rank,
        "score": score,
        "rating_before": rating_before,
        "rating_delta": rating_delta,
        "mu_before": mu_before,
        "is_disconnected": is_disconnected,
        "is_penalized": is_penalized,
    }
    if extra is not None:
        value.update(extra)
    return value


def page_payload(
    *,
    total: int,
    offset: int,
    games: list[dict[str, object]],
    has_more: bool,
    next_cursor: str | None,
    limit: int = 20,
    ok: object = True,
    extra_data: dict[str, object] | None = None,
) -> bytes:
    data: dict[str, object] = {
        "total": total,
        "offset": offset,
        "limit": limit,
        "games": games,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
    if extra_data is not None:
        data.update(extra_data)
    return json.dumps({"ok": ok, "data": data}, separators=(",", ":")).encode("utf-8")


def synthetic_mjai(
    *,
    names: object = None,
    lifecycle_valid: bool = True,
    marker: str = "5m",
) -> bytes:
    """Build a minimal valid four-seat MJAI log, or an invalid-lifecycle variant."""
    seat_names = ["seat0", "seat1", "seat2", "seat3"] if names is None else names
    hands = [["1m"] * 13, ["1p"] * 13, ["1s"] * 13, ["E"] * 13]
    events: list[dict[str, object]] = [
        {"type": "start_game", "names": seat_names, "marker": marker},
        {"type": "start_kyoku", "tehais": hands},
        {"type": "tsumo", "actor": 0, "pai": "5m"},
        {"type": "dahai", "actor": 0, "pai": "5m"},
        {"type": "ryukyoku"},
        {"type": "end_kyoku"},
        {"type": "end_game"},
    ]
    if not lifecycle_valid:
        # A second start_kyoku without an intervening end_kyoku is out of order.
        events.insert(2, {"type": "start_kyoku", "tehais": hands})
    text = "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def malformed_jsonl_gzip() -> bytes:
    return gzip.compress(b'{"type": "start_game"}\nnot json\n', mtime=0)


class RecordingTransport:
    """A fake transport that serves canned responses and records every URL."""

    def __init__(self, responses: dict[str, object] | None = None):
        self.responses: dict[str, object] = dict(responses or {})
        self.calls: list[str] = []

    def route(self, url: str, outcome: object) -> None:
        self.responses[url] = outcome

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected request: {url}")
        outcome = self.responses[url]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, HttpResponse):
            return outcome
        assert isinstance(outcome, bytes)
        return HttpResponse(200, {}, outcome)

    @property
    def metadata_calls(self) -> list[str]:
        return [url for url in self.calls if "/games?" in url]

    @property
    def log_calls(self) -> list[str]:
        return [url for url in self.calls if url.endswith(".jsonl.gz")]


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def collect_sleeps(store: list[float]):
    def sleeper(seconds: float) -> None:
        store.append(seconds)

    return sleeper
