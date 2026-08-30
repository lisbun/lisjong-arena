"""Locked Phase 8 validation measurements and diagnostics."""

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountMetrics,
    ExpectedCountPrediction,
    evaluate_expected_count_predictions,
    measure_expected_count_rows,
)

from .protocol import (
    DEPTH_BUCKETS,
    SNAPSHOT_VALIDATION_MAE,
    Candidate,
    CandidateSummary,
    depth_bucket,
    physical_validity_passes,
)
from .rollout import RolloutResult, flatten_sequences


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


def evaluate_candidate(
    candidate: Candidate,
    sequences: tuple,
    rollout: RolloutResult,
    snapshot_predictions: tuple[ExpectedCountPrediction, ...],
    *,
    dataset_identity: str,
) -> CandidateEvaluation:
    examples = flatten_sequences(sequences)
    references = tuple(value.example for value in examples)
    samples = tuple(value.sample for value in examples)
    if len(rollout.steps) != len(examples):
        raise ValueError("rollout and validation examples differ")
    if len(snapshot_predictions) != len(examples):
        raise ValueError("snapshot and validation examples differ")
    candidate_report = evaluate_expected_count_predictions(
        dataset_identity, references, samples, rollout.predictions
    )
    snapshot_report = evaluate_expected_count_predictions(
        dataset_identity, references, samples, snapshot_predictions
    )
    metrics = candidate_report.partition_metrics[0].metrics
    snapshot_metrics = snapshot_report.partition_metrics[0].metrics
    if abs(snapshot_metrics.per_tile_mae - SNAPSHOT_VALIDATION_MAE) > 1e-12:
        raise RuntimeError("frozen snapshot VALIDATION MAE compatibility drift")
    candidate_by_game = {
        value.game: value.metrics for value in candidate_report.game_metrics
    }
    snapshot_by_game = {
        value.game: value.metrics for value in snapshot_report.game_metrics
    }
    games = tuple(dict.fromkeys(value.example.game for value in examples))
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

    depth_rows = defaultdict(lambda: {"candidate": [], "snapshot": []})
    for trace, example, snapshot in zip(
        rollout.steps, examples, snapshot_predictions, strict=True
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
    "CandidateEvaluation",
    "evaluate_candidate",
    "metrics_value",
]
