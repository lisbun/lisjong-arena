"""Deterministic TRAIN-only E160 prevalence-offset linear probe for Arena #291."""

import math

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_public_riichi_wait_readout.model import (
    assert_frozen_state_unchanged,
)

from .data import centered_latent, validate_centering
from .protocol import (
    LATENT_DIM,
    OUTPUT_ROWS,
    PARAMETER_COUNT,
    SCHEMA,
    SOLVER,
    TILE_KIND_COUNT,
    E160OffsetProbeError,
    exact,
    identity,
    solver_value,
    validate_probability,
)


def _logit(probability: float) -> float:
    value = validate_probability(probability, "baseline probability")
    return math.log(value) - math.log1p(-value)


def _sigmoid(logit: float) -> float:
    if not math.isfinite(logit):
        raise E160OffsetProbeError("probe logit must be finite")
    if logit >= 0:
        value = 1.0 / (1.0 + math.exp(-logit))
    else:
        exp_value = math.exp(logit)
        value = exp_value / (1.0 + exp_value)
    if not 0.0 < value < 1.0:
        raise E160OffsetProbeError("probe probability must be strictly inside (0,1)")
    return value


def correction_logit(weights, centered_values) -> float:
    if len(weights) != LATENT_DIM or len(centered_values) != LATENT_DIM:
        raise E160OffsetProbeError("probe vector dimension differs from 128")
    value = sum(
        float(weight) * float(feature)
        for weight, feature in zip(weights, centered_values, strict=True)
    )
    if not math.isfinite(value):
        raise E160OffsetProbeError("probe correction logit must be finite")
    return value


def predict_probability(
    baseline_probability: float,
    weights,
    centered_values,
) -> float:
    baseline_probability = validate_probability(
        baseline_probability, "baseline probability"
    )
    correction = correction_logit(weights, centered_values)
    if correction == 0.0:
        return baseline_probability
    return _sigmoid(_logit(baseline_probability) + correction)


def _grouped_training_tensors(records: tuple, centering: dict, baseline: dict):
    import torch

    if not records or any(
        record.partition is not DatasetPartition.TRAIN for record in records
    ):
        raise E160OffsetProbeError("probe fitting accepts TRAIN records only")
    validate_centering(centering)
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise E160OffsetProbeError("baseline must contain exactly 34 probabilities")
    offsets = torch.tensor(
        [_logit(float(probability)) for probability in probabilities],
        dtype=torch.float64,
    )
    grouped = []
    for row_index in range(OUTPUT_ROWS):
        features = []
        labels = []
        for record in records:
            target = record.targets[row_index]
            if not target.eligible:
                continue
            features.append(centered_latent(record, row_index, centering))
            labels.append(tuple(float(value) for value in target.mask))
        if not features:
            raise E160OffsetProbeError(
                "every output row requires TRAIN eligible examples"
            )
        grouped.append(
            (
                torch.tensor(features, dtype=torch.float64),
                torch.tensor(labels, dtype=torch.float64),
            )
        )
    return tuple(grouped), offsets


def create_probe_optimizer(weights, frozen_model):
    """Create the locked optimizer and prove it contains no frozen E160 parameter."""
    import torch

    frozen_ids = {id(parameter) for parameter in frozen_model.parameters()}
    optimizer = create_probe_optimizer(weights, frozen_model)
    optimizer_parameters = tuple(
        parameter for group in optimizer.param_groups for parameter in group["params"]
    )
    if len(optimizer_parameters) != 1 or optimizer_parameters[0] is not weights:
        raise E160OffsetProbeError(
            "probe optimizer must contain exactly the standalone correction weights"
        )
    if any(id(parameter) in frozen_ids for parameter in optimizer_parameters):
        raise E160OffsetProbeError("probe optimizer contains a frozen E160 parameter")
    return optimizer


