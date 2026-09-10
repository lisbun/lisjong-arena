"""Value contracts for the bounded RiichiLab third-party corpus."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_ID = "lisjong-arena-riichilab-corpus"
SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_ID = "lisjong-arena-riichilab-recent-games-snapshot"
PLAN_SCHEMA_ID = "lisjong-arena-riichilab-acquisition-plan"
REPORT_SCHEMA_ID = "lisjong-arena-riichilab-acquisition-report"
MAX_ACQUISITION_CEILING = 250

API_BASE_URL = "https://api.riichi.dev/api/v1"
LOG_BASE_URL = "https://logs.riichi.dev/mjai-logs"

TARGET_BOTS = ((126, "FuuroMaster"), (120, "Mortal-v4b"), (294, "zero-test2"))

_GAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}").fullmatch


class CorpusError(ValueError):
    """The corpus input or persisted state violates the v0 contract."""


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_game_id(value: object) -> str:
    if type(value) is not str or _GAME_ID(value) is None:
        raise CorpusError("game_id must be a safe non-empty identifier")
    return value


def normalize_timestamp(value: object, context: str) -> str:
    if type(value) is not str or not value:
        raise CorpusError(f"{context} must be a timestamp string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CorpusError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CorpusError(f"{context} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, order=True)
class Participation:
    game_id: str
    bot_id: int
    seat: int
    played_at: str
    rank: int | None = None
    score: int | None = None

    def __post_init__(self) -> None:
        validate_game_id(self.game_id)
        if type(self.bot_id) is not int or self.bot_id <= 0:
            raise CorpusError("bot_id must be a positive integer")
        if type(self.seat) is not int or not 0 <= self.seat <= 3:
            raise CorpusError("seat must be an integer from 0 through 3")
        object.__setattr__(
            self, "played_at", normalize_timestamp(self.played_at, "played_at")
        )
        if self.rank is not None and (
            type(self.rank) is not int or not 1 <= self.rank <= 4
        ):
            raise CorpusError("rank must be null or an integer from 1 through 4")
        if self.score is not None and type(self.score) is not int:
            raise CorpusError("score must be null or an integer")

    def to_value(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "game_id": self.game_id,
            "played_at": self.played_at,
            "rank": self.rank,
            "score": self.score,
            "seat": self.seat,
        }

    @classmethod
    def from_value(cls, value: object) -> Participation:
        expected = {"bot_id", "game_id", "played_at", "rank", "score", "seat"}
        if type(value) is not dict or set(value) != expected:
            raise CorpusError("participation fields are invalid")
        return cls(
            game_id=value["game_id"],
            bot_id=value["bot_id"],
            seat=value["seat"],
            played_at=value["played_at"],
            rank=value["rank"],
            score=value["score"],
        )


@dataclass(frozen=True, slots=True)
class RecentGamesSnapshot:
    retrieved_at: str
    source_apis: tuple[str, ...]
    target_bots: tuple[tuple[int, str], ...]
    participations: tuple[Participation, ...]
    snapshot_identity: str

    @property
    def game_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.game_id for item in self.participations}))

    def participation_counts(self) -> dict[int, int]:
        return {
            bot_id: sum(item.bot_id == bot_id for item in self.participations)
            for bot_id, _ in self.target_bots
        }

    def to_value(self) -> dict[str, object]:
        counts = self.participation_counts()
        shared_game_count = sum(
            sum(item.game_id == game_id for item in self.participations) > 1
            for game_id in self.game_ids
        )
        return {
            "participation_counts": {
                str(bot_id): counts[bot_id] for bot_id, _ in self.target_bots
            },
            "participation_count": len(self.participations),
            "participations": [item.to_value() for item in self.participations],
            "retrieved_at": self.retrieved_at,
            "schema": SNAPSHOT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "shared_game_count": shared_game_count,
            "snapshot_identity": self.snapshot_identity,
            "source_apis": list(self.source_apis),
            "target_bots": [
                {"bot_id": bot_id, "label": label} for bot_id, label in self.target_bots
            ],
            "unique_game_count": len(self.game_ids),
        }


def snapshot_identity(
    source_apis: tuple[str, ...],
    target_bots: tuple[tuple[int, str], ...],
    participations: tuple[Participation, ...],
) -> str:
    logical = {
        "participations": [item.to_value() for item in participations],
        "source_apis": list(source_apis),
        "target_bot_ids": [item[0] for item in target_bots],
    }
    return sha256_bytes(canonical_json_bytes(logical))


def build_snapshot(
    *,
    retrieved_at: str,
    source_apis: tuple[str, ...],
    target_bots: tuple[tuple[int, str], ...],
    participations: tuple[Participation, ...],
) -> RecentGamesSnapshot:
    normalized_time = normalize_timestamp(retrieved_at, "retrieved_at")
    if type(source_apis) is not tuple or not source_apis:
        raise CorpusError("source_apis must be a non-empty tuple")
    if target_bots != TARGET_BOTS:
        raise CorpusError("target_bots must match the fixed Issue #170 targets")
    target_ids = tuple(item[0] for item in target_bots)
    expected_sources = tuple(f"{API_BASE_URL}/bots/{bot_id}" for bot_id in target_ids)
    if source_apis != expected_sources:
        raise CorpusError("source APIs must match the fixed target bot endpoints")
    ordered = tuple(sorted(participations))
    keys = [(item.game_id, item.bot_id) for item in ordered]
    if len(set(keys)) != len(keys):
        raise CorpusError("duplicate game/bot participation")
    if any(item.bot_id not in target_ids for item in ordered):
        raise CorpusError("participation contains a non-target bot")
    for game_id in {item.game_id for item in ordered}:
        values = [item for item in ordered if item.game_id == game_id]
        if len({item.seat for item in values}) != len(values):
            raise CorpusError(
                f"game {game_id} assigns multiple target bots to one seat"
            )
        if len({item.played_at for item in values}) != 1:
            raise CorpusError(f"game {game_id} has conflicting played_at provenance")
    identity = snapshot_identity(source_apis, target_bots, ordered)
    return RecentGamesSnapshot(
        normalized_time, source_apis, target_bots, ordered, identity
    )


def snapshot_from_value(value: object) -> RecentGamesSnapshot:
    expected = {
        "participation_counts",
        "participation_count",
        "participations",
        "retrieved_at",
        "schema",
        "schema_version",
        "shared_game_count",
        "snapshot_identity",
        "source_apis",
        "target_bots",
        "unique_game_count",
    }
    if type(value) is not dict or set(value) != expected:
        raise CorpusError("snapshot fields are invalid")
    if type(value["schema"]) is not str or value["schema"] != SNAPSHOT_SCHEMA_ID:
        raise CorpusError("snapshot schema is unsupported")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise CorpusError("snapshot schema is unsupported")
    bots_raw = value["target_bots"]
    if type(bots_raw) is not list:
        raise CorpusError("target_bots must be an array")
    bots = []
    for item in bots_raw:
        if type(item) is not dict or set(item) != {"bot_id", "label"}:
            raise CorpusError("target bot fields are invalid")
        if type(item["bot_id"]) is not int or type(item["label"]) is not str:
            raise CorpusError("target bot value is invalid")
        bots.append((item["bot_id"], item["label"]))
    sources_raw = value["source_apis"]
    participations_raw = value["participations"]
    if type(sources_raw) is not list or type(participations_raw) is not list:
        raise CorpusError("snapshot arrays are invalid")
    snapshot = build_snapshot(
        retrieved_at=value["retrieved_at"],
        source_apis=tuple(sources_raw),
        target_bots=tuple(bots),
        participations=tuple(
            Participation.from_value(item) for item in participations_raw
        ),
    )
    if value["snapshot_identity"] != snapshot.snapshot_identity:
        raise CorpusError("snapshot identity mismatch")
    if type(value["unique_game_count"]) is not int or value["unique_game_count"] != len(
        snapshot.game_ids
    ):
        raise CorpusError("snapshot unique game count mismatch")
    if type(value["participation_count"]) is not int or value[
        "participation_count"
    ] != len(snapshot.participations):
        raise CorpusError("snapshot participation count mismatch")
    expected_shared = sum(
        sum(item.game_id == game_id for item in snapshot.participations) > 1
        for game_id in snapshot.game_ids
    )
    if (
        type(value["shared_game_count"]) is not int
        or value["shared_game_count"] != expected_shared
    ):
        raise CorpusError("snapshot shared game count mismatch")
    expected_counts = {
        str(key): count for key, count in snapshot.participation_counts().items()
    }
    counts_value = value["participation_counts"]
    if (
        type(counts_value) is not dict
        or set(counts_value) != set(expected_counts)
        or any(type(counts_value[key]) is not int for key in expected_counts)
        or counts_value != expected_counts
    ):
        raise CorpusError("snapshot participation counts mismatch")
    return snapshot


def strict_json_loads(data: bytes, context: str) -> Any:
    def reject_constant(item: str) -> None:
        raise CorpusError(f"{context} contains non-finite {item}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, item in pairs:
            if key in result:
                raise CorpusError(f"{context} contains duplicate key {key!r}")
            result[key] = item
        return result

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusError(f"{context} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise CorpusError(f"{context} is not valid JSON") from exc


__all__ = [
    "API_BASE_URL",
    "CorpusError",
    "LOG_BASE_URL",
    "MAX_ACQUISITION_CEILING",
    "Participation",
    "RecentGamesSnapshot",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "TARGET_BOTS",
    "build_snapshot",
    "canonical_json_bytes",
    "normalize_timestamp",
    "sha256_bytes",
    "snapshot_from_value",
    "strict_json_loads",
    "utc_now_text",
    "validate_game_id",
]
