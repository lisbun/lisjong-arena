"""Deterministic convex offset-logistic fit for Arena #222."""

import math

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition

from .data import eligible_cells
from .protocol import (
    FEATURE_DIM,
    FEATURE_NAMES,
    FEATURE_SEMANTICS_ID,
    SCHEMA,
    SOLVER,
    TILE_KIND_COUNT,
    ClassicalWaitError,
    exact,
    identity,
    solver_value,
    validate_probability,
)


def _logit(probability: float) -> float:
    probability = validate_probability(probability, "baseline probability")
    return math.log(probability) - math.log1p(-probability)


def _sigmoid(logit: float) -> float:
    if not math.isfinite(logit):
        raise ClassicalWaitError("classical logit must be finite")
    if logit >= 0:
        value = 1.0 / (1.0 + math.exp(-logit))
    else:
        exp_value = math.exp(logit)
        value = exp_value / (1.0 + exp_value)
    if not 0.0 <= value <= 1.0:
        raise ClassicalWaitError("classical probability is outside [0,1]")
    return value


def correction_logit(weights: tuple[float, ...] | list[float], features) -> float:
    if len(weights) != FEATURE_DIM or len(features) != FEATURE_DIM:
        raise ClassicalWaitError("correction vector dimension differs from the lock")
    value = sum(float(weight) * float(feature) for weight, feature in zip(weights, features, strict=True))
    if not math.isfinite(value):
        raise ClassicalWaitError("classical correction logit must be finite")
    return value


def predict_probability(
    baseline_probability: float,
    weights: tuple[float, ...] | list[float],
    features,
) -> float:
    """Apply the locked correction; exact zero correction returns baseline exactly."""
    baseline_probability = validate_probability(
        baseline_probability, "baseline probability"
    )
    correction = correction_logit(weights, features)
    if correction == 0.0:
        return baseline_probability
    return _sigmoid(_logit(baseline_probability) + correction)


def _training_tensors(records: tuple, baseline: dict):
    import torch

    if not records or any(
        record.partition is not DatasetPartition.TRAIN for record in records
    ):
        raise ClassicalWaitError("classical fitting accepts TRAIN records only")
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise ClassicalWaitError("baseline must contain exactly 34 probabilities")
    features = []
    targets = []
    offsets = []
    for _record, _target, tile_index, vector, label in eligible_cells(records):
        features.append(vector)
        targets.append(float(label))
        offsets.append(_logit(float(probabilities[tile_index])))
    if not features:
        raise ClassicalWaitError("TRAIN contains no eligible structural-wait cells")
    return (
        torch.tensor(features, dtype=torch.float64),
        torch.tensor(targets, dtype=torch.float64),
        torch.tensor(offsets, dtype=torch.float64),
    )


