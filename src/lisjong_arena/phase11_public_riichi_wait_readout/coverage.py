"""Pre-exposure public-riichi coverage audit."""

import json
from pathlib import Path

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition

from .protocol import (
    ELIGIBILITY_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    SCHEMA,
    TARGET_SEMANTICS_ID,
    TILE_KIND_COUNT,
    Phase11Error,
    canonical_json_bytes,
    exact,
    identity,
)

PARTITION_FIELDS = (
    "established_rows",
    "eligible_rows",
    "unavailable_rows",
    "available_all_zero_rows",
    "positive_wait_cells",
    "per_tile_positives",
    "eligible_hanchan",
    "eligible_hanchan_identities",
    "all_zero_audit",
)
COVERAGE_FIELDS = (
    "schema",
    "execution_lock_identity",
    "eligibility_semantics",
    "label_semantics",
    "target_semantics",
    "partitions",
    "train_prevalence_baseline",
    "semantic_valid",
)


def _audit_row(record, target) -> dict[str, object]:
    return {
        "source_class": record.source_class,
        "game_seed": record.game_seed,
        "round_index": record.round_index,
        "anchor_identity": record.anchor_identity,
        "depth": record.depth,
        "opponent_wind": target.wind,
        "opponent_seat": target.seat,
        "riichi_junme": target.riichi_junme,
        "open_closed": target.open_closed,
    }


def _partition_value(records: tuple, partition: DatasetPartition) -> dict[str, object]:
    selected = tuple(record for record in records if record.partition is partition)
    established = tuple(
        (record, target)
        for record in selected
        for target in record.targets
        if target.established
    )
    available = tuple(row for row in established if row[1].mask is not None)
    unavailable = tuple(row for row in established if row[1].mask is None)
    all_zero = tuple(row for row in available if not any(row[1].mask))
    eligible_games = sorted(
        {(row[0].source_class, row[0].game_seed) for row in available}
    )
    positives = [0] * TILE_KIND_COUNT
    for _record, target in available:
        for index, value in enumerate(target.mask):
            positives[index] += value
    return {
        "established_rows": len(established),
        "eligible_rows": len(available),
        "unavailable_rows": len(unavailable),
        "available_all_zero_rows": len(all_zero),
        "positive_wait_cells": sum(positives),
        "per_tile_positives": positives,
        "eligible_hanchan": len(eligible_games),
        "eligible_hanchan_identities": [
            {"source_class": source_class, "game_seed": game_seed}
            for source_class, game_seed in eligible_games
        ],
        "all_zero_audit": [_audit_row(*row) for row in all_zero],
    }


def build_coverage(records: tuple, execution_lock_identity: str) -> dict[str, object]:
    from .data import partition_records
    from .evaluation import fit_train_prevalence

    train_records = partition_records(records, DatasetPartition.TRAIN)
    value = {
        "schema": SCHEMA + "/coverage",
        "execution_lock_identity": execution_lock_identity,
        "eligibility_semantics": ELIGIBILITY_SEMANTICS_ID,
        "label_semantics": LABEL_SEMANTICS_ID,
        "target_semantics": TARGET_SEMANTICS_ID,
        "partitions": {
            partition.value: _partition_value(records, partition)
            for partition in (DatasetPartition.TRAIN, DatasetPartition.VALIDATION)
        },
        "train_prevalence_baseline": fit_train_prevalence(train_records),
        "semantic_valid": True,
    }
    value["semantic_valid"] = all(
        row["available_all_zero_rows"] == 0 for row in value["partitions"].values()
    )
    return validate_coverage(value, execution_lock_identity)


