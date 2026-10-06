"""Fixed-rule top-bot selection from one public RiichiLab leaderboard snapshot.

Issue #441の比較先は、対局数が少ないbotや長く稼働していないbotを除いたうえで
選ぶ。規則は結果を見る前に固定し、ここで機械的に適用する。

- `rating >= MIN_RATING`（R1800ロードマップの目標水準）
- `total_games > MIN_TOTAL_GAMES_EXCLUSIVE`（1000局以下は除外）
- `last_played_at`が取得時点から`MAX_IDLE_DAYS`日以内（1か月以上前は除外）

leaderboardは1 pageあたり最大100件なので、rating閾値を下回るentryが出るまで
offsetを進めて読み、最後に先頭pageを再取得する（取得中にpage境界を上へ越えた
botを拾うため）。同じbotは最後に観測した値を使う。閾値の下まで届かない場合は
選定漏れがあり得るのでfail closedする。`last_played_at`はtimezone-naiveのため、取得時刻のUTC wall
clockと比べる。30日の幅に対して時差は数時間なので、境界付近のbotは報告に残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from lisjong_arena.riichilab_coplayer.window import CoplayerWindowError
from lisjong_arena.riichilab_corpus.http import HttpTransport, get_with_bounded_retry
from lisjong_arena.riichilab_corpus.models import API_BASE_URL, strict_json_loads
from lisjong_arena.riichilab_self_history.models import (
    strict_bool,
    strict_int,
    strict_number,
    strict_text,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer

SELECTION_SCHEMA_ID = "lisjong-arena-riichilab-coplayer-selection"
SELECTION_SCHEMA_VERSION = 1
LEADERBOARD_LIMIT = 100
LEADERBOARD_MAX_PAGES = 10
MIN_RATING = 1800
MIN_TOTAL_GAMES_EXCLUSIVE = 1000
MAX_IDLE_DAYS = 30
BORDERLINE_HOURS = 24


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    bot_id: int
    bot_name: str
    rating: float
    total_games: int
    last_played_at: str | None
    current_rank: int

    @classmethod
    def from_api_value(cls, value: object, context: str) -> LeaderboardEntry:
        if type(value) is not dict:
            raise CoplayerWindowError(f"{context} must be an object")
        for key in (
            "bot_id",
            "bot_name",
            "rating",
            "total_games",
            "last_played_at",
            "current_rank",
        ):
            if key not in value:
                raise CoplayerWindowError(f"{context} is missing {key}")
        bot_id = strict_int(value["bot_id"], f"{context} bot_id")
        if bot_id <= 0:
            raise CoplayerWindowError(f"{context} bot_id must be positive")
        total_games = strict_int(value["total_games"], f"{context} total_games")
        if total_games < 0:
            raise CoplayerWindowError(f"{context} total_games must not be negative")
        last = value["last_played_at"]
        if last is not None:
            last = _naive(strict_text(last, f"{context} last_played_at"), context)
        return cls(
            bot_id=bot_id,
            # Names are free text; keep them only for the operator report.
            bot_name=strict_text(value["bot_name"], f"{context} bot_name"),
            rating=strict_number(value["rating"], f"{context} rating"),
            total_games=total_games,
            last_played_at=last,
            current_rank=strict_int(value["current_rank"], f"{context} current_rank"),
        )

    def to_value(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "current_rank": self.current_rank,
            "last_played_at": self.last_played_at,
            "rating": self.rating,
            "total_games": self.total_games,
        }


def _naive(text: str, context: str) -> str:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CoplayerWindowError(f"{context} last_played_at is not ISO-8601") from exc
    if parsed.tzinfo is not None:
        raise CoplayerWindowError(f"{context} last_played_at is unexpectedly aware")
    return parsed.isoformat()


def leaderboard_url(offset: int) -> str:
    return f"{API_BASE_URL}/leaderboard?limit={LEADERBOARD_LIMIT}&offset={offset}"


def parse_leaderboard(payload: bytes) -> tuple[LeaderboardEntry, ...]:
    """Parse one leaderboard page; an empty page is valid past the end."""
    raw = strict_json_loads(payload, "leaderboard")
    if type(raw) is not dict or "ok" not in raw:
        raise CoplayerWindowError("leaderboard must be an ok-wrapped object")
    if strict_bool(raw["ok"], "leaderboard ok") is not True:
        raise CoplayerWindowError("leaderboard is not an ok response")
    data = raw.get("data")
    if type(data) is not list or len(data) > LEADERBOARD_LIMIT:
        raise CoplayerWindowError("leaderboard data must be a bounded array")
    entries = tuple(
        LeaderboardEntry.from_api_value(item, f"leaderboard entry {index}")
        for index, item in enumerate(data)
    )
    if len({entry.bot_id for entry in entries}) != len(entries):
        raise CoplayerWindowError("leaderboard repeats a bot_id")
    ratings = [entry.rating for entry in entries]
    if any(later > earlier for earlier, later in zip(ratings, ratings[1:])):
        raise CoplayerWindowError("leaderboard is not ordered by rating")
    return entries


@dataclass(frozen=True, slots=True)
class Decision:
    entry: LeaderboardEntry
    selected: bool
    reasons: tuple[str, ...]
    borderline: bool

    def to_value(self) -> dict[str, object]:
        return {
            **self.entry.to_value(),
            "borderline_idle": self.borderline,
            "excluded_by": list(self.reasons),
            "selected": self.selected,
        }


def select_top_bots(
    entries: tuple[LeaderboardEntry, ...], *, reference_utc: datetime
) -> tuple[Decision, ...]:
    """Apply the fixed rule to every entry at or above the rating threshold."""
    if reference_utc.tzinfo is not None:
        raise CoplayerWindowError("reference_utc must be a naive UTC wall clock")
    if not entries or entries[-1].rating >= MIN_RATING:
        raise CoplayerWindowError(
            f"leaderboard does not reach below rating {MIN_RATING}; "
            "the snapshot may omit eligible bots"
        )
    cutoff = reference_utc - timedelta(days=MAX_IDLE_DAYS)
    decisions = []
    for entry in entries:
        if entry.rating < MIN_RATING:
            continue
        reasons = []
        if entry.total_games <= MIN_TOTAL_GAMES_EXCLUSIVE:
            reasons.append("total_games<=1000")
        borderline = False
        if entry.last_played_at is None:
            reasons.append("never_played")
        else:
            last = datetime.fromisoformat(entry.last_played_at)
            if last < cutoff:
                reasons.append(f"idle>{MAX_IDLE_DAYS}d")
            borderline = abs(last - cutoff) <= timedelta(hours=BORDERLINE_HOURS)
        decisions.append(
            Decision(
                entry=entry,
                selected=not reasons,
                reasons=tuple(reasons),
                borderline=borderline,
            )
        )
    return tuple(decisions)


def merge_leaderboard_pages(
    pages: list[tuple[LeaderboardEntry, ...]],
) -> tuple[LeaderboardEntry, ...]:
    """Union pages in fetch order; a later observation of a bot replaces earlier."""
    latest: dict[int, LeaderboardEntry] = {}
    for page in pages:
        for entry in page:
            latest[entry.bot_id] = entry
    return tuple(sorted(latest.values(), key=lambda e: (-e.rating, e.bot_id)))


def fetch_leaderboard(
    transport: HttpTransport, *, pacer: RequestPacer, timeout: float = 15.0
) -> tuple[list[dict[str, object]], tuple[LeaderboardEntry, ...]]:
    """Read pages until one drops below the threshold, then re-read page 0."""
    raws: list[dict[str, object]] = []
    pages: list[tuple[LeaderboardEntry, ...]] = []

    def read(offset: int) -> tuple[LeaderboardEntry, ...]:
        url = leaderboard_url(offset)
        pacer.before_request()
        response = get_with_bounded_retry(transport, url, timeout=timeout)
        page = parse_leaderboard(response.body)
        raws.append(
            {
                "offset": offset,
                "url": url,
                "response_text": response.body.decode("utf-8"),
            }
        )
        pages.append(page)
        return page

    offset = 0
    for _ in range(LEADERBOARD_MAX_PAGES):
        page = read(offset)
        if not page or page[-1].rating < MIN_RATING or len(page) < LEADERBOARD_LIMIT:
            break
        offset += LEADERBOARD_LIMIT
    else:
        raise CoplayerWindowError(
            f"leaderboard still at or above {MIN_RATING} after "
            f"{LEADERBOARD_MAX_PAGES} pages"
        )
    if offset:
        read(0)
    return raws, merge_leaderboard_pages(pages)


def selection_value(
    decisions: tuple[Decision, ...], *, retrieved_at: str, reference_utc: datetime
) -> dict[str, object]:
    return {
        "candidates": [decision.to_value() for decision in decisions],
        "reference_utc": reference_utc.isoformat(),
        "retrieved_at": retrieved_at,
        "rule": {
            "max_idle_days": MAX_IDLE_DAYS,
            "min_rating": MIN_RATING,
            "min_total_games_exclusive": MIN_TOTAL_GAMES_EXCLUSIVE,
        },
        "schema": SELECTION_SCHEMA_ID,
        "schema_version": SELECTION_SCHEMA_VERSION,
        "selected_bot_ids": [d.entry.bot_id for d in decisions if d.selected],
    }


def selected_bots_from_value(value: object) -> dict[int, str]:
    """Read a stored selection back, fail closed on any schema drift."""
    if (
        type(value) is not dict
        or value.get("schema") != SELECTION_SCHEMA_ID
        or value.get("schema_version") != SELECTION_SCHEMA_VERSION
    ):
        raise CoplayerWindowError("not a coplayer selection document")
    candidates = value.get("candidates")
    ids = value.get("selected_bot_ids")
    if type(candidates) is not list or type(ids) is not list:
        raise CoplayerWindowError("selection is missing candidates or selected ids")
    names = {
        item["bot_id"]: item["bot_name"]
        for item in candidates
        if type(item) is dict and item.get("selected") is True
    }
    if list(names) != ids:
        raise CoplayerWindowError("selected_bot_ids disagree with the candidates")
    return names


__all__ = [
    "LEADERBOARD_LIMIT",
    "MAX_IDLE_DAYS",
    "MIN_RATING",
    "MIN_TOTAL_GAMES_EXCLUSIVE",
    "Decision",
    "LeaderboardEntry",
    "fetch_leaderboard",
    "leaderboard_url",
    "merge_leaderboard_pages",
    "parse_leaderboard",
    "select_top_bots",
    "selected_bots_from_value",
    "selection_value",
]
