"""Fail-closed local cache and provenance manifest persistence."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from lisjong_arena.riichilab_corpus.models import (
    LOG_BASE_URL,
    SCHEMA_ID,
    SCHEMA_VERSION,
    CorpusError,
    Participation,
    RecentGamesSnapshot,
    canonical_json_bytes,
    normalize_timestamp,
    sha256_bytes,
    strict_json_loads,
    validate_game_id,
)
from lisjong_arena.riichilab_corpus.validation import (
    HiddenInformationCoverage,
    ValidationResult,
    validate_mjai_gzip,
)

CACHE_SCHEMA_ID = "lisjong-arena-riichilab-corpus-cache"
CACHE_INDEX_FILENAME = "cache-index.json"
GAMES_DIRECTORY = "games"


def ensure_outside_git_worktree(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        marker = candidate / ".git"
        if marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file()):
            raise CorpusError(
                "third-party corpus output must be outside every Git worktree"
            )
    return resolved


def atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value)
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(payload)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def read_json(path: Path, context: str) -> object:
    try:
        return strict_json_loads(path.read_bytes(), context)
    except OSError as exc:
        raise CorpusError(f"cannot read {context}: {path}") from exc


def resolve_log_url(participations: tuple[Participation, ...]) -> str:
    if not participations:
        raise CorpusError("a game must have at least one target participation")
    game_ids = {item.game_id for item in participations}
    timestamps = {item.played_at for item in participations}
    if len(game_ids) != 1 or len(timestamps) != 1:
        raise CorpusError("game participation provenance is inconsistent")
    date = participations[0].played_at[:10].replace("-", "/")
    return f"{LOG_BASE_URL}/{date}/{participations[0].game_id}.jsonl.gz"


def _empty_index() -> dict[str, object]:
    return {
        "entries": {},
        "schema": CACHE_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
    }


def load_cache_index(output_dir: Path) -> dict[str, dict[str, object]]:
    path = output_dir / CACHE_INDEX_FILENAME
    if not path.exists():
        return {}
    value = read_json(path, "cache index")
    if (
        type(value) is not dict
        or set(value) != {"entries", "schema", "schema_version"}
        or type(value["schema"]) is not str
        or value["schema"] != CACHE_SCHEMA_ID
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or type(value["entries"]) is not dict
    ):
        raise CorpusError("cache index contract is invalid")
    result = {}
    required = {
        "compressed_sha256",
        "content_length",
        "filename",
        "game_id",
        "http_status",
        "resolved_log_url",
        "retrieved_at",
        "validation",
    }
    for game_id, entry in value["entries"].items():
        if (
            type(game_id) is not str
            or type(entry) is not dict
            or set(entry) != required
        ):
            raise CorpusError("cache entry contract is invalid")
        if entry["game_id"] != game_id:
            raise CorpusError("cache entry game_id mismatch")
        validate_game_id(game_id)
        if (
            type(entry["compressed_sha256"]) is not str
            or len(entry["compressed_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in entry["compressed_sha256"]
            )
        ):
            raise CorpusError("cache entry digest is invalid")
        if type(entry["content_length"]) is not int or entry["content_length"] <= 0:
            raise CorpusError("cache entry content length is invalid")
        if entry["filename"] != f"{GAMES_DIRECTORY}/{game_id}.jsonl.gz":
            raise CorpusError("cache entry identifier is invalid")
        if type(entry["http_status"]) is not int or entry["http_status"] != 200:
            raise CorpusError("cache entry HTTP status is invalid")
        if type(entry["resolved_log_url"]) is not str or not entry[
            "resolved_log_url"
        ].startswith(LOG_BASE_URL + "/"):
            raise CorpusError("cache entry log URL is invalid")
        normalized = normalize_timestamp(
            entry["retrieved_at"], "cache retrieval timestamp"
        )
        if normalized != entry["retrieved_at"]:
            raise CorpusError("cache retrieval timestamp is not canonical UTC")
        _validation_from_value(entry["validation"])
        result[game_id] = entry
    return result


def _save_cache_index(output_dir: Path, entries: dict[str, dict[str, object]]) -> None:
    value = _empty_index()
    value["entries"] = {key: entries[key] for key in sorted(entries)}
    atomic_replace(output_dir / CACHE_INDEX_FILENAME, canonical_json_bytes(value))


def _validation_from_value(value: object) -> ValidationResult:
    if type(value) is not dict or set(value) != {
        "event_count",
        "hidden_information",
        "lifecycle_valid",
    }:
        raise CorpusError("cached validation result is invalid")
    hidden = value["hidden_information"]
    expected_hidden = {
        "actual_tsumo_count",
        "all_seat_initial_hand_rounds",
        "masked_tsumo_count",
        "missing_tsumo_count",
        "reconstructable_rounds",
        "round_count",
    }
    if type(hidden) is not dict or set(hidden) != expected_hidden:
        raise CorpusError("cached hidden-information result is invalid")
    if type(value["event_count"]) is not int or value["event_count"] <= 0:
        raise CorpusError("cached event count is invalid")
    if value["lifecycle_valid"] is not True:
        raise CorpusError("cached lifecycle result is not valid")
    if any(type(hidden[key]) is not int or hidden[key] < 0 for key in expected_hidden):
        raise CorpusError("cached hidden-information count is invalid")
    if (
        hidden["all_seat_initial_hand_rounds"] > hidden["round_count"]
        or hidden["reconstructable_rounds"] > hidden["round_count"]
    ):
        raise CorpusError("cached hidden-information counts are inconsistent")
    return ValidationResult(
        event_count=value["event_count"],
        lifecycle_valid=True,
        hidden_information=HiddenInformationCoverage(**hidden),
    )


def validate_cache_entry(
    output_dir: Path,
    entry: dict[str, object],
    participations: tuple[Participation, ...],
) -> ValidationResult:
    game_id = participations[0].game_id
    expected_filename = f"{GAMES_DIRECTORY}/{game_id}.jsonl.gz"
    if entry["filename"] != expected_filename:
        raise CorpusError(f"cache filename mismatch for {game_id}")
    if entry["resolved_log_url"] != resolve_log_url(participations):
        raise CorpusError(f"cache log URL mismatch for {game_id}")
    if entry["http_status"] != 200:
        raise CorpusError(f"cache HTTP status is not 200 for {game_id}")
    path = output_dir / expected_filename
    if not path.is_file():
        raise CorpusError(f"cache artifact is missing for {game_id}")
    payload = path.read_bytes()
    digest = sha256_bytes(payload)
    if entry["compressed_sha256"] != digest:
        raise CorpusError(f"cache digest mismatch for {game_id}")
    if entry["content_length"] != len(payload):
        raise CorpusError(f"cache content length mismatch for {game_id}")
    actual = validate_mjai_gzip(payload, participations=participations)
    expected = _validation_from_value(entry["validation"])
    if actual != expected:
        raise CorpusError(f"cache validation summary mismatch for {game_id}")
    return actual


def inspect_cache(
    output_dir: Path, snapshot: RecentGamesSnapshot
) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    entries = load_cache_index(output_dir)
    hits = []
    grouped = group_participations(snapshot)
    games_dir = output_dir / GAMES_DIRECTORY
    for game_id, participations in grouped.items():
        artifact = games_dir / f"{game_id}.jsonl.gz"
        entry = entries.get(game_id)
        if entry is None:
            if artifact.exists():
                raise CorpusError(
                    f"unindexed cache artifact exists for {game_id}; refusing reuse"
                )
            continue
        validate_cache_entry(output_dir, entry, participations)
        hits.append(game_id)
    return entries, tuple(sorted(hits))


def persist_download(
    output_dir: Path,
    *,
    participations: tuple[Participation, ...],
    payload: bytes,
    http_status: int,
    content_length: int | None,
    retrieved_at: str,
) -> dict[str, object]:
    if type(payload) is not bytes:
        raise TypeError("payload must be bytes")
    if type(http_status) is not int:
        raise CorpusError("HTTP status must be an integer")
    if content_length is not None and (
        type(content_length) is not int or content_length < 0
    ):
        raise CorpusError("HTTP content length must be null or non-negative")
    normalized_retrieved_at = normalize_timestamp(retrieved_at, "retrieved_at")
    if normalized_retrieved_at != retrieved_at:
        raise CorpusError("retrieved_at must use canonical UTC form")
    game_id = participations[0].game_id
    validation = validate_mjai_gzip(payload, participations=participations)
    if http_status != 200:
        raise CorpusError(f"cannot persist HTTP {http_status} response")
    if content_length is not None and content_length != len(payload):
        raise CorpusError(f"HTTP content-length mismatch for {game_id}")
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = load_cache_index(output_dir)
    destination = output_dir / GAMES_DIRECTORY / f"{game_id}.jsonl.gz"
    if game_id in entries or destination.exists():
        if game_id not in entries or not destination.is_file():
            raise CorpusError(f"incomplete existing cache state for {game_id}")
        existing = destination.read_bytes()
        if existing != payload:
            raise CorpusError(f"conflicting compressed bytes for game {game_id}")
        validate_cache_entry(output_dir, entries[game_id], participations)
        return entries[game_id]

    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(payload)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    entry = {
        "compressed_sha256": sha256_bytes(payload),
        "content_length": len(payload),
        "filename": f"{GAMES_DIRECTORY}/{game_id}.jsonl.gz",
        "game_id": game_id,
        "http_status": 200,
        "resolved_log_url": resolve_log_url(participations),
        "retrieved_at": normalized_retrieved_at,
        "validation": validation.to_value(),
    }
    entries[game_id] = entry
    _save_cache_index(output_dir, entries)
    return entry


def group_participations(
    snapshot: RecentGamesSnapshot,
) -> dict[str, tuple[Participation, ...]]:
    return {
        game_id: tuple(
            item for item in snapshot.participations if item.game_id == game_id
        )
        for game_id in snapshot.game_ids
    }


def cache_state_identity(
    entries: dict[str, dict[str, object]], game_ids: tuple[str, ...]
) -> str:
    state = [
        {
            "entry": entries[game_id],
            "game_id": game_id,
        }
        for game_id in game_ids
        if game_id in entries
    ]
    return sha256_bytes(canonical_json_bytes(state))


def build_manifest(
    snapshot: RecentGamesSnapshot,
    entries: dict[str, dict[str, object]],
) -> dict[str, Any]:
    grouped = group_participations(snapshot)
    if set(snapshot.game_ids) - set(entries):
        raise CorpusError("cannot build a manifest from an incomplete cache")
    games = []
    for game_id in snapshot.game_ids:
        entry = entries[game_id]
        games.append(
            {
                **entry,
                "played_at": grouped[game_id][0].played_at,
                "participations": [item.to_value() for item in grouped[game_id]],
                "source_apis": [
                    bot_api
                    for bot_api, (bot_id, _) in zip(
                        snapshot.source_apis, snapshot.target_bots, strict=True
                    )
                    if any(item.bot_id == bot_id for item in grouped[game_id])
                ],
                "target_bot_ids": sorted(item.bot_id for item in grouped[game_id]),
                "validation_result": "valid",
            }
        )
    identity_input = {
        "games": [
            {
                "compressed_sha256": game["compressed_sha256"],
                "game_id": game["game_id"],
            }
            for game in games
        ],
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
    }
    corpus_identity = sha256_bytes(canonical_json_bytes(identity_input))
    manifest = {
        "compliance": {
            "commercial_use": "HOLD",
            "local_personal_noncommercial_analysis": "GO",
            "local_personal_noncommercial_ml_training": "GO",
            "local_retention": "GO",
            "public_raw_dataset": "NO-GO",
            "raw_log_redistribution": "NO-GO",
            "thousands_game_acquisition": "HOLD",
        },
        "corpus_identity": corpus_identity,
        "games": games,
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "snapshot_identity": snapshot.snapshot_identity,
        "snapshot_retrieved_at": snapshot.retrieved_at,
        "source_apis": list(snapshot.source_apis),
        "target_bot_ids": [item[0] for item in snapshot.target_bots],
        "unique_game_count": len(games),
    }
    manifest["manifest_sha256"] = sha256_bytes(canonical_json_bytes(manifest))
    return manifest


def save_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    path = output_dir / f"manifest-{manifest['snapshot_identity']}.json"
    payload = canonical_json_bytes(manifest)
    if path.exists():
        if path.read_bytes() != payload:
            raise CorpusError("existing manifest conflicts with current manifest")
        return path
    write_new_json(path, manifest)
    return path


def validate_manifest_file(
    output_dir: Path,
    snapshot: RecentGamesSnapshot,
    entries: dict[str, dict[str, object]],
) -> dict[str, Any]:
    expected = build_manifest(snapshot, entries)
    path = output_dir / f"manifest-{snapshot.snapshot_identity}.json"
    if not path.is_file():
        raise CorpusError("snapshot-specific manifest is missing")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CorpusError("cannot read RiichiLab corpus manifest") from exc
    strict_json_loads(payload, "RiichiLab corpus manifest")
    if payload != canonical_json_bytes(expected):
        raise CorpusError("manifest content or digest does not match cache provenance")
    return expected


__all__ = [
    "CACHE_INDEX_FILENAME",
    "GAMES_DIRECTORY",
    "atomic_replace",
    "build_manifest",
    "cache_state_identity",
    "ensure_outside_git_worktree",
    "group_participations",
    "inspect_cache",
    "load_cache_index",
    "persist_download",
    "read_json",
    "resolve_log_url",
    "save_manifest",
    "validate_cache_entry",
    "validate_manifest_file",
    "write_new_json",
]
