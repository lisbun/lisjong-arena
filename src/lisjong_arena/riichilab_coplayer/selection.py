"""Fixed-rule top-bot selection from one public RiichiLab leaderboard snapshot.

Issue #441の比較先は、対局数が少ないbotや長く稼働していないbotを除いたうえで
選ぶ。規則は結果を見る前に固定し、ここで機械的に適用する。

- `rating >= MIN_RATING`（R1800ロードマップの目標水準）
- `total_games > MIN_TOTAL_GAMES_EXCLUSIVE`（1000局以下は除外）
- `last_played_at`が取得時点から`MAX_IDLE_DAYS`日以内（1か月以上前は除外）

leaderboardは1 pageあたり最大100件なので、rating閾値を下回るentryが出るまで
offsetを進めて読む（1 pass）。page間ではbot_idの重複と順位・ratingの逆転を
不整合として扱う。続けて同じpageをもう1 pass読み、両passのbot_id並びと各botの
判定が一致したときだけそのsnapshotを採用する。取得中の変動で一致しない場合は
`LEADERBOARD_MAX_ATTEMPTS`回まで2 passをやり直し、それでも一致しなければ
fail closedする。閾値の下まで届かない場合も選定漏れがあり得るので停止する。

`last_played_at`はtimezone-naiveのため、取得時刻のUTC wall clockと比べる。
30日の幅に対して時差は数時間なので、境界付近のbotは報告に残す。保存した
`selection.json`は、読込時に候補情報と基準日時から規則を再計算し、保存された
判定・選定IDと一致しなければfail closedする。
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
LEADERBOARD_MAX_ATTEMPTS = 3
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
    def from_value(cls, value: object, context: str) -> LeaderboardEntry:
        """Strictly read a stored entry: exactly the canonical field set."""
        expected = {
            "bot_id",
            "bot_name",
            "current_rank",
            "last_played_at",
            "rating",
            "total_games",
        }
        if type(value) is not dict or not expected <= set(value):
            raise CoplayerWindowError(f"{context} is missing entry fields")
        return cls.from_api_value({key: value[key] for key in expected}, context)

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


def decide(entry: LeaderboardEntry, *, reference_utc: datetime) -> Decision:
    """Apply the fixed rule to one entry at or above the rating threshold."""
    if entry.rating < MIN_RATING:
        raise CoplayerWindowError(f"bot {entry.bot_id} is below the rating threshold")
    cutoff = reference_utc - timedelta(days=MAX_IDLE_DAYS)
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
    return Decision(
        entry=entry, selected=not reasons, reasons=tuple(reasons), borderline=borderline
    )


def select_top_bots(
    entries: tuple[LeaderboardEntry, ...], *, reference_utc: datetime
) -> tuple[Decision, ...]:
    """Decide every entry at or above the threshold of one consistent snapshot."""
    if reference_utc.tzinfo is not None:
        raise CoplayerWindowError("reference_utc must be a naive UTC wall clock")
    if not entries or entries[-1].rating >= MIN_RATING:
        raise CoplayerWindowError(
            f"leaderboard does not reach below rating {MIN_RATING}; "
            "the snapshot may omit eligible bots"
        )
    return tuple(
        decide(entry, reference_utc=reference_utc)
        for entry in entries
        if entry.rating >= MIN_RATING
    )


def join_leaderboard_pages(
    pages: list[tuple[LeaderboardEntry, ...]],
) -> tuple[LeaderboardEntry, ...]:
    """Concatenate one pass's pages, refusing any cross-page inconsistency."""
    joined: list[LeaderboardEntry] = []
    seen: set[int] = set()
    for index, page in enumerate(pages):
        for entry in page:
            if entry.bot_id in seen:
                raise CoplayerWindowError(
                    f"leaderboard page {index} repeats bot {entry.bot_id} from an "
                    "earlier page"
                )
            if joined and (
                entry.rating > joined[-1].rating
                or entry.current_rank < joined[-1].current_rank
            ):
                raise CoplayerWindowError(
                    f"leaderboard order is inverted at page {index} bot {entry.bot_id}"
                )
            seen.add(entry.bot_id)
            joined.append(entry)
    return tuple(joined)


class LeaderboardUnstable(CoplayerWindowError):
    """Two consecutive passes disagreed; the leaderboard moved while reading."""


