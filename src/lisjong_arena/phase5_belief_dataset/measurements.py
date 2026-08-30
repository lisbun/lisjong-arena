"""Reproducible Phase 5 baseline and target-coverage measurements."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import isfinite

from lisjong.belief import SCALE, derive_remaining_tile_inventory, wind_index

from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase5_belief_dataset.baseline import BaselinePrediction
from lisjong_arena.phase5_belief_dataset.model import (
    BeliefDataset,
    DatasetPartition,
    GameIdentity,
    TargetAvailabilitySummary,
    TurnExampleReference,
)

CONSERVATION_VIOLATION_TOLERANCE = 1e-3
"""Ignore fixed-point representation noise only when classifying violations.

The pinned estimator quantizes at ``SCALE``.  Summing three opponent allocations
can therefore produce a tiny apparent excess even when semantic conservation is
respected.  Raw excess remains included in total and mean measurements.
"""


def _nonnegative_finite(value: float, name: str) -> None:
    if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class SampleMeasurementRecord:
    """Per-anchor record retaining source, game, and partition identity."""

    example: TurnExampleReference
    expected_count_absolute_error_sum: float
    expected_count_cell_count: int
    expected_count_hand_count: int
    concealed_size_absolute_error_sum: float
    concealed_size_max_error: float
    conservation_violated: bool
    conservation_total_excess: float
    red_five_squared_error_sum: float
    red_five_absolute_error_sum: float
    red_five_cell_count: int
    wait_available: int
    wait_unavailable: int
    wait_all_zero: int
    wait_non_zero: int
    wait_unavailable_reasons: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.example, TurnExampleReference):
            raise TypeError("example must be a TurnExampleReference")
        for name in (
            "expected_count_absolute_error_sum",
            "concealed_size_absolute_error_sum",
            "concealed_size_max_error",
            "conservation_total_excess",
            "red_five_squared_error_sum",
            "red_five_absolute_error_sum",
        ):
            _nonnegative_finite(getattr(self, name), name)
        if self.expected_count_cell_count != 102:
            raise ValueError("one sample must contain 3 x 34 expected-count cells")
        if self.expected_count_hand_count != 3:
            raise ValueError("one sample must contain exactly three opponent hands")
        if self.red_five_cell_count != 9:
            raise ValueError("one sample must contain 3 x 3 red-five cells")
        if self.wait_available + self.wait_unavailable != 3:
            raise ValueError("one sample must retain three wait target rows")
        if self.wait_available != self.wait_all_zero + self.wait_non_zero:
            raise ValueError("available waits must equal all-zero plus non-zero rows")
        if self.wait_unavailable != sum(
            count for _, count in self.wait_unavailable_reasons
        ):
            raise ValueError("unavailable reasons must cover unavailable wait rows")


@dataclass(frozen=True, slots=True)
class ExpectedCountMetrics:
    sample_count: int
    opponent_hand_count: int
    cell_count: int
    absolute_error_sum: float
    concealed_size_absolute_error_sum: float
    concealed_size_max_error: float
    conservation_violation_samples: int
    conservation_total_excess: float

    @property
    def per_tile_mae(self) -> float:
        return self.absolute_error_sum / self.cell_count

    @property
    def per_hand_l1(self) -> float:
        return self.absolute_error_sum / self.opponent_hand_count

    @property
    def concealed_size_inconsistency_mean(self) -> float:
        return self.concealed_size_absolute_error_sum / self.opponent_hand_count

    @property
    def concealed_size_inconsistency_max(self) -> float:
        return self.concealed_size_max_error

    @property
    def conservation_violation_sample_rate(self) -> float:
        return self.conservation_violation_samples / self.sample_count

    @property
    def conservation_mean_excess_per_sample(self) -> float:
        return self.conservation_total_excess / self.sample_count


@dataclass(frozen=True, slots=True)
class RedFiveMetrics:
    cell_count: int
    squared_error_sum: float
    absolute_error_sum: float

    @property
    def brier_score(self) -> float:
        return self.squared_error_sum / self.cell_count

    @property
    def mae(self) -> float:
        return self.absolute_error_sum / self.cell_count


@dataclass(frozen=True, slots=True)
class BeliefQualityMetrics:
    expected_count: ExpectedCountMetrics
    red_five: RedFiveMetrics
    structural_wait_coverage: TargetAvailabilitySummary


@dataclass(frozen=True, slots=True)
class PartitionBaselineMetrics:
    partition: DatasetPartition
    metrics: BeliefQualityMetrics


@dataclass(frozen=True, slots=True)
class GameBaselineMetrics:
    game: GameIdentity
    partition: DatasetPartition
    metrics: BeliefQualityMetrics


@dataclass(frozen=True, slots=True)
class BaselineReport:
    dataset_identity: str
    records: tuple[SampleMeasurementRecord, ...]
    partition_metrics: tuple[PartitionBaselineMetrics, ...]
    game_metrics: tuple[GameBaselineMetrics, ...]


def _measure_sample(
    example: TurnExampleReference,
    sample: TrainingSample,
    prediction: BaselinePrediction,
) -> SampleMeasurementRecord:
    if prediction.example != example:
        raise ValueError("prediction and dataset example identity differ")
    if sample.anchor.source.game_seed != example.game.game_seed:
        raise ValueError("sample and dataset game identity differ")
    absolute_error = 0.0
    size_error_sum = 0.0
    size_error_max = 0.0
    red_squared_error = 0.0
    red_absolute_error = 0.0
    summed_by_tile = [0.0] * 34
    for expected_row in sample.labels.expected_counts:
        wind_number = wind_index(expected_row.identity.wind)
        hand = prediction.belief.hands[wind_number]
        predicted_values = tuple(raw / SCALE for raw in hand.expected_count_raw)
        for tile_index, (predicted, realized) in enumerate(
            zip(predicted_values, expected_row.counts, strict=True)
        ):
            absolute_error += abs(predicted - realized)
            summed_by_tile[tile_index] += predicted
        expected_slots = prediction.concealed_slot_counts_by_wind[wind_number]
        size_error = abs(sum(predicted_values) - expected_slots)
        size_error_sum += size_error
        size_error_max = max(size_error_max, size_error)
        for predicted_raw, realized in zip(
            hand.red_five_probability_raw,
            expected_row.red_five_present,
            strict=True,
        ):
            predicted_red = predicted_raw / SCALE
            error = predicted_red - int(realized)
            red_squared_error += error * error
            red_absolute_error += abs(error)

    policy_input = build_policy_input(sample.anchor.observation)
    remaining = derive_remaining_tile_inventory(policy_input).remaining_tile_counts
    conservation_excess = 0.0
    conservation_violated = False
    for predicted, available in zip(summed_by_tile, remaining, strict=True):
        excess = max(0.0, predicted - available)
        conservation_excess += excess
        if excess > CONSERVATION_VIOLATION_TOLERANCE:
            conservation_violated = True

    wait_available = 0
    wait_all_zero = 0
    wait_non_zero = 0
    reasons = Counter()
    for row in sample.labels.structural_waits:
        if row.mask is None:
            reasons[row.unavailable_reason.value] += 1
        else:
            wait_available += 1
            if any(row.mask):
                wait_non_zero += 1
            else:
                wait_all_zero += 1
    return SampleMeasurementRecord(
        example=example,
        expected_count_absolute_error_sum=absolute_error,
        expected_count_cell_count=102,
        expected_count_hand_count=3,
        concealed_size_absolute_error_sum=size_error_sum,
        concealed_size_max_error=size_error_max,
        conservation_violated=conservation_violated,
        conservation_total_excess=conservation_excess,
        red_five_squared_error_sum=red_squared_error,
        red_five_absolute_error_sum=red_absolute_error,
        red_five_cell_count=9,
        wait_available=wait_available,
        wait_unavailable=sum(reasons.values()),
        wait_all_zero=wait_all_zero,
        wait_non_zero=wait_non_zero,
        wait_unavailable_reasons=tuple(sorted(reasons.items())),
    )


def _aggregate(records: tuple[SampleMeasurementRecord, ...]) -> BeliefQualityMetrics:
    if not records:
        raise ValueError("metric aggregation requires at least one sample record")
    reasons = Counter()
    for record in records:
        reasons.update(dict(record.wait_unavailable_reasons))
    expected = ExpectedCountMetrics(
        sample_count=len(records),
        opponent_hand_count=sum(value.expected_count_hand_count for value in records),
        cell_count=sum(value.expected_count_cell_count for value in records),
        absolute_error_sum=sum(
            value.expected_count_absolute_error_sum for value in records
        ),
        concealed_size_absolute_error_sum=sum(
            value.concealed_size_absolute_error_sum for value in records
        ),
        concealed_size_max_error=max(
            value.concealed_size_max_error for value in records
        ),
        conservation_violation_samples=sum(
            value.conservation_violated for value in records
        ),
        conservation_total_excess=sum(
            value.conservation_total_excess for value in records
        ),
    )
    red = RedFiveMetrics(
        cell_count=sum(value.red_five_cell_count for value in records),
        squared_error_sum=sum(value.red_five_squared_error_sum for value in records),
        absolute_error_sum=sum(value.red_five_absolute_error_sum for value in records),
    )
    coverage = TargetAvailabilitySummary(
        target_rows=len(records) * 3,
        structural_wait_available=sum(value.wait_available for value in records),
        structural_wait_unavailable=sum(value.wait_unavailable for value in records),
        structural_wait_all_zero=sum(value.wait_all_zero for value in records),
        structural_wait_non_zero=sum(value.wait_non_zero for value in records),
        unavailable_reasons=tuple(sorted(reasons.items())),
    )
    return BeliefQualityMetrics(expected, red, coverage)


def evaluate_baseline_predictions(
    dataset: BeliefDataset,
    samples: tuple[TrainingSample, ...],
    predictions: tuple[BaselinePrediction, ...],
) -> BaselineReport:
    if len(dataset.examples) != len(samples) or len(samples) != len(predictions):
        raise ValueError("dataset, samples, and predictions must have equal length")
    records = tuple(
        _measure_sample(example, sample, prediction)
        for example, sample, prediction in zip(
            dataset.examples, samples, predictions, strict=True
        )
    )
    by_partition: dict[DatasetPartition, list[SampleMeasurementRecord]] = defaultdict(
        list
    )
    by_game: dict[GameIdentity, list[SampleMeasurementRecord]] = defaultdict(list)
    partition_by_game = {}
    for record in records:
        by_partition[record.example.partition].append(record)
        by_game[record.example.game].append(record)
        partition_by_game[record.example.game] = record.example.partition
    partition_metrics = tuple(
        PartitionBaselineMetrics(partition, _aggregate(tuple(by_partition[partition])))
        for partition in DatasetPartition
        if by_partition[partition]
    )
    game_metrics = tuple(
        GameBaselineMetrics(
            assignment.game,
            partition_by_game[assignment.game],
            _aggregate(tuple(by_game[assignment.game])),
        )
        for assignment in dataset.games
    )
    return BaselineReport(
        dataset_identity=dataset.dataset_identity,
        records=records,
        partition_metrics=partition_metrics,
        game_metrics=game_metrics,
    )


def metrics_value(metrics: BeliefQualityMetrics) -> dict[str, object]:
    expected = metrics.expected_count
    red = metrics.red_five
    wait = metrics.structural_wait_coverage
    return {
        "samples": expected.sample_count,
        "expected_count": {
            "per_tile_mae": expected.per_tile_mae,
            "per_hand_l1": expected.per_hand_l1,
            "concealed_size_inconsistency_mean": (
                expected.concealed_size_inconsistency_mean
            ),
            "concealed_size_inconsistency_max": (
                expected.concealed_size_inconsistency_max
            ),
            "physical_conservation_violation_sample_rate": (
                expected.conservation_violation_sample_rate
            ),
            "conservation_total_excess": expected.conservation_total_excess,
            "conservation_mean_excess_per_sample": (
                expected.conservation_mean_excess_per_sample
            ),
        },
        "red_five": {"brier_score": red.brier_score, "mae": red.mae},
        "structural_wait_coverage": {
            "available_count": wait.structural_wait_available,
            "available_rate": wait.available_rate,
            "unavailable_count": wait.structural_wait_unavailable,
            "unavailable_rate": wait.unavailable_rate,
            "unavailable_reasons": dict(wait.unavailable_reasons),
            "available_all_zero_count": wait.structural_wait_all_zero,
            "available_all_zero_rate": (
                wait.structural_wait_all_zero / wait.structural_wait_available
                if wait.structural_wait_available
                else 0.0
            ),
            "available_non_zero_count": wait.structural_wait_non_zero,
            "available_non_zero_rate": (
                wait.structural_wait_non_zero / wait.structural_wait_available
                if wait.structural_wait_available
                else 0.0
            ),
        },
    }


def baseline_report_value(report: BaselineReport) -> dict[str, object]:
    return {
        "dataset_identity": report.dataset_identity,
        "partitions": {
            value.partition.value: metrics_value(value.metrics)
            for value in report.partition_metrics
        },
        "games": [
            {
                "source_class": value.game.source_class,
                "game_seed": value.game.game_seed,
                "partition": value.partition.value,
                "metrics": metrics_value(value.metrics),
            }
            for value in report.game_metrics
        ],
    }


__all__ = [
    "CONSERVATION_VIOLATION_TOLERANCE",
    "BaselineReport",
    "BeliefQualityMetrics",
    "ExpectedCountMetrics",
    "GameBaselineMetrics",
    "PartitionBaselineMetrics",
    "RedFiveMetrics",
    "SampleMeasurementRecord",
    "baseline_report_value",
    "evaluate_baseline_predictions",
    "metrics_value",
]
