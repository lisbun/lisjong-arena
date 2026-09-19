"""Immutable value contracts for the RiichiLab longitudinal diagnostic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    canonical_json_bytes,
    normalize_played_at,
    normalize_timestamp,
    sha256_bytes,
    validate_game_id,
)
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError

SCHEMA_VERSION = 1
SUMMARY_SCHEMA_ID = "lisjong-arena-riichilab-longitudinal-summary"
OPPONENT_CACHE_SCHEMA_ID = "lisjong-arena-riichilab-opponent-metadata-cache"
DURABLE_MAP_SCHEMA_ID = "lisjong-arena-riichilab-durable-provenance-map"
UNMAPPED_POLICY = "<unmapped>"
UNRESOLVED = "unresolved"


def _fail(message: str) -> LongitudinalAnalysisError:
    return LongitudinalAnalysisError(message)


def strict_game_id(value: object) -> str:
    try:
        return validate_game_id(value)
    except CorpusError as exc:
        raise _fail(str(exc)) from exc


def strict_text(value: object, context: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value) or len(value) > 512:
        qualifier = "bounded string" if allow_empty else "non-empty bounded string"
        raise _fail(f"{context} must be a {qualifier}")
    return value


def strict_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise _fail(f"{context} must be an integer")
    return value


def strict_number(value: object, context: str) -> float:
    if type(value) is bool or type(value) not in (int, float):
        raise _fail(f"{context} must be a number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise _fail(f"{context} must be finite")
    return result


def canonical_played_at(value: object, context: str) -> str:
    try:
        return normalize_played_at(value, context)
    except CorpusError as exc:
        raise _fail(str(exc)) from exc


def canonical_utc_timestamp(value: object, context: str) -> str:
    try:
        return normalize_timestamp(value, context)
    except CorpusError as exc:
        raise _fail(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class PolicyProvenance:
    policy_identity: str
    source: str
    lisjong_revision: str | None = None
    lisjong_arena_revision: str | None = None
    profile_identity: str | None = None
    durable_record_identity: str | None = None

    def __post_init__(self) -> None:
        strict_text(self.policy_identity, "policy_identity")
        if self.source not in {"durable_record", "legacy_epoch", "unmapped"}:
            raise _fail("provenance source is unsupported")
        if self.source == "unmapped" and self.policy_identity != UNMAPPED_POLICY:
            raise _fail("unmapped provenance must use the reserved policy identity")
        if self.source != "unmapped" and self.policy_identity == UNMAPPED_POLICY:
            raise _fail("mapped provenance cannot use the reserved policy identity")
        for name in (
            "lisjong_revision",
            "lisjong_arena_revision",
            "profile_identity",
            "durable_record_identity",
        ):
            value = getattr(self, name)
            if value is not None:
                strict_text(value, name)

    @property
    def complete(self) -> bool:
        return (
            self.source == "durable_record"
            and self.policy_identity != UNRESOLVED
            and self.lisjong_revision not in (None, UNRESOLVED)
            and self.lisjong_arena_revision not in (None, UNRESOLVED)
            and self.profile_identity not in (None, UNRESOLVED)
            and self.durable_record_identity not in (None, UNRESOLVED)
        )

    def to_value(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "durable_record_identity": self.durable_record_identity,
            "lisjong_arena_revision": self.lisjong_arena_revision,
            "lisjong_revision": self.lisjong_revision,
            "policy_identity": self.policy_identity,
            "profile_identity": self.profile_identity,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RoundMetrics:
    rounds: int = 0
    dealer_rounds: int = 0
    wins: int = 0
    tsumo_wins: int = 0
    ron_wins: int = 0
    deal_in_rounds: int = 0
    deal_in_loss: int = 0
    riichi_rounds: int = 0
    riichi_count: int = 0
    riichi_wins: int = 0
    riichi_deal_ins: int = 0
    open_call_rounds: int = 0
    open_call_count: int = 0
    open_wins: int = 0
    open_deal_ins: int = 0
    chi_count: int = 0
    pon_count: int = 0
    kan_count: int = 0
    draw_rounds: int = 0
    draw_delta: int = 0
    opponent_tsumo_rounds: int = 0
    opponent_tsumo_loss: int = 0
    dealer_wins: int = 0
    dealer_deal_ins: int = 0
    terminal_delta: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int:
                raise _fail(f"round metric {name} must be an integer")
            if name not in {"draw_delta", "terminal_delta"} and value < 0:
                raise _fail(f"round metric {name} must be non-negative")
        if self.tsumo_wins + self.ron_wins != self.wins:
            raise _fail("tsumo and ron wins must partition wins")
        if any(
            value > self.rounds
            for value in (
                self.dealer_rounds,
                self.wins,
                self.deal_in_rounds,
                self.riichi_rounds,
                self.open_call_rounds,
                self.draw_rounds,
                self.opponent_tsumo_rounds,
            )
        ):
            raise _fail("round metric count exceeds its round denominator")

    def to_value(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True, order=True)
class OpponentParticipation:
    game_id: str
    bot_id: int
    bot_name: str | None
    seat: int
    rank: int
    score: int
    rating_before: float | None

    def __post_init__(self) -> None:
        strict_game_id(self.game_id)
        if type(self.bot_id) is not int or self.bot_id <= 0:
            raise _fail("opponent bot_id must be a positive integer")
        if self.bot_name is not None:
            strict_text(self.bot_name, "opponent bot_name")
        if type(self.seat) is not int or not 0 <= self.seat <= 3:
            raise _fail("opponent seat must be an integer from 0 through 3")
        if type(self.rank) is not int or not 1 <= self.rank <= 4:
            raise _fail("opponent rank must be an integer from 1 through 4")
        strict_int(self.score, "opponent score")
        if self.rating_before is not None:
            object.__setattr__(
                self,
                "rating_before",
                strict_number(self.rating_before, "opponent rating_before"),
            )

    def to_value(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "game_id": self.game_id,
            "rank": self.rank,
            "rating_before": self.rating_before,
            "score": self.score,
            "seat": self.seat,
        }

    @classmethod
    def from_value(cls, value: object) -> OpponentParticipation:
        expected = {
            "bot_id",
            "bot_name",
            "game_id",
            "rank",
            "rating_before",
            "score",
            "seat",
        }
        if type(value) is not dict or set(value) != expected:
            raise _fail("opponent participation fields are invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class OpponentCache:
    self_bot_id: int
    retrieved_at: str
    selected_game_ids: tuple[str, ...]
    candidate_universe_identity: str
    candidate_universe_source: str
    candidate_bot_count: int
    bots_queried: tuple[int, ...]
    api_failures: tuple[dict[str, object], ...]
    opponents: tuple[OpponentParticipation, ...]

    def __post_init__(self) -> None:
        if type(self.self_bot_id) is not int or self.self_bot_id <= 0:
            raise _fail("cache self_bot_id must be a positive integer")
        object.__setattr__(
            self,
            "retrieved_at",
            canonical_utc_timestamp(self.retrieved_at, "cache retrieved_at"),
        )
        if type(self.selected_game_ids) is not tuple or not self.selected_game_ids:
            raise _fail("cache selected_game_ids must be a non-empty tuple")
        for game_id in self.selected_game_ids:
            strict_game_id(game_id)
        if tuple(sorted(set(self.selected_game_ids))) != self.selected_game_ids:
            raise _fail("cache selected_game_ids must be unique canonical order")
        strict_text(self.candidate_universe_identity, "candidate universe identity")
        strict_text(self.candidate_universe_source, "candidate universe source")
        if type(self.candidate_bot_count) is not int or self.candidate_bot_count < 0:
            raise _fail("candidate bot count must be non-negative")
        if type(self.bots_queried) is not tuple or any(
            type(bot_id) is not int or bot_id <= 0 for bot_id in self.bots_queried
        ):
            raise _fail("bots_queried must contain positive integers")
        if tuple(sorted(set(self.bots_queried))) != self.bots_queried:
            raise _fail("bots_queried must be unique canonical order")
        if len(self.bots_queried) > self.candidate_bot_count:
            raise _fail("queried bot count exceeds the candidate universe")
        if type(self.opponents) is not tuple or any(
            not isinstance(row, OpponentParticipation) for row in self.opponents
        ):
            raise _fail("opponent cache rows must be a tuple of participations")
        keys = [(row.game_id, row.bot_id) for row in self.opponents]
        if len(set(keys)) != len(keys):
            raise _fail("opponent cache contains duplicate game/bot participation")
        if tuple(sorted(self.opponents)) != self.opponents:
            raise _fail("opponent cache rows are not in canonical order")
        selected = set(self.selected_game_ids)
        for row in self.opponents:
            if row.game_id not in selected:
                raise _fail("opponent cache contains a non-selected game")
            if row.bot_id == self.self_bot_id:
                raise _fail("opponent cache must exclude the self bot")
        for game_id in self.selected_game_ids:
            rows = tuple(row for row in self.opponents if row.game_id == game_id)
            if len(rows) > 3:
                raise _fail("opponent cache has more than three opponents for a game")
            if len({row.seat for row in rows}) != len(rows):
                raise _fail("opponent cache assigns multiple bots to one seat")
            if len({row.rank for row in rows}) != len(rows):
                raise _fail("opponent cache assigns duplicate ranks within a game")
        if type(self.api_failures) is not tuple:
            raise _fail("opponent cache API failures must be a tuple")
        seen_failure_ids: set[int] = set()
        for value in self.api_failures:
            if type(value) is not dict or set(value) != {
                "bot_id",
                "error_type",
                "message",
            }:
                raise _fail("opponent cache API failure fields are invalid")
            bot_id = strict_int(value["bot_id"], "API failure bot_id")
            if bot_id <= 0 or bot_id in seen_failure_ids:
                raise _fail("opponent cache API failures contain an invalid duplicate")
            seen_failure_ids.add(bot_id)
            if bot_id not in self.bots_queried:
                raise _fail("API failure bot was not recorded as queried")
            strict_text(value["error_type"], "API failure type")
            strict_text(value["message"], "API failure message")

    def to_value(self) -> dict[str, object]:
        return {
            "api_failures": list(self.api_failures),
            "bots_queried": list(self.bots_queried),
            "candidate_universe": {
                "bot_count": self.candidate_bot_count,
                "identity": self.candidate_universe_identity,
                "source": self.candidate_universe_source,
            },
            "cache_identity": self.identity,
            "coverage": self.coverage,
            "opponents": [row.to_value() for row in self.opponents],
            "retrieved_at": self.retrieved_at,
            "schema": OPPONENT_CACHE_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "selected_game_ids": list(self.selected_game_ids),
            "self_bot_id": self.self_bot_id,
        }

    @property
    def identity(self) -> str:
        logical = {
            "api_failures": list(self.api_failures),
            "bots_queried": list(self.bots_queried),
            "candidate_universe_identity": self.candidate_universe_identity,
            "candidate_universe_source": self.candidate_universe_source,
            "candidate_bot_count": self.candidate_bot_count,
            "opponents": [row.to_value() for row in self.opponents],
            "schema": OPPONENT_CACHE_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "selected_game_ids": list(self.selected_game_ids),
            "self_bot_id": self.self_bot_id,
        }
        return sha256_bytes(canonical_json_bytes(logical))

    @property
    def coverage(self) -> dict[str, object]:
        complete = 0
        partial = 0
        unknown = 0
        for game_id in self.selected_game_ids:
            rows = tuple(row for row in self.opponents if row.game_id == game_id)
            known = sum(row.rating_before is not None for row in rows)
            if len(rows) == 3 and known == 3:
                complete += 1
            elif known:
                partial += 1
            else:
                unknown += 1
        selected = len(self.selected_game_ids)
        return {
            "complete_opponent_games": complete,
            "opponent_rating_coverage_rate": (
                complete / selected if selected else None
            ),
            "partial_opponent_games": partial,
            "selected_games": selected,
            "unknown_opponent_games": unknown,
        }

    @classmethod
    def from_value(cls, value: object) -> OpponentCache:
        expected = {
            "api_failures",
            "bots_queried",
            "cache_identity",
            "candidate_universe",
            "coverage",
            "opponents",
            "retrieved_at",
            "schema",
            "schema_version",
            "selected_game_ids",
            "self_bot_id",
        }
        if type(value) is not dict or set(value) != expected:
            raise _fail("opponent cache document fields are invalid")
        if (
            value["schema"] != OPPONENT_CACHE_SCHEMA_ID
            or value["schema_version"] != SCHEMA_VERSION
        ):
            raise _fail("opponent cache schema is unsupported")
        universe = value["candidate_universe"]
        if type(universe) is not dict or set(universe) != {
            "bot_count",
            "identity",
            "source",
        }:
            raise _fail("candidate universe fields are invalid")
        for key in ("api_failures", "bots_queried", "opponents", "selected_game_ids"):
            if type(value[key]) is not list:
                raise _fail(f"opponent cache {key} must be an array")
        cache = cls(
            self_bot_id=value["self_bot_id"],
            retrieved_at=value["retrieved_at"],
            selected_game_ids=tuple(value["selected_game_ids"]),
            candidate_universe_identity=universe["identity"],
            candidate_universe_source=universe["source"],
            candidate_bot_count=universe["bot_count"],
            bots_queried=tuple(value["bots_queried"]),
            api_failures=tuple(value["api_failures"]),
            opponents=tuple(
                OpponentParticipation.from_value(item) for item in value["opponents"]
            ),
        )
        if value["coverage"] != cache.coverage:
            raise _fail("opponent cache coverage does not match its rows")
        if value["cache_identity"] != cache.identity:
            raise _fail("opponent cache identity mismatch")
        return cache


@dataclass(frozen=True, slots=True)
class GameAnalysis:
    game_id: str
    played_at: str
    seat: int
    rank: int
    final_score: int
    self_rating_before: float
    rating_delta: float
    is_disconnected: bool
    provenance: PolicyProvenance
    mjai_status: str
    metrics: RoundMetrics | None
    opponents: tuple[OpponentParticipation, ...]

    def __post_init__(self) -> None:
        strict_game_id(self.game_id)
        object.__setattr__(
            self, "played_at", canonical_played_at(self.played_at, "played_at")
        )
        if type(self.seat) is not int or not 0 <= self.seat <= 3:
            raise _fail("self seat must be an integer from 0 through 3")
        if type(self.rank) is not int or not 1 <= self.rank <= 4:
            raise _fail("self rank must be an integer from 1 through 4")
        strict_int(self.final_score, "final score")
        object.__setattr__(
            self,
            "self_rating_before",
            strict_number(self.self_rating_before, "self rating_before"),
        )
        object.__setattr__(
            self, "rating_delta", strict_number(self.rating_delta, "rating_delta")
        )
        if type(self.is_disconnected) is not bool:
            raise _fail("is_disconnected must be a boolean")
        if self.mjai_status not in {"available", "missing"}:
            raise _fail("MJAI status is unsupported")
        if (self.mjai_status == "available") != (self.metrics is not None):
            raise _fail("MJAI status and round metrics disagree")
        if type(self.opponents) is not tuple or any(
            not isinstance(row, OpponentParticipation) for row in self.opponents
        ):
            raise _fail("game opponents must be a tuple of participations")
        if tuple(sorted(self.opponents)) != self.opponents:
            raise _fail("game opponents are not in canonical order")
        if len(self.opponents) > 3:
            raise _fail("a four-player game cannot have more than three opponents")
        if len({row.seat for row in self.opponents}) != len(self.opponents):
            raise _fail("multiple opponents occupy the same seat")
        if len({row.bot_id for row in self.opponents}) != len(self.opponents):
            raise _fail("one opponent bot appears more than once")
        if any(row.game_id != self.game_id for row in self.opponents):
            raise _fail("opponent participation belongs to another game")
        if any(row.seat == self.seat for row in self.opponents):
            raise _fail("opponent participation occupies the self seat")

    @property
    def rating_after(self) -> float:
        return self.self_rating_before + self.rating_delta

    @property
    def known_opponent_ratings(self) -> tuple[float, ...]:
        return tuple(
            row.rating_before for row in self.opponents if row.rating_before is not None
        )

    @property
    def opponent_ratings_complete(self) -> bool:
        return len(self.opponents) == 3 and len(self.known_opponent_ratings) == 3

    @property
    def opponent_rating_avg(self) -> float | None:
        if not self.opponent_ratings_complete:
            return None
        return sum(self.known_opponent_ratings) / 3

    @property
    def rating_gap_avg(self) -> float | None:
        average = self.opponent_rating_avg
        return None if average is None else self.self_rating_before - average

    def to_value(self) -> dict[str, Any]:
        ratings = self.known_opponent_ratings
        return {
            "final_score": self.final_score,
            "game_id": self.game_id,
            "is_disconnected": self.is_disconnected,
            "mjai_status": self.mjai_status,
            "opponent_count_known": len(self.opponents),
            "opponent_rating_avg": self.opponent_rating_avg,
            "opponent_rating_max": max(ratings)
            if self.opponent_ratings_complete
            else None,
            "opponent_rating_min": min(ratings)
            if self.opponent_ratings_complete
            else None,
            "opponent_ratings_complete": self.opponent_ratings_complete,
            "played_at": self.played_at,
            "provenance": self.provenance.to_value(),
            "rank": self.rank,
            "rating_after": self.rating_after,
            "rating_delta": self.rating_delta,
            "rating_gap_avg": self.rating_gap_avg,
            "round_metrics": None if self.metrics is None else self.metrics.to_value(),
            "seat": self.seat,
            "self_rating_before": self.self_rating_before,
        }


__all__ = [
    "DURABLE_MAP_SCHEMA_ID",
    "OPPONENT_CACHE_SCHEMA_ID",
    "SCHEMA_VERSION",
    "SUMMARY_SCHEMA_ID",
    "UNMAPPED_POLICY",
    "UNRESOLVED",
    "GameAnalysis",
    "OpponentCache",
    "OpponentParticipation",
    "PolicyProvenance",
    "RoundMetrics",
    "canonical_played_at",
    "canonical_utc_timestamp",
    "strict_game_id",
    "strict_int",
    "strict_number",
    "strict_text",
]
