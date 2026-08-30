"""Strict compact persistence for the Phase 5 derived dataset manifest."""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes, parse_provenance
from lisjong_arena.phase4_raw_corpus.model import GENERATION_PROTOCOL_ID
from lisjong_arena.phase5_belief_dataset.model import (
    BUILDER_SEMANTICS_ID,
    DATASET_SCHEMA_VERSION,
    BeliefDataset,
    DatasetPartition,
    GameAssignment,
    GameIdentity,
    PartitionSummary,
    Phase5BeliefDatasetError,
    TargetAvailabilitySummary,
    TurnExampleReference,
)

DATASET_MANIFEST_FILENAME = "dataset.json"


@dataclass(frozen=True, slots=True)
class PersistedBeliefDataset:
    dataset: BeliefDataset
    byte_count: int


def _strict_json(data: bytes) -> object:
    def reject_constant(value: str) -> None:
        raise Phase5BeliefDatasetError(f"dataset contains non-finite {value}")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Phase5BeliefDatasetError(f"dataset contains duplicate key {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase5BeliefDatasetError(
            "dataset manifest is not strict UTF-8 JSON"
        ) from error


def _exact_dict(value: object, keys: set[str], context: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise Phase5BeliefDatasetError(f"{context} fields are not exact")
    return value


def _nonnegative_int(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise Phase5BeliefDatasetError(f"{context} must be a non-negative int")
    return value


def _availability(value: object, context: str) -> TargetAvailabilitySummary:
    row = _exact_dict(
        value,
        {
            "target_rows",
            "structural_wait_available",
            "structural_wait_unavailable",
            "structural_wait_all_zero",
            "structural_wait_non_zero",
            "unavailable_reasons",
        },
        context,
    )
    reasons_raw = row["unavailable_reasons"]
    if type(reasons_raw) is not list:
        raise Phase5BeliefDatasetError(
            f"{context}.unavailable_reasons must be an array"
        )
    reasons = []
    for index, item in enumerate(reasons_raw):
        reason = _exact_dict(item, {"reason", "count"}, f"{context}.reason[{index}]")
        if type(reason["reason"]) is not str:
            raise Phase5BeliefDatasetError("unavailable reason must be a str")
        reasons.append(
            (
                reason["reason"],
                _nonnegative_int(reason["count"], f"{context}.reason[{index}].count"),
            )
        )
    return TargetAvailabilitySummary(
        target_rows=_nonnegative_int(row["target_rows"], f"{context}.target_rows"),
        structural_wait_available=_nonnegative_int(
            row["structural_wait_available"], f"{context}.available"
        ),
        structural_wait_unavailable=_nonnegative_int(
            row["structural_wait_unavailable"], f"{context}.unavailable"
        ),
        structural_wait_all_zero=_nonnegative_int(
            row["structural_wait_all_zero"], f"{context}.all_zero"
        ),
        structural_wait_non_zero=_nonnegative_int(
            row["structural_wait_non_zero"], f"{context}.non_zero"
        ),
        unavailable_reasons=tuple(reasons),
    )


def _parse_dataset(value: object) -> BeliefDataset:
    row = _exact_dict(
        value,
        {
            "dataset_schema_version",
            "raw_generation_protocol_id",
            "raw_corpus_identity",
            "provenance",
            "builder_semantics_id",
            "split_policy_id",
            "ordered_games",
            "ordered_examples",
            "partition_summaries",
            "dataset_identity",
        },
        "dataset",
    )
    if row["dataset_schema_version"] != DATASET_SCHEMA_VERSION:
        raise Phase5BeliefDatasetError("unknown dataset schema version")
    if row["raw_generation_protocol_id"] != GENERATION_PROTOCOL_ID:
        raise Phase5BeliefDatasetError("unknown raw generation protocol")
    if row["builder_semantics_id"] != BUILDER_SEMANTICS_ID:
        raise Phase5BeliefDatasetError("unknown builder semantics identity")
    try:
        provenance = parse_provenance(row["provenance"])
    except (TypeError, ValueError) as error:
        raise Phase5BeliefDatasetError(f"invalid provenance: {error}") from error

    games_raw = row["ordered_games"]
    if type(games_raw) is not list:
        raise Phase5BeliefDatasetError("ordered_games must be an array")
    games = []
    for index, item in enumerate(games_raw):
        game = _exact_dict(
            item, {"source_class", "game_seed", "partition"}, f"game[{index}]"
        )
        try:
            games.append(
                GameAssignment(
                    GameIdentity(game["source_class"], game["game_seed"]),
                    DatasetPartition(game["partition"]),
                )
            )
        except (TypeError, ValueError) as error:
            raise Phase5BeliefDatasetError(f"invalid game[{index}]: {error}") from error

    examples_raw = row["ordered_examples"]
    if type(examples_raw) is not list:
        raise Phase5BeliefDatasetError("ordered_examples must be an array")
    examples = []
    for index, item in enumerate(examples_raw):
        example = _exact_dict(
            item,
            {
                "example_identity",
                "source_class",
                "game_seed",
                "partition",
                "round_index",
                "checkpoint_index",
                "anchor_index",
                "hand_number",
                "honba",
                "round_revision",
                "viewer_seat",
            },
            f"example[{index}]",
        )
        try:
            reference = TurnExampleReference(
                game=GameIdentity(example["source_class"], example["game_seed"]),
                partition=DatasetPartition(example["partition"]),
                round_index=example["round_index"],
                checkpoint_index=example["checkpoint_index"],
                anchor_index=example["anchor_index"],
                hand_number=example["hand_number"],
                honba=example["honba"],
                round_revision=example["round_revision"],
                viewer_seat=Seat(example["viewer_seat"]),
            )
        except (TypeError, ValueError) as error:
            raise Phase5BeliefDatasetError(
                f"invalid example[{index}]: {error}"
            ) from error
        if reference.identity != example["example_identity"]:
            raise Phase5BeliefDatasetError("example identity mismatch")
        examples.append(reference)

    summaries_raw = row["partition_summaries"]
    if type(summaries_raw) is not list:
        raise Phase5BeliefDatasetError("partition_summaries must be an array")
    summaries = []
    for index, item in enumerate(summaries_raw):
        summary = _exact_dict(
            item,
            {"partition", "sample_count", "target_availability"},
            f"summary[{index}]",
        )
        try:
            summaries.append(
                PartitionSummary(
                    DatasetPartition(summary["partition"]),
                    _nonnegative_int(
                        summary["sample_count"], f"summary[{index}].sample_count"
                    ),
                    _availability(
                        summary["target_availability"],
                        f"summary[{index}].target_availability",
                    ),
                )
            )
        except (TypeError, ValueError) as error:
            raise Phase5BeliefDatasetError(
                f"invalid summary[{index}]: {error}"
            ) from error

    try:
        dataset = BeliefDataset(
            raw_corpus_identity=row["raw_corpus_identity"],
            provenance=provenance,
            builder_semantics_id=row["builder_semantics_id"],
            split_policy_id=row["split_policy_id"],
            games=tuple(games),
            examples=tuple(examples),
            partition_summaries=tuple(summaries),
        )
    except (TypeError, ValueError) as error:
        raise Phase5BeliefDatasetError(f"invalid dataset: {error}") from error
    if dataset.dataset_identity != row["dataset_identity"]:
        raise Phase5BeliefDatasetError("dataset canonical identity mismatch")
    return dataset


def _manifest_value(dataset: BeliefDataset) -> dict[str, object]:
    return {**dataset.identity_value(), "dataset_identity": dataset.dataset_identity}


def save_belief_dataset(
    dataset: BeliefDataset, destination: str | Path
) -> PersistedBeliefDataset:
    if not isinstance(dataset, BeliefDataset):
        raise TypeError("dataset must be a BeliefDataset")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        data = canonical_json_bytes(_manifest_value(dataset))
        (stage / DATASET_MANIFEST_FILENAME).write_bytes(data)
        validated = load_belief_dataset(stage)
        os.rename(stage, destination)
        return validated
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def load_belief_dataset(destination: str | Path) -> PersistedBeliefDataset:
    destination = Path(destination)
    path = destination / DATASET_MANIFEST_FILENAME
    if not path.is_file():
        raise Phase5BeliefDatasetError("dataset manifest is missing")
    if {entry.name for entry in destination.iterdir()} != {DATASET_MANIFEST_FILENAME}:
        raise Phase5BeliefDatasetError("dataset contains missing or extra files")
    data = path.read_bytes()
    value = _strict_json(data)
    if canonical_json_bytes(value) != data:
        raise Phase5BeliefDatasetError("dataset bytes are not canonical JSON")
    return PersistedBeliefDataset(_parse_dataset(value), len(data))


__all__ = [
    "DATASET_MANIFEST_FILENAME",
    "PersistedBeliefDataset",
    "load_belief_dataset",
    "save_belief_dataset",
]