def fetch_leaderboard(
    transport: HttpTransport,
    *,
    pacer: RequestPacer,
    reference_utc: datetime,
    timeout: float = 15.0,
) -> tuple[list[dict[str, object]], tuple[LeaderboardEntry, ...]]:
    """Return a snapshot whose two consecutive passes agree.

    Agreement means the same bot_id sequence down to the first entry below the
    threshold and the same rule decision for every bot at or above it. Values
    from the second pass are used.
    """
    raws: list[dict[str, object]] = []

    def read_pass(attempt: int, label: str) -> tuple[LeaderboardEntry, ...]:
        pages: list[tuple[LeaderboardEntry, ...]] = []
        offset = 0
        for _ in range(LEADERBOARD_MAX_PAGES):
            url = leaderboard_url(offset)
            pacer.before_request()
            response = get_with_bounded_retry(transport, url, timeout=timeout)
            page = parse_leaderboard(response.body)
            raws.append(
                {
                    "attempt": attempt,
                    "offset": offset,
                    "pass": label,
                    "response_text": response.body.decode("utf-8"),
                    "url": url,
                }
            )
            pages.append(page)
            if (
                not page
                or page[-1].rating < MIN_RATING
                or len(page) < LEADERBOARD_LIMIT
            ):
                return join_leaderboard_pages(pages)
            offset += LEADERBOARD_LIMIT
        raise CoplayerWindowError(
            f"leaderboard still at or above {MIN_RATING} after "
            f"{LEADERBOARD_MAX_PAGES} pages"
        )

    def head(entries: tuple[LeaderboardEntry, ...]) -> tuple[LeaderboardEntry, ...]:
        # Down to and including the first entry below the threshold.
        for index, entry in enumerate(entries):
            if entry.rating < MIN_RATING:
                return entries[: index + 1]
        return entries

    def signature(entries: tuple[LeaderboardEntry, ...]) -> tuple[object, ...]:
        decisions = select_top_bots(entries, reference_utc=reference_utc)
        return (
            tuple(entry.bot_id for entry in head(entries)),
            tuple((d.entry.bot_id, d.selected, d.reasons) for d in decisions),
        )

    for attempt in range(LEADERBOARD_MAX_ATTEMPTS):
        first = read_pass(attempt, "a")
        second = read_pass(attempt, "b")
        if signature(first) == signature(second):
            return raws, head(second)
    raise LeaderboardUnstable(
        f"leaderboard changed between passes in all {LEADERBOARD_MAX_ATTEMPTS} "
        "attempts; retry later"
    )


def selection_value(
    entries: tuple[LeaderboardEntry, ...],
    *,
    retrieved_at: str,
    reference_utc: datetime,
) -> dict[str, object]:
    decisions = select_top_bots(entries, reference_utc=reference_utc)
    return {
        "candidates": [decision.to_value() for decision in decisions],
        "first_below_threshold": entries[-1].to_value(),
        "reference_utc": reference_utc.isoformat(),
        "retrieved_at": retrieved_at,
        "rule": _rule_value(),
        "schema": SELECTION_SCHEMA_ID,
        "schema_version": SELECTION_SCHEMA_VERSION,
        "selected_bot_ids": [d.entry.bot_id for d in decisions if d.selected],
    }


def _rule_value() -> dict[str, object]:
    return {
        "borderline_hours": BORDERLINE_HOURS,
        "max_idle_days": MAX_IDLE_DAYS,
        "min_rating": MIN_RATING,
        "min_total_games_exclusive": MIN_TOTAL_GAMES_EXCLUSIVE,
    }


def selected_bots_from_value(value: object) -> dict[int, str]:
    """Re-derive the selection from the stored candidates and verify it.

    The stored rule must equal this module's rule, every candidate must parse
    as a full entry, the snapshot must be ordered and end below the threshold,
    and recomputing each decision from `reference_utc` must reproduce the
    stored decision and `selected_bot_ids` exactly.
    """
    expected_keys = {
        "candidates",
        "first_below_threshold",
        "reference_utc",
        "retrieved_at",
        "rule",
        "schema",
        "schema_version",
        "selected_bot_ids",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise CoplayerWindowError("selection document fields are invalid")
    if (
        value["schema"] != SELECTION_SCHEMA_ID
        or value["schema_version"] != SELECTION_SCHEMA_VERSION
    ):
        raise CoplayerWindowError("not a coplayer selection document")
    if value["rule"] != _rule_value():
        raise CoplayerWindowError("selection was made under a different rule")
    strict_text(value["retrieved_at"], "selection retrieved_at")
    reference = datetime.fromisoformat(
        strict_text(value["reference_utc"], "selection reference_utc")
    )
    if reference.tzinfo is not None:
        raise CoplayerWindowError("selection reference_utc must be naive UTC")
    candidates = value["candidates"]
    if type(candidates) is not list:
        raise CoplayerWindowError("selection candidates must be an array")
    entries = tuple(
        LeaderboardEntry.from_value(item, f"selection candidate {index}")
        for index, item in enumerate(candidates)
    ) + (
        LeaderboardEntry.from_value(
            value["first_below_threshold"], "selection first_below_threshold"
        ),
    )
    join_leaderboard_pages([entries])
    decisions = select_top_bots(entries, reference_utc=reference)
    if [decision.to_value() for decision in decisions] != candidates:
        raise CoplayerWindowError(
            "stored candidate decisions disagree with the recomputed rule"
        )
    selected = {d.entry.bot_id: d.entry.bot_name for d in decisions if d.selected}
    if value["selected_bot_ids"] != list(selected):
        raise CoplayerWindowError("selected_bot_ids disagree with the recomputed rule")
    return selected


__all__ = [
    "LEADERBOARD_LIMIT",
    "LEADERBOARD_MAX_ATTEMPTS",
    "MAX_IDLE_DAYS",
    "MIN_RATING",
    "MIN_TOTAL_GAMES_EXCLUSIVE",
    "Decision",
    "LeaderboardEntry",
    "LeaderboardUnstable",
    "decide",
    "fetch_leaderboard",
    "join_leaderboard_pages",
    "leaderboard_url",
    "parse_leaderboard",
    "select_top_bots",
    "selected_bots_from_value",
    "selection_value",
]
