"""Exact durable provenance and explicit legacy epoch attribution."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lisjong_arena.riichilab.durable_ranked_game_record import (
    DurableRankedGameRecordError,
    load_ranked_game_record,
)
from lisjong_arena.riichilab_corpus.persistence import read_json
from lisjong_arena.riichilab_longitudinal.errors import LongitudinalAnalysisError
from lisjong_arena.riichilab_longitudinal.models import (
    DURABLE_MAP_SCHEMA_ID,
    SCHEMA_VERSION,
    UNMAPPED_POLICY,
    PolicyProvenance,
    canonical_played_at,
    strict_game_id,
    strict_text,
)
from lisjong_arena.riichilab_self_history.models import SelfHistory, SelfHistoryGame

_EPOCH_REQUIRED = {"policy", "from", "to", "note"}
_EPOCH_OPTIONAL = {
    "lisjong_revision",
    "lisjong_arena_revision",
    "profile_identity",
}


@dataclass(frozen=True, slots=True)
class PolicyEpoch:
    policy_identity: str
    from_played_at: str
    to_played_at: str
    note: str
    lisjong_revision: str | None = None
    lisjong_arena_revision: str | None = None
    profile_identity: str | None = None

    def __post_init__(self) -> None:
        strict_text(self.policy_identity, "epoch policy")
        if self.policy_identity == UNMAPPED_POLICY:
            raise LongitudinalAnalysisError(
                "legacy epoch cannot use the reserved <unmapped> policy"
            )
        object.__setattr__(
            self,
            "from_played_at",
            canonical_played_at(self.from_played_at, "epoch from"),
        )
        object.__setattr__(
            self,
            "to_played_at",
            canonical_played_at(self.to_played_at, "epoch to"),
        )
        strict_text(self.note, "epoch note")
        for name in (
            "lisjong_revision",
            "lisjong_arena_revision",
            "profile_identity",
        ):
            value = getattr(self, name)
            if value is not None:
                strict_text(value, f"epoch {name}")
        start = datetime.fromisoformat(self.from_played_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.to_played_at.replace("Z", "+00:00"))
        if (start.tzinfo is None) != (end.tzinfo is None):
            raise LongitudinalAnalysisError(
                "legacy epoch bounds cannot mix timezone-naive and aware values"
            )
        if start >= end:
            raise LongitudinalAnalysisError("legacy epoch requires from < to")

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.from_played_at.replace("Z", "+00:00"))

    @property
    def end(self) -> datetime:
        return datetime.fromisoformat(self.to_played_at.replace("Z", "+00:00"))

    def contains(self, played_at: str) -> bool:
        value = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
        if (value.tzinfo is None) != (self.start.tzinfo is None):
            raise LongitudinalAnalysisError(
                "played_at and legacy epoch timezone awareness differ; no timezone "
                "is inferred"
            )
        return self.start <= value < self.end

    def provenance(self) -> PolicyProvenance:
        return PolicyProvenance(
            policy_identity=self.policy_identity,
            source="legacy_epoch",
            lisjong_revision=self.lisjong_revision,
            lisjong_arena_revision=self.lisjong_arena_revision,
            profile_identity=self.profile_identity,
        )


def _optional_text(row: dict[str, str | None], key: str) -> str | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    return strict_text(value, f"epoch {key}")


def load_policy_epochs(path: str | Path | None) -> tuple[PolicyEpoch, ...]:
    """Read the explicit legacy CSV and validate its half-open partition."""
    if path is None:
        return ()
    candidate = Path(path)
    try:
        with candidate.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or ())
            if not _EPOCH_REQUIRED.issubset(fields) or not fields.issubset(
                _EPOCH_REQUIRED | _EPOCH_OPTIONAL
            ):
                raise LongitudinalAnalysisError("legacy epoch CSV columns are invalid")
            epochs = tuple(
                PolicyEpoch(
                    policy_identity=strict_text(row.get("policy"), "epoch policy"),
                    from_played_at=canonical_played_at(row.get("from"), "epoch from"),
                    to_played_at=canonical_played_at(row.get("to"), "epoch to"),
                    note=strict_text(row.get("note"), "epoch note"),
                    lisjong_revision=_optional_text(row, "lisjong_revision"),
                    lisjong_arena_revision=_optional_text(
                        row, "lisjong_arena_revision"
                    ),
                    profile_identity=_optional_text(row, "profile_identity"),
                )
                for row in reader
            )
    except OSError as exc:
        raise LongitudinalAnalysisError(
            f"legacy epoch CSV cannot be read: {candidate}"
        ) from exc

    if not epochs:
        return ()
    awareness = {epoch.start.tzinfo is None for epoch in epochs}
    if len(awareness) != 1:
        raise LongitudinalAnalysisError(
            "legacy epochs cannot mix timezone-naive and aware values"
        )
    ordered = tuple(sorted(epochs, key=lambda epoch: (epoch.start, epoch.end)))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end:
            raise LongitudinalAnalysisError("legacy policy epochs overlap")
    return ordered


def load_durable_provenance_map(
    path: str | Path | None,
    history: SelfHistory,
) -> dict[str, PolicyProvenance]:
    """Strict-load explicitly game-ID-bound #168/#232 records.

    The durable v1 bundle deliberately has no RiichiLab server ``game_id``.
    Consequently the consumer requires an explicit local map and verifies the
    mapped bundle's seat and final self score. It never guesses a join from a
    timestamp, directory name, or operator memory.
    """
    if path is None:
        return {}
    map_path = Path(path)
    value = read_json(map_path, "durable provenance map")
    expected = {"records", "schema", "schema_version"}
    if type(value) is not dict or set(value) != expected:
        raise LongitudinalAnalysisError("durable provenance map fields are invalid")
    if (
        value["schema"] != DURABLE_MAP_SCHEMA_ID
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise LongitudinalAnalysisError("durable provenance map schema is unsupported")
    if type(value["records"]) is not list:
        raise LongitudinalAnalysisError("durable provenance records must be an array")

    games = {game.game_id: game for game in history.games}
    result: dict[str, PolicyProvenance] = {}
    record_identities: set[str] = set()
    for raw in value["records"]:
        if type(raw) is not dict or set(raw) != {"game_id", "record_path"}:
            raise LongitudinalAnalysisError(
                "durable provenance map record fields are invalid"
            )
        game_id = strict_game_id(raw["game_id"])
        record_path_text = strict_text(raw["record_path"], "durable record path")
        if game_id in result:
            raise LongitudinalAnalysisError(
                f"durable provenance map repeats game_id {game_id}"
            )
        if game_id not in games:
            raise LongitudinalAnalysisError(
                f"durable provenance map references unknown game_id {game_id}"
            )
        record_path = Path(record_path_text)
        if not record_path.is_absolute():
            record_path = map_path.parent / record_path
        try:
            record = load_ranked_game_record(record_path)
        except (DurableRankedGameRecordError, OSError) as exc:
            raise LongitudinalAnalysisError(
                f"durable record for {game_id} failed strict readback"
            ) from exc
        game = games[game_id]
        if int(record.bound_seat) != game.seat:
            raise LongitudinalAnalysisError(
                f"durable record for {game_id} has a conflicting self seat"
            )
        if (
            record.result.scores is None
            or record.result.scores[game.seat] != game.score
        ):
            raise LongitudinalAnalysisError(
                f"durable record for {game_id} has a conflicting final score"
            )
        source = record.provenance
        if record.record_identity in record_identities:
            raise LongitudinalAnalysisError(
                "one durable record is mapped to multiple self-history games"
            )
        record_identities.add(record.record_identity)
        result[game_id] = PolicyProvenance(
            policy_identity=source.policy_identity,
            source="durable_record",
            lisjong_revision=source.lisjong_revision,
            lisjong_arena_revision=source.lisjong_arena_revision,
            profile_identity=source.profile_identity,
            durable_record_identity=record.record_identity,
        )
    return result


def resolve_policy_provenance(
    game: SelfHistoryGame,
    *,
    durable: dict[str, PolicyProvenance],
    epochs: tuple[PolicyEpoch, ...],
) -> PolicyProvenance:
    """Resolve durable first, then the explicit legacy fallback, else unmapped."""
    if game.game_id in durable:
        return durable[game.game_id]
    matches = tuple(epoch for epoch in epochs if epoch.contains(game.played_at))
    if len(matches) > 1:  # defensive after global overlap validation
        raise LongitudinalAnalysisError(
            f"game {game.game_id} has conflicting legacy provenance"
        )
    if matches:
        return matches[0].provenance()
    return PolicyProvenance(policy_identity=UNMAPPED_POLICY, source="unmapped")


__all__ = [
    "PolicyEpoch",
    "load_durable_provenance_map",
    "load_policy_epochs",
    "resolve_policy_provenance",
]
