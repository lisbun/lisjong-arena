"""Locked Phase 8 validation measurements and diagnostics."""

from collections import defaultdict
from dataclasses import dataclass
from math import isclose
from statistics import median

from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountMetrics,
    ExpectedCountPrediction,
    evaluate_expected_count_predictions,
    measure_expected_count_rows,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition

from .protocol import (
    DEPTH_BUCKETS,
    Candidate,
    CandidateSummary,
    depth_bucket,
    physical_validity_passes,
)
from .rollout import RolloutResult, flatten_sequences

_COMPATIBILITY_ABS_TOLERANCE = 1e-12


def remap_predictions_by_reference(
    target_references: tuple,
    predictions: tuple[ExpectedCountPrediction, ...],
) -> tuple[ExpectedCountPrediction, ...]:
    """Return predictions in target order after exact one-to-one identity checks."""
    if not target_references:
        raise ValueError("prediction remap requires target references")
    target_by_identity = {}
    for reference in target_references:
        identity = reference.identity
        if identity in target_by_identity:
            raise ValueError("target reference identities must be unique")
        target_by_identity[identity] = reference
    predictions_by_identity = {}
    for prediction in predictions:
        identity = prediction.example.identity
        if identity in predictions_by_identity:
            raise ValueError("prediction identities must be unique")
        predictions_by_identity[identity] = prediction
    if set(predictions_by_identity) != set(target_by_identity):
        raise ValueError("prediction and target reference identities differ")
    aligned = tuple(
        predictions_by_identity[reference.identity] for reference in target_references
    )
    if any(
        prediction.example != reference
        for prediction, reference in zip(aligned, target_references, strict=True)
    ):
        raise ValueError("prediction reference differs from target reference")
    return aligned


@dataclass(frozen=True, slots=True)
class CanonicalValidation:
    examples: tuple
    snapshot_predictions: tuple[ExpectedCountPrediction, ...]
    snapshot_metrics: ExpectedCountMetrics

    def __post_init__(self) -> None:
        if not self.examples or any(
            value.example.partition is not DatasetPartition.VALIDATION
            for value in self.examples
        ):
            raise ValueError("canonical validation must contain only VALIDATION")
        references = tuple(value.example for value in self.examples)
        aligned = remap_predictions_by_reference(references, self.snapshot_predictions)
        if aligned != self.snapshot_predictions:
            raise ValueError("snapshot predictions are not in canonical order")
        if self.snapshot_metrics.sample_count != len(self.examples):
            raise ValueError("snapshot metrics and canonical examples differ")


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: Candidate
    metrics: ExpectedCountMetrics
    snapshot_metrics: ExpectedCountMetrics
    delta_mae: float
    per_game: tuple[dict[str, object], ...]
    game_macro_mean_delta_mae: float
    median_per_game_delta_mae: float
    positive_game_count: int
    depth_diagnostics: tuple[dict[str, object], ...]
    physical_consistency: dict[str, object]

    @property
    def summary(self) -> CandidateSummary:
        return CandidateSummary(
            self.candidate,
            self.metrics.per_tile_mae,
            self.positive_game_count,
            len(self.per_game),
            self.physical_consistency["blocking_gate_passed"],
        )


def metrics_value(metrics: ExpectedCountMetrics) -> dict[str, object]:
    return {
        "samples": metrics.sample_count,
        "per_tile_mae": metrics.per_tile_mae,
        "per_hand_l1": metrics.per_hand_l1,
        "concealed_size_inconsistency_mean": (
            metrics.concealed_size_inconsistency_mean
        ),
        "concealed_size_inconsistency_max": (metrics.concealed_size_inconsistency_max),
        "physical_conservation_violation_sample_rate": (
            metrics.conservation_violation_sample_rate
        ),
        "conservation_total_excess": metrics.conservation_total_excess,
        "conservation_mean_excess_per_sample": (
            metrics.conservation_mean_excess_per_sample
        ),
    }


def verify_snapshot_validation_compatibility(
    dataset_identity: str,
    canonical_examples: tuple,
    snapshot_predictions: tuple[ExpectedCountPrediction, ...],
    frozen_validation_metrics: object,
) -> CanonicalValidation:
    """Verify the frozen artifact in canonical dataset VALIDATION order."""
    if not canonical_examples or any(
        value.example.partition is not DatasetPartition.VALIDATION
        for value in canonical_examples
    ):
        raise ValueError("snapshot compatibility requires canonical VALIDATION")
    references = tuple(value.example for value in canonical_examples)
    samples = tuple(value.sample for value in canonical_examples)
    aligned = remap_predictions_by_reference(references, snapshot_predictions)
    report = evaluate_expected_count_predictions(
        dataset_identity, references, samples, aligned
    )
    snapshot_metrics = report.partition_metrics[0].metrics
    actual = metrics_value(snapshot_metrics)
    if type(frozen_validation_metrics) is not dict:
        raise RuntimeError("frozen snapshot VALIDATION compatibility fields drift")
    for name, actual_value in actual.items():
        if name not in frozen_validation_metrics:
            raise RuntimeError(f"frozen snapshot VALIDATION compatibility lacks {name}")
        expected_value = frozen_validation_metrics[name]
        if type(actual_value) is int:
            compatible = type(expected_value) is int and actual_value == expected_value
        else:
            compatible = type(expected_value) in (int, float) and isclose(
                actual_value,
                expected_value,
                rel_tol=0,
                abs_tol=_COMPATIBILITY_ABS_TOLERANCE,
            )
        if not compatible:
            raise RuntimeError(
                f"frozen snapshot VALIDATION compatibility drift for {name}"
            )
    return CanonicalValidation(canonical_examples, aligned, snapshot_metrics)


