"""Exact #172 frozen latents, coverage, and TRAIN-only centering for #291."""

import hashlib
import math
import struct

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    frozen_latent_records,
    partition_records,
)
from lisjong_arena.phase11_public_riichi_wait_readout.retained import (
    load_retained,
    retained_readback_value,
)

from .protocol import (
    CENTERED_MEAN_ABSOLUTE_TOLERANCE,
    CENTERING_SEMANTICS_ID,
    FROZEN_E160_STATE_DIGEST,
    LATENT_DIM,
    LATENT_FINGERPRINT_SEMANTICS_ID,
    OUTPUT_ROWS,
    TILE_KIND_COUNT,
    E160OffsetProbeError,
    exact,
    identity,
)


def _latent_values(latent) -> tuple[float, ...]:
    if hasattr(latent, "detach"):
        values = latent.detach().cpu().reshape(-1).tolist()
    else:
        values = list(latent)
    if len(values) != LATENT_DIM:
        raise E160OffsetProbeError("latent dimension differs from 128")
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise E160OffsetProbeError("latent contains a non-finite value")
    return result


def coverage_value(records: tuple) -> dict[str, object]:
    if not records:
        raise E160OffsetProbeError("coverage requires retained records")
    eligible_rows = 0
    unavailable_rows = 0
    all_zero_rows = 0
    eligible_games = set()
    per_output_row = [0] * OUTPUT_ROWS
    for record in records:
        if len(record.targets) != OUTPUT_ROWS:
            raise E160OffsetProbeError("record must contain exactly three target rows")
        for row_index, target in enumerate(record.targets):
            if not target.established:
                continue
            if target.mask is None:
                unavailable_rows += 1
                continue
            if len(target.mask) != TILE_KIND_COUNT:
                raise E160OffsetProbeError(
                    "structural-wait mask must contain 34 values"
                )
            eligible_rows += 1
            per_output_row[row_index] += 1
            eligible_games.add((record.source_class, record.game_seed))
            if not any(target.mask):
                all_zero_rows += 1
            for value in target.mask:
                if type(value) is not int or value not in (0, 1):
                    raise E160OffsetProbeError("structural-wait label must be binary")
    return {
        "eligible_hanchan": len(eligible_games),
        "eligible_rows": eligible_rows,
        "eligible_cells": eligible_rows * TILE_KIND_COUNT,
        "unavailable_rows": unavailable_rows,
        "all_zero_rows": all_zero_rows,
        "eligible_rows_by_output_row": per_output_row,
    }


def centering_receipt(records: tuple) -> dict[str, object]:
    if not records or any(
        record.partition is not DatasetPartition.TRAIN for record in records
    ):
        raise E160OffsetProbeError("latent centering accepts TRAIN records only")
    sums = [[0.0] * LATENT_DIM for _ in range(OUTPUT_ROWS)]
    counts = [0] * OUTPUT_ROWS
    for record in records:
        latent = _latent_values(record.latent)
        for row_index, target in enumerate(record.targets):
            if not target.eligible:
                continue
            counts[row_index] += 1
            for index, value in enumerate(latent):
                sums[row_index][index] += value
    if any(count <= 0 for count in counts):
        raise E160OffsetProbeError("every output row requires TRAIN eligible examples")
    means = [
        [value / counts[row_index] for value in sums[row_index]]
        for row_index in range(OUTPUT_ROWS)
    ]
    residual_sums = [[0.0] * LATENT_DIM for _ in range(OUTPUT_ROWS)]
    for record in records:
        latent = _latent_values(record.latent)
        for row_index, target in enumerate(record.targets):
            if not target.eligible:
                continue
            for index, value in enumerate(latent):
                residual_sums[row_index][index] += value - means[row_index][index]
    max_abs_centered_mean = max(
        abs(value / counts[row_index])
        for row_index, row in enumerate(residual_sums)
        for value in row
    )
    if max_abs_centered_mean > CENTERED_MEAN_ABSOLUTE_TOLERANCE:
        raise E160OffsetProbeError(
            "TRAIN centered latent mean exceeds the locked tolerance"
        )
    core = {
        "semantics_id": CENTERING_SEMANTICS_ID,
        "fit_partition": "train",
        "eligible_rows_by_output_row": counts,
        "means": means,
        "max_abs_centered_train_mean": max_abs_centered_mean,
        "centered_mean_absolute_tolerance": CENTERED_MEAN_ABSOLUTE_TOLERANCE,
    }
    return {**core, "centering_identity": identity(core)}