def fit_offset_logistic(
    records: tuple,
    baseline: dict,
    *,
    execution_lock_identity: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Fit one full-batch no-intercept logistic correction on TRAIN only."""
    import torch

    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    if torch.cuda.is_available():
        raise ClassicalWaitError("Arena #222 uses the locked CPU-only solver")

    x, y, offsets = _training_tensors(records, baseline)
    weights = torch.zeros(FEATURE_DIM, dtype=torch.float64, requires_grad=True)

    def loss_value():
        logits = offsets + x.mv(weights)
        return torch.nn.functional.binary_cross_entropy_with_logits(
            logits, y, reduction="mean"
        )

    initial_loss = float(loss_value().detach())
    optimizer = torch.optim.LBFGS(
        [weights],
        lr=SOLVER["learning_rate"],
        max_iter=SOLVER["max_iterations"],
        max_eval=SOLVER["max_evaluations"],
        tolerance_grad=SOLVER["tolerance_grad"],
        tolerance_change=SOLVER["tolerance_change"],
        history_size=100,
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
    weight_values = [float(value) for value in weights.detach().tolist()]
    if (
        not math.isfinite(initial_loss)
        or not math.isfinite(final_loss)
        or any(not math.isfinite(value) for value in weight_values)
    ):
        raise ClassicalWaitError("classical fit produced non-finite values")

    state = optimizer.state[weights]
    baseline_identity = identity(baseline)
    model = {
        "schema": SCHEMA + "/classical-model",
        "execution_lock_identity": execution_lock_identity,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_names": list(FEATURE_NAMES),
        "architecture": {
            "kind": "prevalence-offset logistic correction",
            "input_dimension": FEATURE_DIM,
            "free_intercept": False,
            "hidden_layers": [],
            "learned_embedding": False,
            "e160_latent": False,
        },
        "baseline_identity": baseline_identity,
        "solver": solver_value(),
        "train_cells": int(y.numel()),
        "initial_train_log_loss": initial_loss,
        "final_train_log_loss": final_loss,
        "optimizer_iterations": int(state.get("n_iter", 0)),
        "optimizer_function_evaluations": int(state.get("func_evals", 0)),
        "final_max_abs_gradient": float(gradient.abs().max()),
        "weights": weight_values,
    }
    validate_model(model, execution_lock_identity, baseline)

    feature_rows = []
    for index, name in enumerate(FEATURE_NAMES):
        column = x[:, index]
        feature_rows.append(
            {
                "name": name,
                "minimum": float(column.min()),
                "maximum": float(column.max()),
                "mean": float(column.mean()),
            }
        )
    feature_summary = {
        "schema": SCHEMA + "/feature-summary",
        "execution_lock_identity": execution_lock_identity,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "partition": "train",
        "cells": int(y.numel()),
        "features": feature_rows,
    }
    return model, feature_summary


def validate_model(
    value: object, execution_lock_identity: str, baseline: dict
) -> dict[str, object]:
    if type(value) is not dict:
        raise ClassicalWaitError("classical model must be a JSON object")
    expected_fields = {
        "schema",
        "execution_lock_identity",
        "feature_semantics_id",
        "feature_names",
        "architecture",
        "baseline_identity",
        "solver",
        "train_cells",
        "initial_train_log_loss",
        "final_train_log_loss",
        "optimizer_iterations",
        "optimizer_function_evaluations",
        "final_max_abs_gradient",
        "weights",
    }
    if set(value) != expected_fields:
        raise ClassicalWaitError("classical model fields are not exact")
    exact(value["schema"], SCHEMA + "/classical-model", "model schema")
    exact(
        value["execution_lock_identity"],
        execution_lock_identity,
        "model execution-lock binding",
    )
    exact(value["feature_semantics_id"], FEATURE_SEMANTICS_ID, "feature semantics")
    exact(value["feature_names"], list(FEATURE_NAMES), "feature names")
    exact(
        value["architecture"],
        {
            "kind": "prevalence-offset logistic correction",
            "input_dimension": FEATURE_DIM,
            "free_intercept": False,
            "hidden_layers": [],
            "learned_embedding": False,
            "e160_latent": False,
        },
        "classical architecture",
    )
    exact(value["baseline_identity"], identity(baseline), "baseline identity")
    exact(value["solver"], solver_value(), "solver")
    if type(value["train_cells"]) is not int or value["train_cells"] <= 0:
        raise ClassicalWaitError("model train_cells must be positive")
    for name in (
        "initial_train_log_loss",
        "final_train_log_loss",
        "final_max_abs_gradient",
    ):
        number = value[name]
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ClassicalWaitError(f"{name} must be finite and non-negative")
    for name in ("optimizer_iterations", "optimizer_function_evaluations"):
        if type(value[name]) is not int or value[name] < 0:
            raise ClassicalWaitError(f"{name} must be a non-negative integer")
    weights = value["weights"]
    if (
        type(weights) is not list
        or len(weights) != FEATURE_DIM
        or any(type(number) not in (int, float) or not math.isfinite(number) for number in weights)
    ):
        raise ClassicalWaitError("classical weights are invalid")
    return value


__all__ = [
    "correction_logit",
    "fit_offset_logistic",
    "predict_probability",
    "validate_model",
]
