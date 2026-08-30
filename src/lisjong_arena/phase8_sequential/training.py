"""Deterministic TRAIN-only fitting and VALIDATION-MAE checkpoint selection."""

import copy
import platform
import time
from dataclasses import dataclass

from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountMetrics,
    evaluate_expected_count_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition

from .evaluation import CandidateEvaluation, evaluate_candidate
from .model import S2_LATENT_DIM, create_model, parameter_count
from .protocol import (
    BpttMode,
    BpttPolicy,
    Candidate,
    Phase8Sequence,
    checkpoint_improves,
)
from .rollout import (
    _initial_tensor_rows,
    _remap_tensor_rows,
    _step_tensors,
    detach_recurrent_state,
    flatten_sequences,
    self_rollout,
)


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    seed: int = 0
    dataloader_seed: int = 0
    learning_rate: float = 1e-3
    weight_decay: float = 0.0
    max_epochs: int = 40
    patience: int = 6
    workers: int = 0
    deterministic_algorithms: bool = True
    torch_threads: int = 1

    def __post_init__(self) -> None:
        if type(self.seed) is not int or type(self.dataloader_seed) is not int:
            raise TypeError("training seeds must be ints")
        if self.learning_rate <= 0 or self.weight_decay != 0:
            raise ValueError("Phase 8 optimizer values differ from the lock")
        for name in ("max_epochs", "patience", "torch_threads"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive int")
        if self.workers != 0:
            raise ValueError("Phase 8 requires workers=0")


FORMAL_TRAINING_CONFIG = TrainingConfig()


@dataclass(frozen=True, slots=True)
class EpochMetrics:
    epoch: int
    train_mse: float
    validation_mae: float


@dataclass(frozen=True, slots=True)
class InferenceThroughput:
    samples_per_second: float
    torch_thread_count: int
    platform: str


@dataclass(frozen=True, slots=True)
class TrainingResult:
    candidate: Candidate
    model: object
    config: TrainingConfig
    bptt_policy: BpttPolicy
    selected_epoch: int
    history: tuple[EpochMetrics, ...]
    training_wall_clock_seconds: float
    peak_process_ram_bytes: int | None
    train_mse: float
    train_metrics: ExpectedCountMetrics
    validation: CandidateEvaluation
    inference_throughput: InferenceThroughput
    parameter_count: int


def _peak_process_ram_bytes() -> int | None:
    from lisjong_arena.phase6_snapshot.training import _peak_process_ram_bytes as peak

    return peak()


def _partition_sequences(
    sequences: tuple[Phase8Sequence, ...], partition: DatasetPartition
) -> tuple[Phase8Sequence, ...]:
    selected = tuple(value for value in sequences if value.partition is partition)
    if not selected:
        raise ValueError(f"training requires {partition.value} sequences")
    return selected


def _metrics(dataset_identity: str, sequences: tuple, predictions: tuple):
    examples = flatten_sequences(sequences)
    report = evaluate_expected_count_predictions(
        dataset_identity,
        tuple(value.example for value in examples),
        tuple(value.sample for value in examples),
        predictions,
    )
    return report.partition_metrics[0].metrics


def _chunk_ranges(length: int, policy: BpttPolicy):
    if policy.mode is BpttMode.FULL_SEQUENCE:
        return ((0, length),)
    size = policy.truncation_length
    return tuple((start, min(start + size, length)) for start in range(0, length, size))


def _train_one_sequence(
    model,
    candidate,
    sequence,
    policy,
    *,
    objective_cell_count: int,
):
    import torch

    if type(objective_cell_count) is not int or objective_cell_count <= 0:
        raise ValueError("objective_cell_count must be a positive int")
    rows_by_wind = None
    latent = None
    squared_error_sum = 0.0
    cell_count = 0
    maximum_residual = 0.0
    for start, stop in _chunk_ranges(len(sequence.steps), policy):
        chunk_squared_error = None
        for step in sequence.steps[start:stop]:
            if rows_by_wind is None:
                _value, rows_by_wind = _initial_tensor_rows(step)
                if candidate is Candidate.S2:
                    latent = torch.zeros((1, S2_LATENT_DIM), dtype=torch.float32)
            previous = _remap_tensor_rows(rows_by_wind, step.opponent_winds)
            features, row_marginals, column_marginals = _step_tensors(step)
            if candidate is Candidate.S1:
                constrained = model(features, previous, row_marginals, column_marginals)
            else:
                constrained, latent = model(
                    features,
                    previous,
                    latent,
                    row_marginals,
                    column_marginals,
                )
            prediction = constrained.allocation[:, :3, :]
            target = torch.tensor(step.target, dtype=torch.float64).unsqueeze(0)
            step_squared_error = torch.square(prediction - target).sum()
            chunk_squared_error = (
                step_squared_error
                if chunk_squared_error is None
                else chunk_squared_error + step_squared_error
            )
            squared_error_sum += float(step_squared_error.detach())
            cell_count += target.numel()
            maximum_residual = max(maximum_residual, constrained.maximum_residual)
            rows_by_wind = {
                wind: prediction[0, index, :]
                for index, wind in enumerate(step.opponent_winds)
            }
        loss = chunk_squared_error / objective_cell_count
        loss.backward()
        if policy.mode is BpttMode.TRUNCATED and stop < len(sequence.steps):
            rows_by_wind, latent = detach_recurrent_state(rows_by_wind, latent)
    return squared_error_sum, cell_count, maximum_residual


def _train_pooled_epoch(
    model,
    candidate: Candidate,
    sequences: tuple[Phase8Sequence, ...],
    policy: BpttPolicy,
    optimizer,
    order: tuple[int, ...] | list[int],
) -> tuple[float, int, float]:
    """Apply one Adam update for the pooled expected-count cell objective."""
    import torch

    expected_order = set(range(len(sequences)))
    if len(order) != len(sequences) or set(order) != expected_order:
        raise ValueError("training order must contain every sequence exactly once")
    objective_cell_count = sum(len(sequence.steps) * 3 * 34 for sequence in sequences)
    optimizer.zero_grad(set_to_none=True)
    squared_error_sum = 0.0
    cell_count = 0
    maximum_residual = 0.0
    for index in order:
        sequence_error, sequence_cells, sequence_residual = _train_one_sequence(
            model,
            candidate,
            sequences[index],
            policy,
            objective_cell_count=objective_cell_count,
        )
        squared_error_sum += sequence_error
        cell_count += sequence_cells
        maximum_residual = max(maximum_residual, sequence_residual)
    if cell_count != objective_cell_count:
        raise RuntimeError(
            "TRAIN objective cell count differs from materialized targets"
        )
    if any(
        parameter.grad is not None and not bool(torch.isfinite(parameter.grad).all())
        for parameter in model.parameters()
    ):
        raise RuntimeError("training produced a non-finite gradient")
    optimizer.step()
    return squared_error_sum, cell_count, maximum_residual


def train_candidate(
    candidate: Candidate,
    sequences: tuple[Phase8Sequence, ...],
    *,
    dataset_identity: str,
    bptt_policy: BpttPolicy,
    snapshot_validation_predictions: tuple,
    config: TrainingConfig = FORMAL_TRAINING_CONFIG,
) -> TrainingResult:
    """Fit on TRAIN and select strictly by pooled self-rollout VALIDATION MAE."""
    import torch

    train_sequences = _partition_sequences(sequences, DatasetPartition.TRAIN)
    validation_sequences = _partition_sequences(sequences, DatasetPartition.VALIDATION)
    torch.manual_seed(config.seed)
    torch.use_deterministic_algorithms(config.deterministic_algorithms)
    torch.set_num_threads(config.torch_threads)
    model = create_model(candidate)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    generator = torch.Generator().manual_seed(config.dataloader_seed)
    best_mae = float("inf")
    best_epoch = 0
    best_state = None
    no_improvement = 0
    history = []
    started = time.perf_counter()
    for epoch in range(1, config.max_epochs + 1):
        model.train()
        order = torch.randperm(len(train_sequences), generator=generator).tolist()
        squared_error_sum, cell_count, _residual = _train_pooled_epoch(
            model,
            candidate,
            train_sequences,
            bptt_policy,
            optimizer,
            order,
        )
        validation_rollout = self_rollout(model, candidate, validation_sequences)
        validation_metrics = _metrics(
            dataset_identity,
            validation_sequences,
            validation_rollout.predictions,
        )
        validation_mae = validation_metrics.per_tile_mae
        history.append(
            EpochMetrics(epoch, squared_error_sum / cell_count, validation_mae)
        )
        if checkpoint_improves(validation_mae, best_mae):
            best_mae = validation_mae
            best_epoch = epoch
            best_state = copy.deepcopy(model.state_dict())
            no_improvement = 0
        else:
            no_improvement += 1
            if no_improvement >= config.patience:
                break
    training_seconds = time.perf_counter() - started
    if best_state is None:
        raise RuntimeError("training did not produce a validation checkpoint")
    model.load_state_dict(best_state, strict=True)
    train_rollout = self_rollout(model, candidate, train_sequences)
    validation_rollout = self_rollout(model, candidate, validation_sequences)
    train_metrics = _metrics(
        dataset_identity, train_sequences, train_rollout.predictions
    )
    validation = evaluate_candidate(
        candidate,
        validation_sequences,
        validation_rollout,
        snapshot_validation_predictions,
        dataset_identity=dataset_identity,
    )
    return TrainingResult(
        candidate=candidate,
        model=model,
        config=config,
        bptt_policy=bptt_policy,
        selected_epoch=best_epoch,
        history=tuple(history),
        training_wall_clock_seconds=training_seconds,
        peak_process_ram_bytes=_peak_process_ram_bytes(),
        train_mse=next(
            value.train_mse for value in history if value.epoch == best_epoch
        ),
        train_metrics=train_metrics,
        validation=validation,
        inference_throughput=InferenceThroughput(
            samples_per_second=(
                len(flatten_sequences(validation_sequences))
                / validation_rollout.wall_clock_seconds
            ),
            torch_thread_count=config.torch_threads,
            platform=(
                f"{platform.system()} {platform.release()} ({platform.machine()})"
            ),
        ),
        parameter_count=parameter_count(model),
    )


__all__ = [
    "FORMAL_TRAINING_CONFIG",
    "EpochMetrics",
    "InferenceThroughput",
    "TrainingConfig",
    "TrainingResult",
    "train_candidate",
]
