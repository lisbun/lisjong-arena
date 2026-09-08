"""Validation evidence and the locked paired Issue #190 decision rule."""

from math import isfinite, sqrt
from statistics import mean, stdev

from lisjong_arena.learned_policy_offline_q.protocol import BATCH_SIZE
from lisjong_arena.learned_policy_stage2.network import (
    masked_argmax,
    masked_cross_entropy,
)

from .errors import DataSufficiencyError, DataSufficiencyEvidenceBlocked
from .protocol import (
    NORMAL_95_Z,
    PRIMARY_LARGER_SCALE,
    PRIMARY_SMALLER_SCALE,
    VALIDATION_SEEDS,
    DataSufficiencyOutcome,
)

_ROW_FIELDS = {
    "source_row_index",
    "seed",
    "round_ordinal",
    "actor_seat",
    "decision_ordinal",
    "masked_ce",
    "teacher_exact_match",
}


def _finite_number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not isfinite(float(value)):
        raise DataSufficiencyError(f"{name} must be a finite number")
    return float(value)


def evaluate_validation_rows(model, tensors) -> list[dict[str, object]]:
    """Retain one masked-CE output and agreement bit per VALIDATION decision."""
    import torch

    if tensors.row_count != len(tensors.source_rows):
        raise DataSufficiencyError("VALIDATION tensor metadata is not row-aligned")
    model.eval()
    losses: list[float] = []
    predictions: list[int] = []
    with torch.no_grad():
        for start in range(0, tensors.row_count, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, tensors.row_count)
            logits = model(tensors.features[start:stop])
            batch_losses = masked_cross_entropy(
                logits,
                tensors.legal_mask[start:stop],
                tensors.behavior_action_index[start:stop],
            )
            losses.extend(float(value) for value in batch_losses.tolist())
            predictions.extend(
                int(value)
                for value in masked_argmax(
                    logits, tensors.legal_mask[start:stop]
                ).tolist()
            )
    if len(losses) != tensors.row_count or any(not isfinite(value) for value in losses):
        raise DataSufficiencyError("VALIDATION produced missing or non-finite CE")
    return [
        {
            "source_row_index": row.source_row_index,
            "seed": row.seed,
            "round_ordinal": row.round_ordinal,
            "actor_seat": row.actor_seat,
            "decision_ordinal": row.decision_ordinal,
            "masked_ce": loss,
            "teacher_exact_match": prediction == row.behavior_action_index,
        }
        for row, loss, prediction in zip(
            tensors.source_rows, losses, predictions, strict=True
        )
    ]


def summarize_validation_rows(rows: object) -> dict[str, object]:
    """Derive per-hanchan and decision-weighted aggregate metrics from raw rows."""
    if type(rows) is not list or not rows:
        raise DataSufficiencyError("validation_rows must be a non-empty list")
    grouped: dict[int, list[dict[str, object]]] = {
        seed: [] for seed in VALIDATION_SEEDS
    }
    identities: set[int] = set()
    total_ce = 0.0
    total_matches = 0
    for row in rows:
        if type(row) is not dict or set(row) != _ROW_FIELDS:
            raise DataSufficiencyError("validation row fields are invalid")
        index = row["source_row_index"]
        seed = row["seed"]
        if type(index) is not int or index < 0 or index in identities:
            raise DataSufficiencyError("validation source row identity is malformed")
        if type(seed) is not int or seed not in grouped:
            raise DataSufficiencyError("validation row has an unexpected seed")
        for name in ("round_ordinal", "actor_seat", "decision_ordinal"):
            if type(row[name]) is not int:
                raise DataSufficiencyError(f"validation row {name} is malformed")
        if type(row["teacher_exact_match"]) is not bool:
            raise DataSufficiencyError("teacher_exact_match must be a boolean")
        loss = _finite_number(row["masked_ce"], "validation masked_ce")
        if loss < 0.0:
            raise DataSufficiencyError("validation masked_ce must not be negative")
        identities.add(index)
        grouped[seed].append(row)
        total_ce += loss
        total_matches += int(row["teacher_exact_match"])

    per_hanchan: list[dict[str, object]] = []
    for seed in VALIDATION_SEEDS:
        block = grouped[seed]
        if not block:
            raise DataSufficiencyError(f"validation hanchan {seed} is missing")
        ce_sum = sum(float(row["masked_ce"]) for row in block)
        matches = sum(bool(row["teacher_exact_match"]) for row in block)
        count = len(block)
        per_hanchan.append(
            {
                "seed": seed,
                "eligible_decision_count": count,
                "masked_ce_sum": ce_sum,
                "masked_ce": ce_sum / count,
                "teacher_exact_agreement_count": matches,
                "teacher_exact_agreement": matches / count,
            }
        )
    count = len(rows)
    return {
        "eligible_decision_count": count,
        "masked_ce_sum": total_ce,
        "aggregate_masked_ce": total_ce / count,
        "teacher_exact_agreement_count": total_matches,
        "teacher_exact_agreement": total_matches / count,
        "per_hanchan": per_hanchan,
    }