def validate_coverage(value: object, execution_lock_identity: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(COVERAGE_FIELDS):
        raise Phase11Error("coverage fields are not exact")
    exact(value["schema"], SCHEMA + "/coverage", "coverage schema")
    exact(
        value["execution_lock_identity"],
        execution_lock_identity,
        "coverage execution lock binding",
    )
    exact(value["eligibility_semantics"], ELIGIBILITY_SEMANTICS_ID, "eligibility")
    exact(value["label_semantics"], LABEL_SEMANTICS_ID, "label semantics")
    exact(value["target_semantics"], TARGET_SEMANTICS_ID, "target semantics")
    if type(value["semantic_valid"]) is not bool:
        raise Phase11Error("coverage semantic_valid must be a JSON boolean")
    partitions = value["partitions"]
    if type(partitions) is not dict or set(partitions) != {"train", "validation"}:
        raise Phase11Error("coverage partitions are not exact")
    expected_valid = True
    for name, row in partitions.items():
        if type(row) is not dict or set(row) != set(PARTITION_FIELDS):
            raise Phase11Error(f"{name} coverage fields are not exact")
        for field in PARTITION_FIELDS[:5] + ("eligible_hanchan",):
            if type(row[field]) is not int or row[field] < 0:
                raise Phase11Error(f"{name}.{field} must be a nonnegative JSON int")
        positives = row["per_tile_positives"]
        if (
            type(positives) is not list
            or len(positives) != TILE_KIND_COUNT
            or any(type(item) is not int or item < 0 for item in positives)
        ):
            raise Phase11Error(f"{name} per-tile positives are invalid")
        games = row["eligible_hanchan_identities"]
        if (
            type(games) is not list
            or len(games) != row["eligible_hanchan"]
            or any(
                type(item) is not dict
                or set(item) != {"source_class", "game_seed"}
                or type(item["source_class"]) is not str
                or not item["source_class"]
                or type(item["game_seed"]) is not int
                for item in games
            )
            or games
            != sorted(games, key=lambda item: (item["source_class"], item["game_seed"]))
            or len({(item["source_class"], item["game_seed"]) for item in games})
            != len(games)
        ):
            raise Phase11Error(f"{name} eligible hanchan identities are invalid")
        exact(sum(positives), row["positive_wait_cells"], f"{name} positives")
        if row["eligible_rows"] + row["unavailable_rows"] != row["established_rows"]:
            raise Phase11Error(f"{name} established row accounting differs")
        audit = row["all_zero_audit"]
        if type(audit) is not list or len(audit) != row["available_all_zero_rows"]:
            raise Phase11Error(f"{name} all-zero audit count differs")
        for item in audit:
            if type(item) is not dict or set(item) != {
                "source_class",
                "game_seed",
                "round_index",
                "anchor_identity",
                "depth",
                "opponent_wind",
                "opponent_seat",
                "riichi_junme",
                "open_closed",
            }:
                raise Phase11Error(f"{name} all-zero audit fields are not exact")
            for field in (
                "game_seed",
                "round_index",
                "depth",
                "opponent_seat",
                "riichi_junme",
            ):
                if type(item[field]) is not int or item[field] < 0:
                    raise Phase11Error(f"{name} all-zero audit integer is invalid")
            for field in ("source_class", "anchor_identity", "opponent_wind"):
                if type(item[field]) is not str or not item[field]:
                    raise Phase11Error(f"{name} all-zero audit identity is invalid")
            if item["open_closed"] not in ("open", "closed"):
                raise Phase11Error(
                    f"{name} all-zero audit open/closed value is invalid"
                )
        expected_valid = expected_valid and row["available_all_zero_rows"] == 0
    from .evaluation import validate_baseline

    validate_baseline(value["train_prevalence_baseline"], partitions["train"])
    exact(value["semantic_valid"], expected_valid, "coverage semantic validity")
    return value


def save_coverage(path: str | Path, value: dict, lock_identity: str) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"coverage destination already exists: {destination}")
    validate_coverage(value, lock_identity)
    payload = dict(value)
    payload["coverage_identity"] = identity(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_coverage(path: str | Path, lock_identity: str) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("coverage is not valid JSON") from error
    if canonical_json_bytes(payload) != data or type(payload) is not dict:
        raise Phase11Error("coverage bytes are not canonical JSON")
    recorded = payload.pop("coverage_identity", None)
    exact(recorded, identity(payload), "coverage identity")
    validate_coverage(payload, lock_identity)
    payload["coverage_identity"] = recorded
    return payload


__all__ = [
    "build_coverage",
    "load_coverage",
    "save_coverage",
    "validate_coverage",
]