def evaluate_candidate(
    candidate: Candidate,
    sequences: tuple,
    rollout: RolloutResult,
    canonical_validation: CanonicalValidation,
    *,
    dataset_identity: str,
) -> CandidateEvaluation:
    sequence_examples = flatten_sequences(sequences)
    sequence_references = tuple(value.example for value in sequence_examples)
    if len(rollout.steps) != len(sequence_examples):
        raise ValueError("rollout and validation examples differ")
    if (
        tuple(value.prediction.example for value in rollout.steps)
        != sequence_references
    ):
        raise ValueError("rollout steps and sequence references differ")
    canonical_examples = canonical_validation.examples
    canonical_references = tuple(value.example for value in canonical_examples)
    canonical_samples = tuple(value.sample for value in canonical_examples)
    candidate_predictions = remap_predictions_by_reference(
        canonical_references, rollout.predictions
    )
    candidate_report = evaluate_expected_count_predictions(
        dataset_identity,
        canonical_references,
        canonical_samples,
        candidate_predictions,
    )
    snapshot_report = evaluate_expected_count_predictions(
        dataset_identity,
        canonical_references,
        canonical_samples,
        canonical_validation.snapshot_predictions,
    )
    metrics = candidate_report.partition_metrics[0].metrics
    snapshot_metrics = snapshot_report.partition_metrics[0].metrics
    if snapshot_metrics != canonical_validation.snapshot_metrics:
        raise RuntimeError(
            "canonical snapshot VALIDATION metrics changed after preflight"
        )
    candidate_by_game = {
        value.game: value.metrics for value in candidate_report.game_metrics
    }
    snapshot_by_game = {
        value.game: value.metrics for value in snapshot_report.game_metrics
    }
    games = tuple(dict.fromkeys(value.example.game for value in canonical_examples))
    per_game = tuple(
        {
            "source_class": game.source_class,
            "game_seed": game.game_seed,
            "sample_count": candidate_by_game[game].sample_count,
            "snapshot_mae": snapshot_by_game[game].per_tile_mae,
            "candidate_mae": candidate_by_game[game].per_tile_mae,
            "delta_mae": (
                snapshot_by_game[game].per_tile_mae
                - candidate_by_game[game].per_tile_mae
            ),
        }
        for game in games
    )
    deltas = tuple(value["delta_mae"] for value in per_game)

    snapshot_sequence_predictions = remap_predictions_by_reference(
        sequence_references, canonical_validation.snapshot_predictions
    )
    depth_rows = defaultdict(lambda: {"candidate": [], "snapshot": []})
    for trace, example, snapshot in zip(
        rollout.steps,
        sequence_examples,
        snapshot_sequence_predictions,
        strict=True,
    ):
        bucket = depth_bucket(trace.depth)
        depth_rows[bucket]["candidate"].extend(
            measure_expected_count_rows(
                example.example, example.sample, trace.prediction
            )
        )
        depth_rows[bucket]["snapshot"].extend(
            measure_expected_count_rows(example.example, example.sample, snapshot)
        )
    depth_diagnostics = []
    for name in DEPTH_BUCKETS:
        candidate_rows = depth_rows[name]["candidate"]
        snapshot_rows = depth_rows[name]["snapshot"]
        if not candidate_rows:
            depth_diagnostics.append(
                {
                    "bucket": name,
                    "sample_count": 0,
                    "candidate_mae": None,
                    "snapshot_mae": None,
                    "delta_mae": None,
                }
            )
            continue
        candidate_absolute = sum(value.absolute_error_sum for value in candidate_rows)
        snapshot_absolute = sum(value.absolute_error_sum for value in snapshot_rows)
        cells = sum(value.cell_count for value in candidate_rows)
        candidate_mae = candidate_absolute / cells
        snapshot_mae = snapshot_absolute / cells
        depth_diagnostics.append(
            {
                "bucket": name,
                "sample_count": len(candidate_rows) // 3,
                "candidate_mae": candidate_mae,
                "snapshot_mae": snapshot_mae,
                "delta_mae": snapshot_mae - candidate_mae,
            }
        )
    physical_ok = physical_validity_passes(
        constraint_non_convergence_count=0,
        maximum_residual=rollout.maximum_residual,
        concealed_size_inconsistency_max=metrics.concealed_size_inconsistency_max,
        conservation_violation_sample_rate=metrics.conservation_violation_sample_rate,
    )
    physical = {
        "constraint_non_convergence_count": 0,
        "maximum_row_column_residual": rollout.maximum_residual,
        "concealed_size_inconsistency_max": metrics.concealed_size_inconsistency_max,
        "physical_conservation_violation_sample_rate": (
            metrics.conservation_violation_sample_rate
        ),
        "conservation_total_excess": metrics.conservation_total_excess,
        "conservation_mean_excess_per_sample": (
            metrics.conservation_mean_excess_per_sample
        ),
        "blocking_gate_passed": physical_ok,
    }
    return CandidateEvaluation(
        candidate,
        metrics,
        snapshot_metrics,
        snapshot_metrics.per_tile_mae - metrics.per_tile_mae,
        per_game,
        sum(deltas) / len(deltas),
        median(deltas),
        sum(value > 0 for value in deltas),
        tuple(depth_diagnostics),
        physical,
    )


__all__ = [
    "CanonicalValidation",
    "CandidateEvaluation",
    "evaluate_candidate",
    "metrics_value",
    "remap_predictions_by_reference",
    "verify_snapshot_validation_compatibility",
]
