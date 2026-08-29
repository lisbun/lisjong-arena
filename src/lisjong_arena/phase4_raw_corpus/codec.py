"""Explicit versioned JSON codec for Phase 4 raw values."""

import json
from typing import Any

from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat
from lisjong_engine.wind import Wind

from lisjong_arena.phase3_bootstrap_corpus.decoding import (
    parse_evidence,
    parse_observation,
    parse_provenance,
    parse_tile,
)
from lisjong_arena.phase3_bootstrap_corpus.encoding import (
    evidence_to_dict,
    observation_to_dict,
    provenance_to_dict,
    tile_to_dict,
)
from lisjong_arena.phase3_bootstrap_corpus.model import Phase3BootstrapArtifactError
from lisjong_arena.phase4_raw_corpus.model import (
    GENERATION_PROTOCOL_ID,
    SCHEMA_VERSION,
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    Phase4RawCorpusError,
    RawGame,
    RawRound,
    ViewerEvidence,
)


def _object(value: object, keys: set[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise Phase4RawCorpusError(f"{context} must be an object")
    if set(value) != keys:
        raise Phase4RawCorpusError(f"{context} fields must be exactly {sorted(keys)}")
    return value


def _list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise Phase4RawCorpusError(f"{context} must be an array")
    return value


def _int(value: object, context: str) -> int:
    if type(value) is not int:
        raise Phase4RawCorpusError(f"{context} must be an int")
    return value


def _enum(enum_type, value: object, context: str):
    if type(value) is not str:
        raise Phase4RawCorpusError(f"{context} must be a string enum")
    try:
        return enum_type(value)
    except ValueError:
        raise Phase4RawCorpusError(f"{context} has an invalid enum value") from None


def _checkpoint_to_dict(value: DecisionCheckpoint) -> dict[str, Any]:
    return {
        "checkpoint_index": value.checkpoint_index,
        "round_revision": value.round_revision,
        "decision_kind": value.decision_kind.value,
        "observation": observation_to_dict(value.observation),
        "evidence_cutoff": value.evidence_cutoff,
    }


def _truth_to_dict(value: CheckpointTruth) -> dict[str, Any]:
    return {
        "checkpoint_index": value.checkpoint_index,
        "viewer_seat": value.viewer_seat.value,
        "opponents": [
            {
                "opponent_seat": opponent.opponent_seat.value,
                "concealed_tiles": [
                    tile_to_dict(tile) for tile in opponent.concealed_tiles
                ],
            }
            for opponent in value.opponents
        ],
    }


def raw_game_to_dict(game: RawGame) -> dict[str, Any]:
    return {
        "seed": game.seed,
        "rounds": [
            {
                "round_index": raw_round.round_index,
                "prevailing_wind": raw_round.prevailing_wind.value,
                "hand_number": raw_round.hand_number,
                "dealer_seat": raw_round.dealer_seat.value,
                "honba": raw_round.honba,
                "viewer_evidence": [
                    {
                        "viewer_seat": stream.viewer_seat.value,
                        "evidence": [
                            evidence_to_dict(item) for item in stream.evidence
                        ],
                    }
                    for stream in raw_round.viewer_evidence
                ],
                "checkpoints": [
                    _checkpoint_to_dict(checkpoint)
                    for checkpoint in raw_round.checkpoints
                ],
                "training_truth": [
                    _truth_to_dict(truth) for truth in raw_round.training_truth
                ],
            }
            for raw_round in game.rounds
        ],
    }


def _parse_checkpoint(value: object, context: str) -> DecisionCheckpoint:
    obj = _object(
        value,
        {
            "checkpoint_index",
            "round_revision",
            "decision_kind",
            "observation",
            "evidence_cutoff",
        },
        context,
    )
    observation = parse_observation(obj["observation"], f"{context}.observation")
    decision_kind = _enum(
        ObservationDecisionKind, obj["decision_kind"], f"{context}.decision_kind"
    )
    if observation.decision_kind is not decision_kind:
        raise Phase4RawCorpusError("checkpoint decision kind duplicates must agree")
    return DecisionCheckpoint(
        checkpoint_index=_int(obj["checkpoint_index"], f"{context}.checkpoint_index"),
        round_revision=_int(obj["round_revision"], f"{context}.round_revision"),
        observation=observation,
        evidence_cutoff=_int(obj["evidence_cutoff"], f"{context}.evidence_cutoff"),
    )


def _parse_truth(value: object, context: str) -> CheckpointTruth:
    obj = _object(value, {"checkpoint_index", "viewer_seat", "opponents"}, context)
    opponents = []
    for index, item in enumerate(_list(obj["opponents"], f"{context}.opponents")):
        row_context = f"{context}.opponents[{index}]"
        row = _object(item, {"opponent_seat", "concealed_tiles"}, row_context)
        opponents.append(
            OpponentConcealedTruth(
                opponent_seat=_enum(
                    Seat, row["opponent_seat"], f"{row_context}.opponent_seat"
                ),
                concealed_tiles=tuple(
                    parse_tile(tile, f"{row_context}.concealed_tiles[{tile_index}]")
                    for tile_index, tile in enumerate(
                        _list(
                            row["concealed_tiles"],
                            f"{row_context}.concealed_tiles",
                        )
                    )
                ),
            )
        )
    return CheckpointTruth(
        checkpoint_index=_int(obj["checkpoint_index"], f"{context}.checkpoint_index"),
        viewer_seat=_enum(Seat, obj["viewer_seat"], f"{context}.viewer_seat"),
        opponents=tuple(opponents),
    )


def parse_raw_game(value: object, context: str = "game") -> RawGame:
    try:
        obj = _object(value, {"seed", "rounds"}, context)
        rounds = []
        for round_index, item in enumerate(_list(obj["rounds"], f"{context}.rounds")):
            round_context = f"{context}.rounds[{round_index}]"
            row = _object(
                item,
                {
                    "round_index",
                    "prevailing_wind",
                    "hand_number",
                    "dealer_seat",
                    "honba",
                    "viewer_evidence",
                    "checkpoints",
                    "training_truth",
                },
                round_context,
            )
            streams = []
            for viewer_index, stream_value in enumerate(
                _list(row["viewer_evidence"], f"{round_context}.viewer_evidence")
            ):
                stream_context = f"{round_context}.viewer_evidence[{viewer_index}]"
                stream = _object(
                    stream_value, {"viewer_seat", "evidence"}, stream_context
                )
                streams.append(
                    ViewerEvidence(
                        viewer_seat=_enum(
                            Seat,
                            stream["viewer_seat"],
                            f"{stream_context}.viewer_seat",
                        ),
                        evidence=tuple(
                            parse_evidence(
                                evidence,
                                f"{stream_context}.evidence[{evidence_index}]",
                            )
                            for evidence_index, evidence in enumerate(
                                _list(
                                    stream["evidence"],
                                    f"{stream_context}.evidence",
                                )
                            )
                        ),
                    )
                )
            rounds.append(
                RawRound(
                    round_index=_int(
                        row["round_index"], f"{round_context}.round_index"
                    ),
                    prevailing_wind=_enum(
                        Wind,
                        row["prevailing_wind"],
                        f"{round_context}.prevailing_wind",
                    ),
                    hand_number=_int(
                        row["hand_number"], f"{round_context}.hand_number"
                    ),
                    dealer_seat=_enum(
                        Seat, row["dealer_seat"], f"{round_context}.dealer_seat"
                    ),
                    honba=_int(row["honba"], f"{round_context}.honba"),
                    viewer_evidence=tuple(streams),
                    checkpoints=tuple(
                        _parse_checkpoint(
                            checkpoint, f"{round_context}.checkpoints[{i}]"
                        )
                        for i, checkpoint in enumerate(
                            _list(row["checkpoints"], f"{round_context}.checkpoints")
                        )
                    ),
                    training_truth=tuple(
                        _parse_truth(truth, f"{round_context}.training_truth[{i}]")
                        for i, truth in enumerate(
                            _list(
                                row["training_truth"],
                                f"{round_context}.training_truth",
                            )
                        )
                    ),
                )
            )
        return RawGame(seed=_int(obj["seed"], f"{context}.seed"), rounds=tuple(rounds))
    except Phase4RawCorpusError:
        raise
    except (Phase3BootstrapArtifactError, TypeError, ValueError) as error:
        raise Phase4RawCorpusError(f"{context}: {error}") from error


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Phase4RawCorpusError("value is not deterministic JSON") from error


def shard_value(games: tuple[RawGame, ...], shard_index: int) -> dict[str, Any]:
    if type(shard_index) is not int or shard_index < 0:
        raise ValueError("shard_index must be a non-negative int")
    games = tuple(games)
    if not games:
        raise ValueError("a shard must contain at least one game")
    return {
        "schema_version": SCHEMA_VERSION,
        "generation_protocol": GENERATION_PROTOCOL_ID,
        "shard_index": shard_index,
        "seeds": [game.seed for game in games],
        "games": [raw_game_to_dict(game) for game in games],
    }


def parse_shard_value(
    value: object, context: str = "shard"
) -> tuple[int, tuple[int, ...], tuple[RawGame, ...]]:
    obj = _object(
        value,
        {"schema_version", "generation_protocol", "shard_index", "seeds", "games"},
        context,
    )
    if _int(obj["schema_version"], f"{context}.schema_version") != SCHEMA_VERSION:
        raise Phase4RawCorpusError("unknown shard schema version")
    if obj["generation_protocol"] != GENERATION_PROTOCOL_ID:
        raise Phase4RawCorpusError("unknown generation protocol")
    seeds = tuple(
        _int(seed, f"{context}.seeds[{index}]")
        for index, seed in enumerate(_list(obj["seeds"], f"{context}.seeds"))
    )
    if not 1 <= len(seeds) <= 4 or seeds != tuple(sorted(set(seeds))):
        raise Phase4RawCorpusError("shard seeds must be 1..4 unique ascending values")
    games = tuple(
        parse_raw_game(game, f"{context}.games[{index}]")
        for index, game in enumerate(_list(obj["games"], f"{context}.games"))
    )
    if tuple(game.seed for game in games) != seeds:
        raise Phase4RawCorpusError("shard seeds and games must agree in order")
    shard_index = _int(obj["shard_index"], f"{context}.shard_index")
    if shard_index < 0:
        raise Phase4RawCorpusError("shard_index must be non-negative")
    canonical = shard_value(games, shard_index)
    if canonical != value:
        raise Phase4RawCorpusError("shard is not canonical")
    return shard_index, seeds, games


__all__ = [
    "canonical_json_bytes",
    "parse_provenance",
    "parse_raw_game",
    "parse_shard_value",
    "provenance_to_dict",
    "raw_game_to_dict",
    "shard_value",
]
