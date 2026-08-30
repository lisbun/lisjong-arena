"""Deterministic sharded gzip persistence and logical Phase 4 identity."""

import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    ANCHOR_SEMANTICS_ID,
    EVIDENCE_CUTOFF_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import (
    effective_rule_provenance,
)
from lisjong_arena.phase4_raw_corpus.codec import (
    canonical_json_bytes,
    parse_provenance,
    parse_shard_value,
    provenance_to_dict,
    shard_value,
)
from lisjong_arena.phase4_raw_corpus.model import (
    GENERATION_PROTOCOL_ID,
    MAX_GAMES_PER_SHARD,
    SCHEMA_VERSION,
    Phase4RawCorpusError,
    RawCorpus,
)

MANIFEST_FILENAME = "manifest.json"
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ShardInfo:
    shard_index: int
    filename: str
    seeds: tuple[int, ...]
    canonical_sha256: str
    uncompressed_bytes: int
    compressed_bytes: int


@dataclass(frozen=True, slots=True)
class PersistedRawCorpus:
    corpus: RawCorpus
    shards: tuple[ShardInfo, ...]
    corpus_identity: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _nonnegative_int(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise Phase4RawCorpusError(f"{context} must be a non-negative int")
    return value


def _digest(value: object, context: str) -> str:
    if (
        type(value) is not str
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase4RawCorpusError(f"{context} must be a lowercase SHA-256")
    return value


def _validate_provenance(provenance: TrainingPipelineProvenance) -> None:
    if not isinstance(provenance, TrainingPipelineProvenance):
        raise TypeError("provenance must be TrainingPipelineProvenance")
    if not provenance.source_revisions.fully_resolved:
        raise Phase4RawCorpusError(
            "persistent generation requires fully resolved source revisions"
        )
    if provenance.anchor_semantics_id != ANCHOR_SEMANTICS_ID:
        raise Phase4RawCorpusError("unexpected anchor semantics identity")
    if provenance.evidence_cutoff_semantics_id != EVIDENCE_CUTOFF_SEMANTICS_ID:
        raise Phase4RawCorpusError("unexpected evidence cutoff semantics identity")
    if provenance.label_semantics_id != LABEL_SEMANTICS_ID:
        raise Phase4RawCorpusError("unexpected label semantics identity")
    if provenance.effective_rules != effective_rule_provenance(RuleSet.default()):
        raise Phase4RawCorpusError("fixed corpus requires RuleSet.default() provenance")


def _json_loads(data: bytes, context: str) -> object:
    def reject_constant(value: str) -> None:
        raise Phase4RawCorpusError(f"{context} contains non-finite {value}")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Phase4RawCorpusError(f"{context} contains duplicate key {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase4RawCorpusError(f"{context} is not strict UTF-8 JSON") from error


def _identity_input(
    provenance: TrainingPipelineProvenance,
    ordered_seeds: tuple[int, ...],
    shards: tuple[ShardInfo, ...],
) -> dict[str, Any]:
    encoded = provenance_to_dict(provenance)
    return {
        "schema_version": SCHEMA_VERSION,
        "generation_protocol": GENERATION_PROTOCOL_ID,
        "source_class": "first-party-bootstrap",
        "effective_rules": encoded["effective_rules"],
        "source_revisions": encoded["source_revisions"],
        "phase2_semantics": {
            "anchor": encoded["anchor_semantics_id"],
            "evidence_cutoff": encoded["evidence_cutoff_semantics_id"],
            "label": encoded["label_semantics_id"],
        },
        "ordered_seeds": list(ordered_seeds),
        "shards": [
            {
                "shard_index": shard.shard_index,
                "seeds": list(shard.seeds),
                "canonical_sha256": shard.canonical_sha256,
            }
            for shard in shards
        ],
    }


def corpus_identity(
    provenance: TrainingPipelineProvenance,
    ordered_seeds: tuple[int, ...],
    shards: tuple[ShardInfo, ...],
) -> str:
    """Hash logical identity only; paths, timing and gzip bytes are excluded."""
    return _sha256(
        canonical_json_bytes(_identity_input(provenance, ordered_seeds, shards))
    )


def _manifest_value(
    provenance: TrainingPipelineProvenance,
    ordered_seeds: tuple[int, ...],
    shards: tuple[ShardInfo, ...],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation_protocol": GENERATION_PROTOCOL_ID,
        "source_class": "first-party-bootstrap",
        "provenance": provenance_to_dict(provenance),
        "ordered_seeds": list(ordered_seeds),
        "max_games_per_shard": MAX_GAMES_PER_SHARD,
        "shards": [
            {
                "shard_index": shard.shard_index,
                "filename": shard.filename,
                "seeds": list(shard.seeds),
                "canonical_sha256": shard.canonical_sha256,
                "uncompressed_bytes": shard.uncompressed_bytes,
                "compressed_bytes": shard.compressed_bytes,
            }
            for shard in shards
        ],
        "corpus_identity": corpus_identity(provenance, ordered_seeds, shards),
    }


def _gzip_bytes(data: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
        stream.write(data)
    return output.getvalue()


def save_raw_corpus(corpus: RawCorpus, destination: str | Path) -> PersistedRawCorpus:
    """Stage, validate, then atomically publish a complete corpus directory."""
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    _validate_provenance(corpus.provenance)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        shards = []
        for shard_index, start in enumerate(
            range(0, len(corpus.games), MAX_GAMES_PER_SHARD)
        ):
            games = corpus.games[start : start + MAX_GAMES_PER_SHARD]
            canonical = canonical_json_bytes(shard_value(games, shard_index))
            compressed = _gzip_bytes(canonical)
            filename = f"shard-{shard_index:03d}.json.gz"
            (stage / filename).write_bytes(compressed)
            shards.append(
                ShardInfo(
                    shard_index,
                    filename,
                    tuple(game.seed for game in games),
                    _sha256(canonical),
                    len(canonical),
                    len(compressed),
                )
            )
        shard_tuple = tuple(shards)
        manifest = _manifest_value(
            corpus.provenance,
            tuple(game.seed for game in corpus.games),
            shard_tuple,
        )
        (stage / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        validated = load_raw_corpus(stage)
        os.rename(stage, destination)
        return validated
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def _strict_manifest(value: object) -> dict[str, object]:
    keys = {
        "schema_version",
        "generation_protocol",
        "source_class",
        "provenance",
        "ordered_seeds",
        "max_games_per_shard",
        "shards",
        "corpus_identity",
    }
    if type(value) is not dict or set(value) != keys:
        raise Phase4RawCorpusError("manifest fields are not exact")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise Phase4RawCorpusError("unknown manifest schema version")
    if value["generation_protocol"] != GENERATION_PROTOCOL_ID:
        raise Phase4RawCorpusError("unknown generation protocol")
    if value["source_class"] != "first-party-bootstrap":
        raise Phase4RawCorpusError("unsupported source class")
    if (
        type(value["max_games_per_shard"]) is not int
        or value["max_games_per_shard"] != MAX_GAMES_PER_SHARD
    ):
        raise Phase4RawCorpusError("invalid max games per shard")
    _digest(value["corpus_identity"], "manifest.corpus_identity")
    return value


def load_raw_corpus(destination: str | Path) -> PersistedRawCorpus:
    destination = Path(destination)
    manifest_path = destination / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise Phase4RawCorpusError("manifest is missing")
    manifest_bytes = manifest_path.read_bytes()
    manifest = _strict_manifest(_json_loads(manifest_bytes, "manifest"))
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise Phase4RawCorpusError("manifest bytes are not canonical JSON")
    try:
        provenance = parse_provenance(manifest["provenance"])
    except (TypeError, ValueError) as error:
        raise Phase4RawCorpusError(f"invalid provenance: {error}") from error
    _validate_provenance(provenance)
    ordered_seeds_value = manifest["ordered_seeds"]
    if type(ordered_seeds_value) is not list or any(
        type(seed) is not int for seed in ordered_seeds_value
    ):
        raise Phase4RawCorpusError("ordered_seeds must be an integer array")
    ordered_seeds = tuple(ordered_seeds_value)
    if ordered_seeds != tuple(sorted(set(ordered_seeds))):
        raise Phase4RawCorpusError("ordered_seeds must be unique and ascending")
    rows = manifest["shards"]
    if type(rows) is not list:
        raise Phase4RawCorpusError("shards must be an array")
    expected_files = {MANIFEST_FILENAME}
    shards = []
    games = []
    for expected_index, row in enumerate(rows):
        keys = {
            "shard_index",
            "filename",
            "seeds",
            "canonical_sha256",
            "uncompressed_bytes",
            "compressed_bytes",
        }
        if type(row) is not dict or set(row) != keys:
            raise Phase4RawCorpusError("shard manifest fields are not exact")
        if type(row["shard_index"]) is not int or row["shard_index"] != expected_index:
            raise Phase4RawCorpusError("shards must be ordered contiguously")
        filename = row["filename"]
        if (
            type(filename) is not str
            or filename != f"shard-{expected_index:03d}.json.gz"
        ):
            raise Phase4RawCorpusError("shard filename is not canonical")
        seeds_value = row["seeds"]
        if (
            type(seeds_value) is not list
            or not 1 <= len(seeds_value) <= MAX_GAMES_PER_SHARD
            or any(type(seed) is not int for seed in seeds_value)
            or tuple(seeds_value) != tuple(sorted(set(seeds_value)))
        ):
            raise Phase4RawCorpusError("shard seeds must be unique ascending integers")
        canonical_digest = _digest(row["canonical_sha256"], "shard.canonical_sha256")
        uncompressed_bytes = _nonnegative_int(
            row["uncompressed_bytes"], "shard.uncompressed_bytes"
        )
        compressed_bytes = _nonnegative_int(
            row["compressed_bytes"], "shard.compressed_bytes"
        )
        expected_files.add(filename)
        path = destination / filename
        if not path.is_file():
            raise Phase4RawCorpusError(f"missing shard: {filename}")
        compressed = path.read_bytes()
        if len(compressed) != compressed_bytes:
            raise Phase4RawCorpusError("compressed byte count mismatch")
        try:
            canonical = gzip.decompress(compressed)
        except (gzip.BadGzipFile, EOFError) as error:
            raise Phase4RawCorpusError("invalid gzip shard") from error
        if len(canonical) != uncompressed_bytes:
            raise Phase4RawCorpusError("uncompressed byte count mismatch")
        if _sha256(canonical) != canonical_digest:
            raise Phase4RawCorpusError("shard digest mismatch")
        value = _json_loads(canonical, filename)
        shard_index, seeds, shard_games = parse_shard_value(value, filename)
        if canonical_json_bytes(shard_value(shard_games, shard_index)) != canonical:
            raise Phase4RawCorpusError("shard bytes are not canonical JSON")
        if list(seeds) != seeds_value:
            raise Phase4RawCorpusError("shard seed identity mismatch")
        shards.append(
            ShardInfo(
                shard_index,
                filename,
                seeds,
                canonical_digest,
                uncompressed_bytes,
                compressed_bytes,
            )
        )
        games.extend(shard_games)
    actual_entries = {path.name for path in destination.iterdir()}
    if actual_entries != expected_files:
        raise Phase4RawCorpusError("corpus contains missing or extra files")
    if tuple(game.seed for game in games) != ordered_seeds:
        raise Phase4RawCorpusError("manifest game order does not match shards")
    shard_tuple = tuple(shards)
    identity = corpus_identity(provenance, ordered_seeds, shard_tuple)
    if manifest["corpus_identity"] != identity:
        raise Phase4RawCorpusError("corpus canonical identity mismatch")
    corpus = RawCorpus(provenance=provenance, games=tuple(games))
    return PersistedRawCorpus(corpus, shard_tuple, identity)


__all__ = [
    "MANIFEST_FILENAME",
    "PersistedRawCorpus",
    "ShardInfo",
    "corpus_identity",
    "load_raw_corpus",
    "save_raw_corpus",
]