def paired_comparison(scale_results: dict[str, dict[str, object]]) -> dict[str, object]:
    """Compute CE(S20)-CE(S15) over the six fixed hanchan blocks."""
    try:
        smaller = scale_results[PRIMARY_SMALLER_SCALE]["validation"]["per_hanchan"]
        larger = scale_results[PRIMARY_LARGER_SCALE]["validation"]["per_hanchan"]
    except (KeyError, TypeError) as error:
        raise DataSufficiencyError(
            "primary scale validation evidence is missing"
        ) from error
    smaller_by_seed = {entry["seed"]: entry for entry in smaller}
    larger_by_seed = {entry["seed"]: entry for entry in larger}
    if (
        tuple(smaller_by_seed) != VALIDATION_SEEDS
        or tuple(larger_by_seed) != VALIDATION_SEEDS
    ):
        raise DataSufficiencyError("primary validation hanchan pairing is not exact")
    pairs = []
    for seed in VALIDATION_SEEDS:
        left = _finite_number(smaller_by_seed[seed]["masked_ce"], "S15 masked CE")
        right = _finite_number(larger_by_seed[seed]["masked_ce"], "S20 masked CE")
        pairs.append(
            {
                "seed": seed,
                "s15_masked_ce": left,
                "s20_masked_ce": right,
                "difference_s20_minus_s15": right - left,
            }
        )
    differences = [entry["difference_s20_minus_s15"] for entry in pairs]
    paired_mean = mean(differences)
    sample_sd = stdev(differences)
    standard_error = sample_sd / sqrt(len(differences))
    margin = NORMAL_95_Z * standard_error
    return {
        "difference": "CE(S20)-CE(S15)",
        "paired_unit": "whole_hanchan",
        "hanchan_count": len(pairs),
        "pairs": pairs,
        "mean_difference": paired_mean,
        "sample_standard_deviation": sample_sd,
        "standard_error": standard_error,
        "normal_approximation_z": NORMAL_95_Z,
        "interval_lower": paired_mean - margin,
        "interval_upper": paired_mean + margin,
    }


def classify_comparison(comparison: dict[str, object]) -> DataSufficiencyOutcome:
    """Apply only the pre-registered interval-upper decision rule."""
    lower = _finite_number(comparison.get("interval_lower"), "interval_lower")
    upper = _finite_number(comparison.get("interval_upper"), "interval_upper")
    if lower > upper:
        raise DataSufficiencyError("paired interval bounds are reversed")
    if comparison.get("hanchan_count") != len(VALIDATION_SEEDS):
        raise DataSufficiencyError("paired interval does not cover six hanchan")
    if upper < 0.0:
        return DataSufficiencyOutcome.CLEAR_DATA_SCALE_SIGNAL
    return DataSufficiencyOutcome.NO_CLEAR_DATA_SCALE_SIGNAL


def outcome_for_failure(error: BaseException) -> DataSufficiencyOutcome:
    """Keep source unavailability distinct from malformed protocol evidence."""
    if isinstance(error, DataSufficiencyEvidenceBlocked):
        return DataSufficiencyOutcome.EVIDENCE_BLOCKED
    return DataSufficiencyOutcome.STOP_INVALID


__all__ = [
    "classify_comparison",
    "evaluate_validation_rows",
    "outcome_for_failure",
    "paired_comparison",
    "summarize_validation_rows",
]
