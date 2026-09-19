"""Issue #269 self-history value contracts.

このmoduleはoperator-owned self BotのRiichiLab game metadataだけを扱う。
Issue #170の`TARGET_BOTS` / `RecentGamesSnapshot` / `build_snapshot()` /
`MAX_ACQUISITION_CEILING`とはschema identityを共有しない。共有するのは
`strict_json_loads` / `canonical_json_bytes` / `sha256_bytes` /
`validate_game_id` / `normalize_played_at`等のneutral primitiveだけである。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from lisjong_arena.riichilab_corpus.models import (
    API_BASE_URL,
    LOG_BASE_URL,
    CorpusError,
    canonical_json_bytes,
    normalize_played_at,
    normalize_timestamp,
    sha256_bytes,
    validate_game_id,
)
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError

HISTORY_SCHEMA_ID = "lisjong-arena-riichilab-self-history"
PAGE_SCHEMA_ID = "lisjong-arena-riichilab-self-history-page"
REPORT_SCHEMA_ID = "lisjong-arena-riichilab-self-history-report"
SCHEMA_VERSION = 1

# RiichiLab Web UIがv1で送るrequest limit。response `data.limit`はこの値と
# 一致しなければならない。caller-configurableにしない。
REQUEST_LIMIT = 20

# canonical orderはpagination順ではなくlogical metadataから固定する。
CANONICAL_ORDERING = "played_at,game_id"

_MAX_TEXT_LENGTH = 128

_T = TypeVar("_T")


def self_history_api_url(bot_id: int) -> str:
    """Return the paginated self-history endpoint for one operator-owned bot."""
    if type(bot_id) is not int or bot_id <= 0:
        raise SelfHistoryError("bot_id must be a positive integer")
    return f"{API_BASE_URL}/bots/{bot_id}/games"


def strict_text(value: object, context: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TEXT_LENGTH:
        raise SelfHistoryError(f"{context} must be a non-empty bounded string")
    return value


def strict_bool(value: object, context: str) -> bool:
    """Accept only a JSON boolean; `0` / `1` are a Python/JSON type confusion."""
    if type(value) is not bool:
        raise SelfHistoryError(f"{context} must be a JSON boolean")
    return value


def strict_int(value: object, context: str) -> int:
    """Accept only a JSON integer; `bool` is a Python `int` subclass and is rejected."""
    if type(value) is not int:
        raise SelfHistoryError(f"{context} must be a JSON integer")
    return value


def strict_number(value: object, context: str) -> float:
    """Accept a JSON integer or float and canonicalize it to `float`.

    RiichiLab rating / mu fieldはreal-valued quantityである。serverが整数値を
    `1500`として送る場合と`1500.0`として送る場合の両方があり得るため、狭すぎる
    int-only contractを推測で固定せず、canonical modelでは常に`float`へ寄せる。
    `strict_json_loads`がNaN / Infinityを既に拒否している。
    """
    if type(value) is bool or type(value) not in (int, float):
        raise SelfHistoryError(f"{context} must be a JSON number")
    return float(value)


def is_naive_played_at(value: str) -> bool:
    """Report whether a canonical `played_at` carries no timezone."""
    return datetime.fromisoformat(value).tzinfo is None


def _reused(validator: Callable[..., _T], *arguments: object) -> _T:
    """Run a reused #170 primitive and surface one error type for field checks.

    低レベルprimitiveは`CorpusError`を送出する。typed model側のfield contract
    violationは呼び出し側から見て1種類であるべきなので、ここで`SelfHistoryError`
    へ揃える。transport / gzip / MJAI primitiveのerrorはacquisition境界で
    `ACQUISITION_ERRORS`として扱うため、ここでは変換しない。
    """
    try:
        return validator(*arguments)
    except CorpusError as exc:
        raise SelfHistoryError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SelfHistoryGame:
    """One operator-owned participation row from `/api/v1/bots/{bot_id}/games`.

    field setはIssue #269のtyped canonical modelである。API responseのunknown
    extra fieldはraw page provenanceにだけ残し、ここへ暗黙に追加しない。
    """

    game_id: str
    game_type: str
    player_count: int
    played_at: str
    seat: int
    rank: int
    score: int
    rating_before: float
    rating_delta: float
    mu_before: float
    is_disconnected: bool
    is_penalized: bool

    def __post_init__(self) -> None:
        _reused(validate_game_id, self.game_id)
        strict_text(self.game_type, "game_type")
        if type(self.player_count) is not int or not 3 <= self.player_count <= 4:
            raise SelfHistoryError("player_count must be an integer from 3 through 4")
        object.__setattr__(
            self,
            "played_at",
            _reused(normalize_played_at, self.played_at, "played_at"),
        )
        if type(self.seat) is not int or not 0 <= self.seat < self.player_count:
            raise SelfHistoryError("seat must be an integer within the table size")
        if type(self.rank) is not int or not 1 <= self.rank <= self.player_count:
            raise SelfHistoryError("rank must be an integer within the table size")
        strict_int(self.score, "score")
        for field in ("rating_before", "rating_delta", "mu_before"):
            object.__setattr__(self, field, strict_number(getattr(self, field), field))
        strict_bool(self.is_disconnected, "is_disconnected")
        strict_bool(self.is_penalized, "is_penalized")

    @property
    def order_key(self) -> tuple[str, str]:
        return (self.played_at, self.game_id)

    def to_value(self) -> dict[str, object]:
        return {
            "game_id": self.game_id,
            "game_type": self.game_type,
            "is_disconnected": self.is_disconnected,
            "is_penalized": self.is_penalized,
            "mu_before": self.mu_before,
            "played_at": self.played_at,
            "player_count": self.player_count,
            "rank": self.rank,
            "rating_before": self.rating_before,
            "rating_delta": self.rating_delta,
            "score": self.score,
            "seat": self.seat,
        }

    @classmethod
    def from_value(cls, value: object) -> SelfHistoryGame:
        expected = {
            "game_id",
            "game_type",
            "is_disconnected",
            "is_penalized",
            "mu_before",
            "played_at",
            "player_count",
            "rank",
            "rating_before",
            "rating_delta",
            "score",
            "seat",
        }
        if type(value) is not dict or set(value) != expected:
            raise SelfHistoryError("self-history game fields are invalid")
        return cls(
            game_id=value["game_id"],
            game_type=value["game_type"],
            player_count=value["player_count"],
            played_at=value["played_at"],
            seat=value["seat"],
            rank=value["rank"],
            score=value["score"],
            rating_before=value["rating_before"],
            rating_delta=value["rating_delta"],
            mu_before=value["mu_before"],
            is_disconnected=value["is_disconnected"],
            is_penalized=value["is_penalized"],
        )

    @classmethod
    def from_api_value(cls, value: object, context: str) -> SelfHistoryGame:
        """Project one raw API game object onto the typed canonical model."""
        if type(value) is not dict:
            raise SelfHistoryError(f"{context} must be an object")
        required = (
            "game_id",
            "game_type",
            "player_count",
            "played_at",
            "seat",
            "rank",
            "score",
            "rating_before",
            "rating_delta",
            "mu_before",
            "is_disconnected",
            "is_penalized",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise SelfHistoryError(
                f"{context} is missing required fields: {', '.join(sorted(missing))}"
            )
        # Unknown extra API fields stay in the raw page provenance only.
        return cls(
            game_id=value["game_id"],
            game_type=value["game_type"],
            player_count=strict_int(value["player_count"], f"{context} player_count"),
            played_at=value["played_at"],
            seat=strict_int(value["seat"], f"{context} seat"),
            rank=strict_int(value["rank"], f"{context} rank"),
            score=strict_int(value["score"], f"{context} score"),
            rating_before=strict_number(
                value["rating_before"], f"{context} rating_before"
            ),
            rating_delta=strict_number(
                value["rating_delta"], f"{context} rating_delta"
            ),
            mu_before=strict_number(value["mu_before"], f"{context} mu_before"),
            is_disconnected=strict_bool(
                value["is_disconnected"], f"{context} is_disconnected"
            ),
            is_penalized=strict_bool(value["is_penalized"], f"{context} is_penalized"),
        )


def canonical_order(games: tuple[SelfHistoryGame, ...]) -> tuple[SelfHistoryGame, ...]:
    """Order games by the fixed `(played_at, game_id)` contract.

    `game_id`はhistory内でuniqueなので、この順序はpagination page boundaryから
    独立したstrict total orderである。
    """
    return tuple(sorted(games, key=lambda game: game.order_key))


def history_identity(bot_id: int, games: tuple[SelfHistoryGame, ...]) -> str:
    """Derive the history identity from canonical logical metadata only.

    output path、retrieval time、pagination page boundaryはinputに含めない。
    """
    logical = {
        "bot_id": bot_id,
        "games": [game.to_value() for game in canonical_order(games)],
        "schema": HISTORY_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
    }
    return sha256_bytes(canonical_json_bytes(logical))


@dataclass(frozen=True, slots=True)
class SelfHistory:
    """The completed canonical self-history for one operator-owned bot."""

    bot_id: int
    retrieved_at: str
    source_api: str
    declared_total: int
    games: tuple[SelfHistoryGame, ...]
    identity: str

    @property
    def game_ids(self) -> tuple[str, ...]:
        return tuple(game.game_id for game in self.games)

    @property
    def oldest_played_at(self) -> str | None:
        return self.games[0].played_at if self.games else None

    @property
    def newest_played_at(self) -> str | None:
        return self.games[-1].played_at if self.games else None

    def to_value(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "declared_total": self.declared_total,
            "game_count": len(self.games),
            "games": [game.to_value() for game in self.games],
            "history_identity": self.identity,
            "ordering": CANONICAL_ORDERING,
            "retrieved_at": self.retrieved_at,
            "schema": HISTORY_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "source_api": self.source_api,
        }


def build_history(
    *,
    bot_id: int,
    retrieved_at: str,
    declared_total: int,
    games: tuple[SelfHistoryGame, ...],
) -> SelfHistory:
    """Construct a fail-closed canonical history from validated metadata."""
    source_api = self_history_api_url(bot_id)
    normalized_retrieved_at = _reused(normalize_timestamp, retrieved_at, "retrieved_at")
    if type(declared_total) is not int or declared_total < 0:
        raise SelfHistoryError("declared_total must be a non-negative integer")
    if type(games) is not tuple:
        raise SelfHistoryError("games must be a tuple")
    ordered = canonical_order(games)
    game_ids = [game.game_id for game in ordered]
    if len(set(game_ids)) != len(game_ids):
        raise SelfHistoryError("self-history contains duplicate game_id values")
    if len(ordered) != declared_total:
        raise SelfHistoryError(
            f"self-history has {len(ordered)} games but the server declared "
            f"{declared_total}"
        )
    awareness = {is_naive_played_at(game.played_at) for game in ordered}
    if len(awareness) > 1:
        raise SelfHistoryError(
            "self-history mixes timezone-naive and timezone-aware played_at values"
        )
    return SelfHistory(
        bot_id=bot_id,
        retrieved_at=normalized_retrieved_at,
        source_api=source_api,
        declared_total=declared_total,
        games=ordered,
        identity=history_identity(bot_id, ordered),
    )


def history_from_value(value: object) -> SelfHistory:
    """Strictly read back a published `history.json` document."""
    expected = {
        "bot_id",
        "declared_total",
        "game_count",
        "games",
        "history_identity",
        "ordering",
        "retrieved_at",
        "schema",
        "schema_version",
        "source_api",
    }
    if type(value) is not dict or set(value) != expected:
        raise SelfHistoryError("self-history document fields are invalid")
    if value["schema"] != HISTORY_SCHEMA_ID or value["schema_version"] != (
        SCHEMA_VERSION
    ):
        raise SelfHistoryError("self-history schema is unsupported")
    if value["ordering"] != CANONICAL_ORDERING:
        raise SelfHistoryError("self-history ordering contract is unsupported")
    if type(value["games"]) is not list:
        raise SelfHistoryError("self-history games must be an array")
    games = tuple(SelfHistoryGame.from_value(item) for item in value["games"])
    if games != canonical_order(games):
        raise SelfHistoryError("self-history games are not in canonical order")
    history = build_history(
        bot_id=strict_int(value["bot_id"], "bot_id"),
        retrieved_at=value["retrieved_at"],
        declared_total=strict_int(value["declared_total"], "declared_total"),
        games=games,
    )
    if history.retrieved_at != value["retrieved_at"]:
        raise SelfHistoryError("self-history retrieved_at must use canonical UTC form")
    if value["source_api"] != history.source_api:
        raise SelfHistoryError("self-history source API does not match its bot_id")
    if strict_int(value["game_count"], "game_count") != len(history.games):
        raise SelfHistoryError("self-history game count mismatch")
    if value["history_identity"] != history.identity:
        raise SelfHistoryError("self-history identity mismatch")
    return history


def resolve_self_log_url(game: SelfHistoryGame) -> str:
    """Resolve the server-side MJAI URL from the canonical `played_at` date.

    calendar dateはserver-provided / normalized `played_at`からそのまま作り、
    timezone変換を行わない。これは2026-09-19 manual 257-game acquisitionで
    観測されたcontractである。
    """
    date = game.played_at[:10].replace("-", "/")
    return f"{LOG_BASE_URL}/{date}/{game.game_id}.jsonl.gz"


__all__ = [
    "CANONICAL_ORDERING",
    "HISTORY_SCHEMA_ID",
    "PAGE_SCHEMA_ID",
    "REPORT_SCHEMA_ID",
    "REQUEST_LIMIT",
    "SCHEMA_VERSION",
    "SelfHistory",
    "SelfHistoryGame",
    "build_history",
    "canonical_order",
    "history_from_value",
    "history_identity",
    "is_naive_played_at",
    "resolve_self_log_url",
    "self_history_api_url",
    "strict_bool",
    "strict_int",
    "strict_number",
    "strict_text",
]
