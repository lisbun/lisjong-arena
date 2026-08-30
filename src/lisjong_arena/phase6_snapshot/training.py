"""Locked CPU train/validation orchestration for the Phase 6 model."""

import copy
import platform
import time
from dataclasses import dataclass
from math import isclose

from lisjong.belief import SCALE, wind_index
from lisjong.policy_contract import Wind

from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase5_belief_dataset.baseline import (
    predict_conditional_uniform_baseline,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountMetrics,
    ExpectedCountPrediction,
    ExpectedCountPredictionRow,
    evaluate_expected_count_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import (
    BeliefDataset,
    DatasetPartition,
    TurnExampleReference,
)

from .feature import build_phase6_snapshot_feature
from .model import create_model, parameter_count
from .tensor import tensor_values

LOCKED_RAW_CORPUS_IDENTITY = (
    "779599517d787a9515c69a9bd8bd610a491520d24799b0532cd05e2d0136c79e"
)
LOCKED_DATASET_IDENTITY = (
    "3167b61277a088f7c2b5c0e9e01aefb8af1b8c9c1609eb8de3f9a6bf7eff73e1"
)

_BASELINE_REFERENCE = {
    "per_tile_mae": 0.494942517957461,
    "per_hand_l1": 16.828045610553673,
    "concealed_size_inconsistency_mean": 0.0010961064194593756,
    "concealed_size_inconsistency_max": 0.0030517578125,
    "physical_conservation_violation_sample_rate": 0.0,
    "conservation_total_excess": 0.0,
}


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    seed: int = 0
    dataloader_seed: int = 0
    learning_rate: float = 1e-3
    weight_decay: float = 0.0
    batch_size: int = 256
    max_epochs: int = 40
    patience: int = 6
    workers: int = 0
    drop_last: bool = False
    deterministic_algorithms: bool = True
    torch_threads: int = 1

    def __post_init__(self) -> None:
        if type(self.seed) is not int or type(self.dataloader_seed) is not int:
            raise TypeError("training seeds must be ints")
        if self.learning_rate <= 0 or self.weight_decay < 0:
            raise ValueError("optimizer values are invalid")
        for name in ("batch_size", "max_epochs", "patience", "torch_threads"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive int")
        if self.workers != 0 or self.drop_last:
            raise ValueError("Phase 6 requires workers=0 and drop_last=False")


FORMAL_TRAINING_CONFIG = TrainingConfig()


@dataclass(frozen=True, slots=True)
class Phase6Example:
    example: TurnExampleReference
    sample: TrainingSample
    feature_values: tuple[float, ...]
    opponent_winds: tuple[Wind, Wind, Wind]
    row_marginals: tuple[float, float, float, float]
    column_marginals: tuple[float, ...]
    target: tuple[tuple[float, ...], ...]
    feature_coverage: "FeatureCoverage"


@dataclass(frozen=True, slots=True)
class FeatureCoverage:
    samples: int
    opponent_riichi_declaration_rows: int
    opponent_call_history_rows: int
    opponent_kan_history_rows: int
    meld_kind_counts: tuple[int, ...]
    public_draw_source_counts: tuple[int, ...]
    response_history_counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TrainValidationData:
    train: tuple[Phase6Example, ...]
    validation: tuple[Phase6Example, ...]

    def __post_init__(self) -> None:
        if not self.train or not self.validation:
            raise ValueError("both TRAIN and VALIDATION data are required")
        if any(
            value.example.partition is not DatasetPartition.TRAIN
            for value in self.train
        ):
            raise ValueError("training data contains a non-TRAIN example")
        if any(
            value.example.partition is not DatasetPartition.VALIDATION
            for value in self.validation
        ):
            raise ValueError("validation data contains a non-VALIDATION example")


@dataclass(frozen=True, slots=True)
class EpochLoss:
    epoch: int
    train_mse: float
    validation_mse: float


@dataclass(frozen=True, slots=True)
class InferenceThroughput:
    samples_per_second: float
    batch_size: int
    torch_thread_count: int
    platform: str


@dataclass(frozen=True, slots=True)
class TrainingResult:
    model: object
    config: TrainingConfig
    selected_epoch: int
    history: tuple[EpochLoss, ...]
    training_wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    train_mse: float
    validation_mse: float
    train_metrics: ExpectedCountMetrics
    validation_metrics: ExpectedCountMetrics
    constraint_maximum_residual: float
    constraint_non_convergence_count: int
    inference_throughput: InferenceThroughput
    parameter_count: int


def _wind_from_engine(engine_wind) -> Wind:
    return next(wind for wind in Wind if wind.value == engine_wind.value)


def build_phase6_example(
    example: TurnExampleReference,
    sample: TrainingSample,
) -> Phase6Example:
    """Materialize one example; the feature builder receives only the anchor."""
    if example.partition is DatasetPartition.TEST:
        raise ValueError("Phase 6 learned-model materialization rejects TEST")
    feature = build_phase6_snapshot_feature(sample.anchor)
    opponent_winds = tuple(_wind_from_engine(value.wind) for value in feature.opponents)
    rows_by_wind = {
        row.identity.wind: tuple(float(value) for value in row.counts)
        for row in sample.labels.expected_counts
    }
    if set(rows_by_wind) != set(opponent_winds):
        raise ValueError("feature and label opponent wind identities differ")
    opponent_slots = tuple(
        float(value.concealed_slot_count) for value in feature.opponents
    )
    other_hidden = float(sum(feature.remaining_tile_counts) - sum(opponent_slots))
    if other_hidden < 0:
        raise ValueError("remaining inventory implies negative other-hidden mass")
    row_marginals = opponent_slots + (other_hidden,)
    column_marginals = tuple(float(value) for value in feature.remaining_tile_counts)
    if sum(row_marginals) != sum(column_marginals):
        raise ValueError("row and column marginal total mass differ")
    return Phase6Example(
        example=example,
        sample=sample,
        feature_values=tensor_values(feature),
        opponent_winds=opponent_winds,
        row_marginals=row_marginals,
        column_marginals=column_marginals,
        target=tuple(rows_by_wind[wind] for wind in opponent_winds),
        feature_coverage=FeatureCoverage(
            samples=1,
            opponent_riichi_declaration_rows=sum(
                value.riichi_declaration_present for value in feature.opponents
            ),
            opponent_call_history_rows=sum(
                value.last_call_present for value in feature.opponents
            ),
            opponent_kan_history_rows=sum(
                value.last_kan_present for value in feature.opponents
            ),
            meld_kind_counts=tuple(
                sum(value.meld_kind_counts[index] for value in feature.opponents)
                for index in range(5)
            ),
            public_draw_source_counts=tuple(
                sum(
                    value.public_draw_source_counts[index]
                    for value in feature.opponents
                )
                for index in range(2)
            ),
            response_history_counts=feature.response_history_counts,
        ),
    )


def aggregate_feature_coverage(
    examples: tuple[Phase6Example, ...],
) -> FeatureCoverage:
    if not examples:
        raise ValueError("feature coverage requires at least one example")
    values = tuple(example.feature_coverage for example in examples)
    return FeatureCoverage(
        samples=sum(value.samples for value in values),
        opponent_riichi_declaration_rows=sum(
            value.opponent_riichi_declaration_rows for value in values
        ),
        opponent_call_history_rows=sum(
            value.opponent_call_history_rows for value in values
        ),
        opponent_kan_history_rows=sum(
            value.opponent_kan_history_rows for value in values
        ),
        meld_kind_counts=tuple(
            sum(value.meld_kind_counts[index] for value in values) for index in range(5)
        ),
        public_draw_source_counts=tuple(
            sum(value.public_draw_source_counts[index] for value in values)
            for index in range(2)
        ),
        response_history_counts=tuple(
            sum(value.response_history_counts[index] for value in values)
            for index in range(9)
        ),
    )


def prepare_train_validation_data(
    dataset: BeliefDataset,
    samples: tuple[TrainingSample, ...],
    *,
    example_builder=build_phase6_example,
) -> TrainValidationData:
    """Seal TEST before feature materialization or any model-facing operation."""
    if dataset.dataset_identity != LOCKED_DATASET_IDENTITY:
        raise ValueError("formal Phase 6 requires the locked Phase 5 dataset identity")
    if dataset.raw_corpus_identity != LOCKED_RAW_CORPUS_IDENTITY:
        raise ValueError("formal Phase 6 requires the locked Phase 5 raw identity")
    if len(dataset.examples) != len(samples):
        raise ValueError("dataset examples and resolved samples must align")
    train = []
    validation = []
    for example, sample in zip(dataset.examples, samples, strict=True):
        if example.partition is DatasetPartition.TEST:
            continue
        materialized = example_builder(example, sample)
        if example.partition is DatasetPartition.TRAIN:
            train.append(materialized)
        elif example.partition is DatasetPartition.VALIDATION:
            validation.append(materialized)
        else:  # pragma: no cover - closed enum future guard
            raise ValueError("unsupported Phase 6 partition")
    return TrainValidationData(tuple(train), tuple(validation))


def _baseline_expected_prediction(
    reference: TurnExampleReference,
    sample: TrainingSample,
) -> ExpectedCountPrediction:
    baseline = predict_conditional_uniform_baseline(reference, sample.anchor)
    rows = tuple(
        ExpectedCountPredictionRow(
            expected.identity.wind,
            tuple(
                value / SCALE
                for value in baseline.belief.hands[
                    wind_index(expected.identity.wind)
                ].expected_count_raw
            ),
            baseline.concealed_slot_counts_by_wind[wind_index(expected.identity.wind)],
        )
        for expected in sample.labels.expected_counts
    )
    return ExpectedCountPrediction(reference, rows)


def verify_phase5_validation_compatibility(
    dataset: BeliefDataset,
    samples: tuple[TrainingSample, ...],
) -> ExpectedCountMetrics:
    """Re-evaluate only locked VALIDATION against the current lisjong pin."""
    selected = tuple(
        (example, sample)
        for example, sample in zip(dataset.examples, samples, strict=True)
        if example.partition is DatasetPartition.VALIDATION
    )
    examples = tuple(value[0] for value in selected)
    selected_samples = tuple(value[1] for value in selected)
    predictions = tuple(
        _baseline_expected_prediction(example, sample) for example, sample in selected
    )
    report = evaluate_expected_count_predictions(
        dataset.dataset_identity, examples, selected_samples, predictions
    )
    metrics = report.partition_metrics[0].metrics
    actual = {
        "per_tile_mae": metrics.per_tile_mae,
        "per_hand_l1": metrics.per_hand_l1,
        "concealed_size_inconsistency_mean": (
            metrics.concealed_size_inconsistency_mean
        ),
        "concealed_size_inconsistency_max": metrics.concealed_size_inconsistency_max,
        "physical_conservation_violation_sample_rate": (
            metrics.conservation_violation_sample_rate
        ),
        "conservation_total_excess": metrics.conservation_total_excess,
    }
    for name, expected in _BASELINE_REFERENCE.items():
        if not isclose(actual[name], expected, rel_tol=0, abs_tol=1e-12):
            raise RuntimeError(
                f"Phase 5 validation compatibility drift for {name}: "
                f"{actual[name]!r} != {expected!r}"
            )
    return metrics


def _tensor_partition(values: tuple[Phase6Example, ...]):
    import torch

    return torch.utils.data.TensorDataset(
        torch.tensor([value.feature_values for value in values], dtype=torch.float32),
        torch.tensor([value.row_marginals for value in values], dtype=torch.float64),
        torch.tensor([value.column_marginals for value in values], dtype=torch.float64),
        torch.tensor([value.target for value in values], dtype=torch.float64),
    )


def _loader(dataset, *, config: TrainingConfig, shuffle: bool):
    import torch

    generator = None
    if shuffle:
        generator = torch.Generator().manual_seed(config.dataloader_seed)
    return torch.utils.data.DataLoader(
        dataset,
        batch_size=config.batch_size,
        shuffle=shuffle,
        generator=generator,
        num_workers=config.workers,
        drop_last=config.drop_last,
    )


def _evaluate_mse(model, loader) -> tuple[float, float]:
    import torch

    squared_error_sum = 0.0
    cell_count = 0
    maximum_residual = 0.0
    model.eval()
    with torch.no_grad():
        for features, rows, columns, target in loader:
            constrained = model(features, rows, columns)
            prediction = constrained.allocation[:, :3, :]
            squared_error_sum += float(torch.square(prediction - target).sum())
            cell_count += target.numel()
            maximum_residual = max(maximum_residual, constrained.maximum_residual)
    return squared_error_sum / cell_count, maximum_residual


def _predict(
    model,
    loader,
    examples: tuple[Phase6Example, ...],
) -> tuple[tuple[ExpectedCountPrediction, ...], float]:
    import torch

    predictions = []
    maximum_residual = 0.0
    offset = 0
    model.eval()
    with torch.no_grad():
        for features, rows, columns, _target in loader:
            constrained = model(features, rows, columns)
            values = constrained.allocation[:, :3, :].tolist()
            maximum_residual = max(maximum_residual, constrained.maximum_residual)
            for batch_row in values:
                source = examples[offset]
                predictions.append(
                    ExpectedCountPrediction(
                        source.example,
                        tuple(
                            ExpectedCountPredictionRow(wind, tuple(row), int(slot))
                            for wind, row, slot in zip(
                                source.opponent_winds,
                                batch_row,
                                source.row_marginals[:3],
                                strict=True,
                            )
                        ),
                    )
                )
                offset += 1
    if offset != len(examples):
        raise RuntimeError("inference output count differs from examples")
    return tuple(predictions), maximum_residual


def _peak_process_ram_bytes() -> int | None:
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            get_process = ctypes.windll.kernel32.GetCurrentProcess
            get_process.restype = wintypes.HANDLE
            get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_Counters),
                wintypes.DWORD,
            ]
            get_memory_info.restype = wintypes.BOOL
            if not get_memory_info(
                get_process(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return None
            return int(counters.PeakWorkingSetSize)
        except AttributeError, OSError:
            return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage if platform.system() == "Darwin" else usage * 1024)
    except ImportError, OSError:
        return None


def _benchmark(
    model, loader, sample_count: int, config: TrainingConfig
) -> InferenceThroughput:
    import torch

    start = time.perf_counter()
    model.eval()
    with torch.no_grad():
        for features, rows, columns, _target in loader:
            model(features, rows, columns)
    seconds = time.perf_counter() - start
    return InferenceThroughput(
        samples_per_second=sample_count / seconds,
        batch_size=config.batch_size,
        torch_thread_count=config.torch_threads,
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
    )


def train_phase6_model(
    data: TrainValidationData,
    *,
    dataset_identity: str,
    config: TrainingConfig = FORMAL_TRAINING_CONFIG,
) -> TrainingResult:
    """Fit on TRAIN, select on VALIDATION, and never accept TEST input."""
    import torch

    if dataset_identity != LOCKED_DATASET_IDENTITY:
        raise ValueError("training requires the locked dataset identity")
    torch.manual_seed(config.seed)
    torch.use_deterministic_algorithms(config.deterministic_algorithms)
    torch.set_num_threads(config.torch_threads)
    model = create_model()
    train_dataset = _tensor_partition(data.train)
    validation_dataset = _tensor_partition(data.validation)
    train_loader = _loader(train_dataset, config=config, shuffle=True)
    validation_loader = _loader(validation_dataset, config=config, shuffle=False)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    best_loss = float("inf")
    best_epoch = 0
    best_state = None
    no_improvement = 0
    history = []
    maximum_residual = 0.0
    started = time.perf_counter()
    for epoch in range(1, config.max_epochs + 1):
        model.train()
        train_squared_error = 0.0
        train_cells = 0
        for features, rows, columns, target in train_loader:
            optimizer.zero_grad(set_to_none=True)
            constrained = model(features, rows, columns)
            prediction = constrained.allocation[:, :3, :]
            loss = torch.mean(torch.square(prediction - target))
            loss.backward()
            if any(
                parameter.grad is not None
                and not bool(torch.isfinite(parameter.grad).all())
                for parameter in model.parameters()
            ):
                raise RuntimeError("training produced a non-finite gradient")
            optimizer.step()
            train_squared_error += float(
                torch.square(prediction.detach() - target).sum()
            )
            train_cells += target.numel()
            maximum_residual = max(maximum_residual, constrained.maximum_residual)
        train_mse = train_squared_error / train_cells
        validation_mse, validation_residual = _evaluate_mse(model, validation_loader)
        maximum_residual = max(maximum_residual, validation_residual)
        history.append(EpochLoss(epoch, train_mse, validation_mse))
        if validation_mse < best_loss:
            best_loss = validation_mse
            best_epoch = epoch
            best_state = copy.deepcopy(model.state_dict())
            no_improvement = 0
        else:
            no_improvement += 1
            if no_improvement >= config.patience:
                break
    wall_clock = time.perf_counter() - started
    if best_state is None:
        raise RuntimeError("training did not produce a validation checkpoint")
    model.load_state_dict(best_state)

    ordered_train_loader = _loader(train_dataset, config=config, shuffle=False)
    train_mse, train_residual = _evaluate_mse(model, ordered_train_loader)
    validation_mse, validation_residual = _evaluate_mse(model, validation_loader)
    train_predictions, train_prediction_residual = _predict(
        model, ordered_train_loader, data.train
    )
    validation_predictions, validation_prediction_residual = _predict(
        model, validation_loader, data.validation
    )
    maximum_residual = max(
        maximum_residual,
        train_residual,
        validation_residual,
        train_prediction_residual,
        validation_prediction_residual,
    )
    train_report = evaluate_expected_count_predictions(
        dataset_identity,
        tuple(value.example for value in data.train),
        tuple(value.sample for value in data.train),
        train_predictions,
    )
    validation_report = evaluate_expected_count_predictions(
        dataset_identity,
        tuple(value.example for value in data.validation),
        tuple(value.sample for value in data.validation),
        validation_predictions,
    )
    throughput = _benchmark(model, validation_loader, len(data.validation), config)
    return TrainingResult(
        model=model,
        config=config,
        selected_epoch=best_epoch,
        history=tuple(history),
        training_wall_clock_seconds=wall_clock,
        peak_process_ram_bytes=_peak_process_ram_bytes(),
        train_mse=train_mse,
        validation_mse=validation_mse,
        train_metrics=train_report.partition_metrics[0].metrics,
        validation_metrics=validation_report.partition_metrics[0].metrics,
        constraint_maximum_residual=maximum_residual,
        constraint_non_convergence_count=0,
        inference_throughput=throughput,
        parameter_count=parameter_count(model),
    )


__all__ = [
    "FORMAL_TRAINING_CONFIG",
    "LOCKED_DATASET_IDENTITY",
    "LOCKED_RAW_CORPUS_IDENTITY",
    "EpochLoss",
    "FeatureCoverage",
    "InferenceThroughput",
    "Phase6Example",
    "TrainingConfig",
    "TrainingResult",
    "TrainValidationData",
    "build_phase6_example",
    "aggregate_feature_coverage",
    "prepare_train_validation_data",
    "train_phase6_model",
    "verify_phase5_validation_compatibility",
]