def fit_probe(
    records: tuple,
    baseline: dict,
    centering: dict,
    *,
    execution_lock_identity: str,
    latent_fingerprint: str,
    frozen_model,
    frozen_snapshot,
) -> dict[str, object]:
    """Fit exactly one bias-free [3,34,128] correction on TRAIN only."""
    import torch

    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    if torch.cuda.is_available():
        raise E160OffsetProbeError("Arena #291 requires the locked CPU-only solver")
    if any(parameter.requires_grad for parameter in frozen_model.parameters()):
        raise E160OffsetProbeError("frozen E160 parameters must not require gradients")

    grouped, offsets = _grouped_training_tensors(records, centering, baseline)
    weights = torch.zeros(
        (OUTPUT_ROWS, TILE_KIND_COUNT, LATENT_DIM),
        dtype=torch.float64,
        requires_grad=True,
    )
    total_cells = sum(int(labels.numel()) for _features, labels in grouped)

    def loss_value():
        total = torch.zeros((), dtype=torch.float64)
        for row_index, (features, labels) in enumerate(grouped):
            logits = offsets.unsqueeze(0) + features.matmul(weights[row_index].T)
            total = total + torch.nn.functional.binary_cross_entropy_with_logits(
                logits, labels, reduction="sum"
            )
        return total / total_cells

    initial_loss = float(loss_value().detach())
    optimizer = torch.optim.LBFGS(
        [weights],
        lr=SOLVER["learning_rate"],
        max_iter=SOLVER["max_iterations"],
        max_eval=SOLVER["max_evaluations"],
        tolerance_grad=SOLVER["tolerance_grad"],
        tolerance_change=SOLVER["tolerance_change"],
        history_size=SOLVER["history_size"],
        line_search_fn=SOLVER["line_search"],
    )

    def closure():
        optimizer.zero_grad(set_to_none=True)
        loss = loss_value()
        loss.backward()
        return loss

    optimizer.step(closure)
    final_loss_tensor = loss_value()
    gradient = torch.autograd.grad(final_loss_tensor, weights)[0]
    final_loss = float(final_loss_tensor.detach())
    weight_values = weights.detach().tolist()
    if (
        not math.isfinite(initial_loss)
        or not math.isfinite(final_loss)
        or not bool(torch.isfinite(weights).all())
        or not bool(torch.isfinite(gradient).all())
    ):
        raise E160OffsetProbeError("probe fit produced non-finite values")
    assert_frozen_state_unchanged(frozen_snapshot, frozen_model)
    if any(parameter.grad is not None for parameter in frozen_model.parameters()):
        raise E160OffsetProbeError(
            "frozen E160 parameters unexpectedly received gradients"
        )

    state = optimizer.state[weights]
    model = {
        "schema": SCHEMA + "/model",
        "execution_lock_identity": execution_lock_identity,
        "baseline_identity": identity(baseline),
        "centering_identity": centering["centering_identity"],
        "latent_fingerprint": latent_fingerprint,
        "architecture": {
            "kind": "prevalence-offset bias-free linear probe",
            "weight_shape": [OUTPUT_ROWS, TILE_KIND_COUNT, LATENT_DIM],
            "parameter_count": PARAMETER_COUNT,
            "free_intercept": False,
            "hidden_layers": [],
            "activation": None,
            "learned_embedding": False,
            "trunk_update": False,
        },
        "solver": solver_value(),
        "train_cells": total_cells,
        "initial_train_log_loss": initial_loss,
        "final_train_log_loss": final_loss,
        "optimizer_iterations": int(state.get("n_iter", 0)),
        "optimizer_function_evaluations": int(state.get("func_evals", 0)),
        "final_max_abs_gradient": float(gradient.abs().max()),
        "weights": weight_values,
    }
    return validate_model(
        model,
        execution_lock_identity,
        baseline,
        centering,
        latent_fingerprint,
    )


def validate_model(
    value: object,
    execution_lock_identity: str,
    baseline: dict,
    centering: dict,
    latent_fingerprint: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "execution_lock_identity",
        "baseline_identity",
        "centering_identity",
        "latent_fingerprint",
        "architecture",
        "solver",
        "train_cells",
        "initial_train_log_loss",
        "final_train_log_loss",
        "optimizer_iterations",
        "optimizer_function_evaluations",
        "final_max_abs_gradient",
        "weights",
    }:
        raise E160OffsetProbeError("probe model fields are not exact")
    exact(value["schema"], SCHEMA + "/model", "model schema")
    exact(
        value["execution_lock_identity"],
        execution_lock_identity,
        "model lock binding",
    )
    exact(value["baseline_identity"], identity(baseline), "model baseline identity")
    exact(
        value["centering_identity"],
        centering["centering_identity"],
        "model centering identity",
    )
    exact(value["latent_fingerprint"], latent_fingerprint, "model latent fingerprint")
    exact(
        value["architecture"],
        {
            "kind": "prevalence-offset bias-free linear probe",
            "weight_shape": [OUTPUT_ROWS, TILE_KIND_COUNT, LATENT_DIM],
            "parameter_count": PARAMETER_COUNT,
            "free_intercept": False,
            "hidden_layers": [],
            "activation": None,
            "learned_embedding": False,
            "trunk_update": False,
        },
        "probe architecture",
    )
    exact(value["solver"], solver_value(), "probe solver")
    if type(value["train_cells"]) is not int or value["train_cells"] <= 0:
        raise E160OffsetProbeError("model train_cells must be positive")
    for name in (
        "initial_train_log_loss",
        "final_train_log_loss",
        "final_max_abs_gradient",
    ):
        number = value[name]
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise E160OffsetProbeError(f"{name} must be finite and non-negative")
    for name in ("optimizer_iterations", "optimizer_function_evaluations"):
        if type(value[name]) is not int or value[name] < 0:
            raise E160OffsetProbeError(f"{name} must be a non-negative integer")
    if value["optimizer_iterations"] > SOLVER["max_iterations"]:
        raise E160OffsetProbeError("optimizer iterations exceed the locked budget")
    if value["optimizer_function_evaluations"] > SOLVER["max_evaluations"]:
        raise E160OffsetProbeError("optimizer evaluations exceed the locked budget")
    weights = value["weights"]
    if type(weights) is not list or len(weights) != OUTPUT_ROWS:
        raise E160OffsetProbeError("probe weights must contain three output rows")
    for row in weights:
        if type(row) is not list or len(row) != TILE_KIND_COUNT:
            raise E160OffsetProbeError("probe weight row must contain 34 tile vectors")
        for vector in row:
            if (
                type(vector) is not list
                or len(vector) != LATENT_DIM
                or any(
                    type(number) not in (int, float) or not math.isfinite(number)
                    for number in vector
                )
            ):
                raise E160OffsetProbeError("probe weight vector is invalid")
    return value


__all__ = [
    "correction_logit",
    "create_probe_optimizer",
    "fit_probe",
    "predict_probability",
    "validate_model",
]
