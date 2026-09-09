"""Deterministic readout-only training over frozen next-latent records."""

import copy
import time
from dataclasses import dataclass

from .evaluation import mean_binary_log_loss
from .model import (
    assert_frozen_state_unchanged,
    create_readout_head,
    create_readout_optimizer,
    readout_logits,
)
from .protocol import (
    FORMAL_TRAINING_CONFIG,
    Phase11Error,
    ReadoutTrainingConfig,
)


@dataclass(frozen=True, slots=True)
class EpochMetrics:
    epoch: int
    train_binary_log_loss: float
    validation_binary_log_loss: float


@dataclass(frozen=True, slots=True)
class TrainingResult:
    head: object
    config: ReadoutTrainingConfig
    selected_epoch: int
    history: tuple[EpochMetrics, ...]
    train_binary_log_loss: float
    validation_binary_log_loss: float
    training_wall_seconds: float
    optimizer_parameter_names: tuple[str, ...]
    frozen_digest_before: str
    frozen_digest_after: str


def _objective_cell_count(records: tuple) -> int:
    return sum(target.eligible for record in records for target in record.targets) * 34


def _train_epoch(head, records: tuple, optimizer, order: list[int]) -> float:
    import torch

    if len(order) != len(records) or set(order) != set(range(len(records))):
        raise Phase11Error(
            "training order must contain every TRAIN record exactly once"
        )
    objective_cells = _objective_cell_count(records)
    if objective_cells <= 0:
        raise Phase11Error("TRAIN contains no eligible target cells")
    optimizer.zero_grad(set_to_none=True)
    loss_sum = 0.0
    for index in order:
        record = records[index]
        logits = readout_logits(head, record.latent.unsqueeze(0))[0]
        record_loss = None
        for row_index, target in enumerate(record.targets):
            if not target.eligible:
                continue
            truth = torch.tensor(target.mask, dtype=torch.float32)
            row_loss = torch.nn.functional.binary_cross_entropy_with_logits(
                logits[row_index], truth, reduction="sum"
            )
            record_loss = row_loss if record_loss is None else record_loss + row_loss
            loss_sum += float(row_loss.detach())
        if record_loss is not None:
            (record_loss / objective_cells).backward()
    if any(
        parameter.grad is None or not bool(torch.isfinite(parameter.grad).all())
        for parameter in head.parameters()
    ):
        raise Phase11Error("readout training produced a missing or non-finite gradient")
    optimizer.step()
    return loss_sum / objective_cells


def _checkpoint_improves(candidate: float, best: float) -> bool:
    return candidate < best and abs(candidate - best) > 1e-12


def train_readout(
    frozen_model,
    frozen_snapshot,
    train_records: tuple,
    validation_records: tuple,
    *,
    config: ReadoutTrainingConfig = FORMAL_TRAINING_CONFIG,
) -> TrainingResult:
    """Fit only the fixed head and select by VALIDATION binary log loss."""
    import torch

    if not train_records or not validation_records:
        raise Phase11Error("readout training requires TRAIN and VALIDATION records")
    train_records = tuple(
        record
        for record in train_records
        if any(row.eligible for row in record.targets)
    )
    validation_records = tuple(
        record
        for record in validation_records
        if any(row.eligible for row in record.targets)
    )
    if not train_records or not validation_records:
        raise Phase11Error(
            "readout training requires eligible TRAIN and VALIDATION rows"
        )
    torch.manual_seed(config.seed)
    torch.use_deterministic_algorithms(config.deterministic_algorithms)
    torch.set_num_threads(config.torch_threads)
    head = create_readout_head()
    optimizer = create_readout_optimizer(
        head,
        frozen_model,
        learning_rate=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    generator = torch.Generator().manual_seed(config.dataloader_seed)
    best = float("inf")
    best_epoch = 0
    best_state = None
    no_improvement = 0
    history = []
    started = time.perf_counter()
    for epoch in range(1, config.max_epochs + 1):
        head.train()
        order = torch.randperm(len(train_records), generator=generator).tolist()
        train_loss = _train_epoch(head, train_records, optimizer, order)
        validation_loss = mean_binary_log_loss(head, validation_records)
        history.append(EpochMetrics(epoch, train_loss, validation_loss))
        if _checkpoint_improves(validation_loss, best):
            best = validation_loss
            best_epoch = epoch
            best_state = copy.deepcopy(head.state_dict())
            no_improvement = 0
        else:
            no_improvement += 1
            if no_improvement >= config.patience:
                break
    if best_state is None:
        raise Phase11Error("readout training did not produce a checkpoint")
    head.load_state_dict(best_state, strict=True)
    after = assert_frozen_state_unchanged(frozen_snapshot, frozen_model)
    return TrainingResult(
        head=head,
        config=config,
        selected_epoch=best_epoch,
        history=tuple(history),
        train_binary_log_loss=mean_binary_log_loss(head, train_records),
        validation_binary_log_loss=mean_binary_log_loss(head, validation_records),
        training_wall_seconds=time.perf_counter() - started,
        optimizer_parameter_names=tuple(name for name, _ in head.named_parameters()),
        frozen_digest_before=frozen_snapshot.digest,
        frozen_digest_after=after.digest,
    )


__all__ = ["EpochMetrics", "TrainingResult", "train_readout"]