def validate_centering(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "semantics_id",
        "fit_partition",
        "eligible_rows_by_output_row",
        "means",
        "max_abs_centered_train_mean",
        "centered_mean_absolute_tolerance",
        "centering_identity",
    }:
        raise E160OffsetProbeError("centering fields are not exact")
    exact(value["semantics_id"], CENTERING_SEMANTICS_ID, "centering semantics")
    exact(value["fit_partition"], "train", "centering partition")
    counts = value["eligible_rows_by_output_row"]
    means = value["means"]
    if (
        type(counts) is not list
        or len(counts) != OUTPUT_ROWS
        or any(type(count) is not int or count <= 0 for count in counts)
    ):
        raise E160OffsetProbeError("centering output-row counts are invalid")
    if type(means) is not list or len(means) != OUTPUT_ROWS:
        raise E160OffsetProbeError("centering must contain three mean vectors")
    for vector in means:
        if (
            type(vector) is not list
            or len(vector) != LATENT_DIM
            or any(
                type(number) not in (int, float) or not math.isfinite(number)
                for number in vector
            )
        ):
            raise E160OffsetProbeError("centering vector is invalid")
    maximum = value["max_abs_centered_train_mean"]
    tolerance = value["centered_mean_absolute_tolerance"]
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum < 0:
        raise E160OffsetProbeError("centered TRAIN mean diagnostic is invalid")
    exact(
        tolerance,
        CENTERED_MEAN_ABSOLUTE_TOLERANCE,
        "centered TRAIN mean tolerance",
    )
    if maximum > tolerance:
        raise E160OffsetProbeError("centered TRAIN mean exceeds the locked tolerance")
    core = {name: value[name] for name in value if name != "centering_identity"}
    exact(value["centering_identity"], identity(core), "centering identity")
    return value


def centered_latent(record, row_index: int, centering: dict) -> tuple[float, ...]:
    validate_centering(centering)
    if type(row_index) is not int or not 0 <= row_index < OUTPUT_ROWS:
        raise E160OffsetProbeError("output row index must be in 0..2")
    latent = _latent_values(record.latent)
    mean = centering["means"][row_index]
    return tuple(
        value - float(center) for value, center in zip(latent, mean, strict=True)
    )


def centered_latent_summary(
    records: tuple,
    centering: dict,
) -> dict[str, object]:
    """Summarize centered latent values without using structural-wait labels."""
    if not records:
        raise E160OffsetProbeError("centered latent summary requires records")
    partitions = {record.partition for record in records}
    if len(partitions) != 1:
        raise E160OffsetProbeError(
            "centered latent summary requires exactly one retained partition"
        )
    validate_centering(centering)

    def empty_stats():
        return {
            "eligible_rows": 0,
            "scalar_values": 0,
            "minimum": math.inf,
            "maximum": -math.inf,
            "sum": 0.0,
            "square_sum": 0.0,
        }

    overall = empty_stats()
    per_row = [empty_stats() for _ in range(OUTPUT_ROWS)]

    def add(stats: dict, values: tuple[float, ...]) -> None:
        stats["eligible_rows"] += 1
        stats["scalar_values"] += len(values)
        stats["minimum"] = min(stats["minimum"], min(values))
        stats["maximum"] = max(stats["maximum"], max(values))
        stats["sum"] += sum(values)
        stats["square_sum"] += sum(value * value for value in values)

    for record in records:
        for row_index, target in enumerate(record.targets):
            if not target.eligible:
                continue
            values = centered_latent(record, row_index, centering)
            add(overall, values)
            add(per_row[row_index], values)

    def finish(stats: dict) -> dict[str, object]:
        count = stats["scalar_values"]
        if stats["eligible_rows"] <= 0 or count <= 0:
            raise E160OffsetProbeError(
                "centered latent summary requires eligible rows"
            )
        mean = stats["sum"] / count
        rms = math.sqrt(stats["square_sum"] / count)
        result = {
            "eligible_rows": stats["eligible_rows"],
            "scalar_values": count,
            "minimum": stats["minimum"],
            "maximum": stats["maximum"],
            "mean": mean,
            "rms": rms,
            "max_abs": max(abs(stats["minimum"]), abs(stats["maximum"])),
        }
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for name, value in result.items()
            if name not in ("eligible_rows", "scalar_values")
        ):
            raise E160OffsetProbeError(
                "centered latent summary contains a non-finite value"
            )
        return result

    partition = next(iter(partitions))
    return {
        "partition": partition.value,
        "overall": finish(overall),
        "per_output_row": [
            {"output_row": row_index, **finish(stats)}
            for row_index, stats in enumerate(per_row)
        ],
    }


