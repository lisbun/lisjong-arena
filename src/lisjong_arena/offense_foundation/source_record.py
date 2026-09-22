"""Reusable player-safe source records for Offense Foundation O0.

This artifact is deliberately independent from the locked #331 scientific corpus.
It retains typed PolicyInput-equivalent observations and canonical InternalAction
values so future lisjong-owned Learning code can rematerialize representations
without replaying the source hanchan.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from lisjong.action_vocabulary import encode_action

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import ArtifactValidationError, parse_json_text
from lisjong_arena.durable_local_game_record import (
    _action_to_value,
    _parse_action,
    _parse_policy_input,
    _policy_input_to_value,
)

from .protocol import GAME_MODE, ordered_games
from .qualification import read_document, seal, unseal, write_document
from .semantics import OffenseError

SOURCE_SCHEMA_V1 = "arena-offense-o0-player-safe-source-record-v1"
"""Historical schema identity. Frozen: no allocation provenance field, and
new generation always writes ``SOURCE_SCHEMA_V2`` instead.

This module still recognizes the schema string and its (narrower) manifest
field shape. That is a distinct, weaker claim than "a genuinely historical
(pre-lisbun/lisjong-arena#346/#347) corpus can be read end to end today":
``read_source_record()`` calls ``read_corpus() -> validate_lock() ->
make_lock() -> validate_request()`` before it inspects this schema at all,
and #347 already narrowed ``validate_request()`` to require
``allocation_bindings`` unconditionally. A lock built from a genuinely
pre-#347 request (``known_used_seeds``/``freshness_evidence``, no
``allocation_bindings``) is rejected there, independent of this schema.
That is a pre-existing consequence of #347, not something this module
changes or restores; see ``tests/test_offense_source_record.py``'s
``test_a_genuinely_pre_347_protocol_lock_is_rejected_upstream_of_the_schema``.
"""

SOURCE_SCHEMA_V2 = "arena-offense-o0-player-safe-source-record-v2"
"""Adds ``allocation_bindings``: the exact per-split Arena seed-allocation
binding (lisbun/lisjong-arena#346/#347) that produced the request population,
copied verbatim from the locked ``request.allocation_bindings``. This lets a
standalone reader of the source record alone (no access to the live seed
ledger or the full protocol lock) see and propagate the allocation provenance
that authorized the underlying seeds.
"""

SUPPORTED_SOURCE_SCHEMAS = frozenset({SOURCE_SCHEMA_V1, SOURCE_SCHEMA_V2})

SOURCE_KIND = "player-safe-source-record"
SOURCE_FILENAME = "source-record.jsonl"

_ROW_FIELDS = {
    "game_ordinal",
    "seed",
    "split",
    "step_ordinal",
    "decision_ordinal",
    "actor_seat",
    "policy_input",
    "legal_actions",
    "teacher_selected_action",
}


def _canonical_line(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _file_info(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def encode_observation(
    observation,
    *,
    game_ordinal: int,
    split: str,
    seed: int,
    step_ordinal: int,
    decision_ordinal: int,
) -> dict[str, object]:
    """Project one actual teacher decision onto the player-safe source schema."""
    seat = int(observation.seat)
    policy_input = observation.policy_input
    trace = observation.decision_trace
    if int(policy_input.self_seat) != seat:
        raise OffenseError("source-record PolicyInput seat mismatch")
    if trace.selected_action not in trace.legal_actions:
        raise OffenseError("source-record selected action is not legal")
    if any(int(action.actor) != seat for action in trace.legal_actions):
        raise OffenseError("source-record legal action actor mismatch")

    legal = [
        _action_to_value(action, f"legal_actions[{index}]")
        for index, action in enumerate(trace.legal_actions)
    ]
    legal.sort(key=lambda value: _canonical_line(value))
    serialized_legal = [_canonical_line(value) for value in legal]
    if len(serialized_legal) != len(set(serialized_legal)):
        raise OffenseError("source-record duplicate canonical legal action")

    selected = _action_to_value(trace.selected_action, "teacher_selected_action")
    if _canonical_line(selected) not in set(serialized_legal):
        raise OffenseError("source-record canonical selected action is not legal")

    return {
        "game_ordinal": game_ordinal,
        "seed": seed,
        "split": split,
        "step_ordinal": step_ordinal,
        "decision_ordinal": decision_ordinal,
        "actor_seat": seat,
        "policy_input": _policy_input_to_value(policy_input),
        "legal_actions": legal,
        "teacher_selected_action": selected,
    }


def write_game(
    path: Path,
    *,
    game_ordinal: int,
    split: str,
    seed: int,
    lock_identity: str,
    result,
    inspection,
):
    """Write one source hanchan from the already-executed canonical inspection."""
    if result.seed != seed or result.game_mode != GAME_MODE:
        raise OffenseError("source-record executed game identity mismatch")
    path.mkdir()
    payload = path / SOURCE_FILENAME
    ordinal = 0
    with payload.open("x", encoding="utf-8", newline="\n") as stream:
        for expected_step, step in enumerate(inspection.step_observations):
            if step.step_ordinal != expected_step:
                raise OffenseError("source-record noncontiguous execution steps")
            previous_seat = -1
            for observation in step.seat_decisions:
                if int(observation.seat) <= previous_seat:
                    raise OffenseError(
                        "source-record noncanonical execution seat order"
                    )
                previous_seat = int(observation.seat)
                row = encode_observation(
                    observation,
                    game_ordinal=game_ordinal,
                    split=split,
                    seed=seed,
                    step_ordinal=step.step_ordinal,
                    decision_ordinal=ordinal,
                )
                stream.write(_canonical_line(row))
                ordinal += 1
    if ordinal != result.decisions or len(inspection.step_observations) != result.steps:
        raise OffenseError("source-record incomplete game/decision accounting")
    return seal(
        {
            "game_ordinal": game_ordinal,
            "seed": seed,
            "split": split,
            "lock_identity": lock_identity,
            "game_mode": GAME_MODE,
            "decision_count": ordinal,
            "steps": result.steps,
            "files": {SOURCE_FILENAME: _file_info(payload)},
        }
    )


def build_manifest(lock, scientific_corpus_identity, game_summaries):
    """Seal an independent source-record manifest without entering corpus identity.

    Always writes the current ``SOURCE_SCHEMA_V2``: every lock built by
    ``protocol.make_lock`` since #347 already carries a
    ``request.allocation_bindings`` entry for each population split, so the
    allocation provenance backing this generation run is always available
    here and is copied through verbatim (Arena remains the sole owner and
    generator of the allocation ledger; this only republishes the exact
    binding that already authorized the request).
    """
    return seal(
        {
            "schema": SOURCE_SCHEMA_V2,
            "kind": SOURCE_KIND,
            "lock_identity": lock["identity"],
            "scientific_corpus_identity": scientific_corpus_identity,
            "game_mode": GAME_MODE,
            "source_contract": lock["qualification"]["binding"],
            "allocation_bindings": lock["request"]["allocation_bindings"],
            "games": game_summaries,
        }
    )


def write_manifest(path: Path, lock, scientific_corpus_identity, game_summaries):
    manifest = build_manifest(lock, scientific_corpus_identity, game_summaries)
    write_document(path / "manifest.json", manifest)
    return manifest


def _read_row(row: object):
    if type(row) is not dict or set(row) != _ROW_FIELDS:
        raise OffenseError("invalid source-record decision fields")
    for field in (
        "game_ordinal",
        "seed",
        "step_ordinal",
        "decision_ordinal",
        "actor_seat",
    ):
        if type(row[field]) is not int or row[field] < 0:
            raise OffenseError(f"invalid source-record integer field: {field}")
    if type(row["split"]) is not str or not row["split"]:
        raise OffenseError("invalid source-record split")

    try:
        policy_input = _parse_policy_input(row["policy_input"], "policy_input")
        if _policy_input_to_value(policy_input) != row["policy_input"]:
            raise OffenseError("source-record PolicyInput roundtrip mismatch")

        legal_values = row["legal_actions"]
        if type(legal_values) is not list or not legal_values:
            raise OffenseError("source-record legal_actions must be nonempty")
        legal_actions = tuple(
            _parse_action(value, f"legal_actions[{index}]")
            for index, value in enumerate(legal_values)
        )
        roundtripped_legal = [
            _action_to_value(action, f"legal_actions[{index}]")
            for index, action in enumerate(legal_actions)
        ]
        if roundtripped_legal != legal_values:
            raise OffenseError("source-record legal action roundtrip mismatch")
        canonical_legal = [_canonical_line(value) for value in legal_values]
        if canonical_legal != sorted(canonical_legal) or len(canonical_legal) != len(
            set(canonical_legal)
        ):
            raise OffenseError("source-record legal actions are not canonical")

        selected = _parse_action(
            row["teacher_selected_action"], "teacher_selected_action"
        )
        if (
            _action_to_value(selected, "teacher_selected_action")
            != row["teacher_selected_action"]
        ):
            raise OffenseError("source-record selected action roundtrip mismatch")
    except OffenseError:
        raise
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise OffenseError(f"invalid source-record typed value: {error}") from error

    seat = row["actor_seat"]
    if int(policy_input.self_seat) != seat:
        raise OffenseError("source-record PolicyInput seat mismatch")
    if any(int(action.actor) != seat for action in legal_actions):
        raise OffenseError("source-record legal action actor mismatch")
    if int(selected.actor) != seat or selected not in legal_actions:
        raise OffenseError("source-record selected action mismatch")
    return policy_input, legal_actions, selected


def _read_game(
    path: Path,
    game: object,
    *,
    game_ordinal: int,
    split: str,
    seed: int,
    lock_identity: str,
    corpus_game_path: Path,
):
    body = unseal(game)
    if set(body) != {
        "game_ordinal",
        "seed",
        "split",
        "lock_identity",
        "game_mode",
        "decision_count",
        "steps",
        "files",
    }:
        raise OffenseError("invalid source-record game fields")
    for field in ("game_ordinal", "seed", "decision_count", "steps"):
        if type(game.get(field)) is not int or game[field] < 0:
            raise OffenseError("invalid source-record game count/identity")
    if (
        game["game_ordinal"],
        game["seed"],
        game["split"],
        game["lock_identity"],
        game["game_mode"],
    ) != (game_ordinal, seed, split, lock_identity, GAME_MODE):
        raise OffenseError("source-record game population/provenance mismatch")
    if set(game["files"]) != {SOURCE_FILENAME} or {
        child.name for child in path.iterdir()
    } != {SOURCE_FILENAME}:
        raise OffenseError("missing/unexpected source-record game payload")
    payload = path / SOURCE_FILENAME
    if _file_info(payload) != game["files"][SOURCE_FILENAME]:
        raise OffenseError("source-record payload checksum/size mismatch")

    corpus_rows_path = corpus_game_path / "rows.jsonl"
    total, last_step, last_seat = 0, -1, -1
    sentinel = object()
    with (
        payload.open(encoding="utf-8") as source_rows,
        corpus_rows_path.open(encoding="utf-8") as scientific_rows,
    ):
        for source_line, scientific_line in itertools.zip_longest(
            source_rows, scientific_rows, fillvalue=sentinel
        ):
            if source_line is sentinel or scientific_line is sentinel:
                raise OffenseError("source-record/scientific decision count mismatch")
            source_row = parse_json_text(source_line)
            if source_line != _canonical_line(source_row):
                raise OffenseError("source-record row is not canonical JSON")
            _, legal_actions, selected = _read_row(source_row)
            if (
                source_row["game_ordinal"] != game_ordinal
                or source_row["seed"] != seed
                or source_row["split"] != split
            ):
                raise OffenseError("source-record row provenance mismatch")
            step, seat = source_row["step_ordinal"], source_row["actor_seat"]
            if (
                source_row["decision_ordinal"] != total
                or step not in (last_step, last_step + 1)
                or (step == last_step and seat <= last_seat)
            ):
                raise OffenseError(
                    "source-record decision ordering/accounting mismatch"
                )
            last_step, last_seat = step, seat

            scientific = parse_json_text(scientific_line)
            if (
                scientific["step_ordinal"] != step
                or scientific["decision_ordinal"] != total
                or scientific["actor_seat"] != seat
                or scientific["legal_indices"]
                != sorted(encode_action(action) for action in legal_actions)
                or scientific["teacher_action_index"] != encode_action(selected)
            ):
                raise OffenseError(
                    "source-record canonical action/scientific row mismatch"
                )
            total += 1

    if (total, last_step + 1) != (game["decision_count"], game["steps"]):
        raise OffenseError("source-record game accounting mismatch")


def _require_allocation_bindings(manifest, expected_lock):
    """Bind the manifest's per-split allocation provenance to the exact
    binding recorded in the locked ``request.allocation_bindings``.

    ``lock_identity`` already ties the manifest to one exact sealed lock, so
    this is defense-in-depth (matching how ``source_contract`` is also
    re-checked explicitly above), and it validates the binding *shape* the
    same way a standalone downstream reader must: no access to the live
    ledger is required or performed here, only the shape and the exact
    values already authorized by this lock.
    """
    expected_bindings = expected_lock["request"]["allocation_bindings"]
    bindings = manifest["allocation_bindings"]
    populations = expected_lock["request"]["populations"]
    if type(bindings) is not dict or set(bindings) != set(populations):
        raise OffenseError("source-record allocation bindings do not match splits")
    for split, seeds in populations.items():
        try:
            binding = seed_registry.validate_binding_shape(bindings[split], seeds=seeds)
        except seed_registry.SeedRegistryError as error:
            raise OffenseError(
                f"invalid {split} source-record allocation binding: {error}"
            ) from error
        if binding != expected_bindings[split]:
            raise OffenseError(
                f"source-record {split} allocation binding differs from the locked request"
            )


def read_source_record(path, *, expected_lock, corpus_path):
    """Strict-read source records and bind every decision to the locked corpus."""
    from .corpus import read_corpus

    path = Path(path)
    corpus_path = Path(corpus_path)
    corpus = read_corpus(corpus_path, expected_lock=expected_lock)
    manifest = read_document(path / "manifest.json")
    body = unseal(manifest)
    schema = body.get("schema")
    if schema not in SUPPORTED_SOURCE_SCHEMAS:
        raise OffenseError("unsupported source-record schema")
    expected_fields = {
        "schema",
        "kind",
        "lock_identity",
        "scientific_corpus_identity",
        "game_mode",
        "source_contract",
        "games",
    }
    if schema == SOURCE_SCHEMA_V2:
        expected_fields = expected_fields | {"allocation_bindings"}
    if set(body) != expected_fields:
        raise OffenseError("invalid source-record manifest fields")
    if (
        manifest["kind"] != SOURCE_KIND
        or manifest["lock_identity"] != expected_lock["identity"]
        or manifest["scientific_corpus_identity"] != corpus["identity"]
        or manifest["game_mode"] != GAME_MODE
        or manifest["source_contract"] != expected_lock["qualification"]["binding"]
    ):
        raise OffenseError("source-record schema/provenance identity mismatch")
    if schema == SOURCE_SCHEMA_V2:
        _require_allocation_bindings(manifest, expected_lock)

    games = ordered_games(expected_lock)
    if type(manifest["games"]) is not list or len(manifest["games"]) != len(games):
        raise OffenseError("source-record missing/extra hanchan")
    expected_names = {"manifest.json", *(f"game-{i:03d}" for i in range(len(games)))}
    if {child.name for child in path.iterdir()} != expected_names:
        raise OffenseError("missing/unexpected source-record files")
    if len(corpus["games"]) != len(games):
        raise OffenseError("source-record scientific corpus game count mismatch")

    for game_ordinal, ((split, seed), game) in enumerate(
        zip(games, manifest["games"], strict=True)
    ):
        _read_game(
            path / f"game-{game_ordinal:03d}",
            game,
            game_ordinal=game_ordinal,
            split=split,
            seed=seed,
            lock_identity=expected_lock["identity"],
            corpus_game_path=corpus_path / f"game-{game_ordinal:03d}",
        )
        if game["decision_count"] != corpus["games"][game_ordinal]["decision_count"]:
            raise OffenseError("source-record/scientific game accounting mismatch")
    return manifest


__all__ = [
    "SOURCE_FILENAME",
    "SOURCE_KIND",
    "SOURCE_SCHEMA_V1",
    "SOURCE_SCHEMA_V2",
    "SUPPORTED_SOURCE_SCHEMAS",
    "build_manifest",
    "encode_observation",
    "read_source_record",
    "write_game",
    "write_manifest",
]