def latent_fingerprint(records: tuple) -> str:
    if not records:
        raise E160OffsetProbeError("latent fingerprint requires records")
    digest = hashlib.sha256()
    digest.update(LATENT_FINGERPRINT_SEMANTICS_ID.encode("ascii"))
    for record in records:
        digest.update(record.partition.value.encode("ascii"))
        digest.update(record.source_class.encode("utf-8"))
        digest.update(str(record.game_seed).encode("ascii"))
        digest.update(str(record.round_index).encode("ascii"))
        digest.update(record.anchor_identity.encode("ascii"))
        digest.update(str(record.depth).encode("ascii"))
        digest.update("|".join(record.opponent_winds).encode("ascii"))
        for value in _latent_values(record.latent):
            digest.update(struct.pack("<f", value))
    return digest.hexdigest()


def latent_reference(
    train: tuple,
    validation: tuple,
    frozen_state_digest: str,
) -> dict[str, object]:
    exact(frozen_state_digest, FROZEN_E160_STATE_DIGEST, "frozen E160 state digest")
    centering = centering_receipt(train)
    value = {
        "frozen_state_digest": frozen_state_digest,
        "latent_fingerprint_semantics_id": LATENT_FINGERPRINT_SEMANTICS_ID,
        "latent_fingerprint": latent_fingerprint(train + validation),
        "train_coverage": coverage_value(train),
        "validation_coverage": coverage_value(validation),
        "centering": centering,
        "centered_latent_summary": {
            "train": centered_latent_summary(train, centering),
            "validation": centered_latent_summary(validation, centering),
        },
    }
    return value


def load_latent_records(
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
):
    """Strict-read retained evidence and reproduce exact #172 latent extraction."""
    try:
        evidence = load_retained(corpus_root, phase157_root, phase167_root)
        records, snapshot = frozen_latent_records(evidence)
        train = partition_records(records, DatasetPartition.TRAIN)
        validation = partition_records(records, DatasetPartition.VALIDATION)
    except E160OffsetProbeError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise E160OffsetProbeError(
            f"retained E160 latent readback failed: {error}"
        ) from error
    exact(snapshot.digest, FROZEN_E160_STATE_DIGEST, "frozen E160 state digest")
    return train, validation, evidence, snapshot


def retained_receipt(evidence) -> dict[str, object]:
    value = retained_readback_value(evidence)
    return {
        "phase150_execution_lock_identity": value["phase150_execution_lock_identity"],
        "population_identity": value["population_identity"],
        "raw_corpus_identity": value["raw_corpus_identity"],
        "dataset_identity": value["dataset_identity"],
        "phase167_execution_lock_identity": value["phase167_execution_lock_identity"],
        "phase167_result_identity": value["phase167_result_identity"],
        "e160_weights_sha256": value["e160_weights_sha256"],
        "train_seeds": value["train_seeds"],
        "validation_seeds": value["validation_seeds"],
        "formal_test": value["formal_test"],
    }


__all__ = [
    "centered_latent",
    "centered_latent_summary",
    "centering_receipt",
    "coverage_value",
    "latent_fingerprint",
    "latent_reference",
    "load_latent_records",
    "retained_receipt",
    "validate_centering",
]
