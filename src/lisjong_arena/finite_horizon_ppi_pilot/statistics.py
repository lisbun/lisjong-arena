"""Issue #279 task-specific prediction-powered mean inference.

This module intentionally implements only the locked hanchan-level mean estimand.
It is not a general-purpose PPI framework.
"""

from __future__ import annotations

import math
from statistics import NormalDist


def _values(values: object, name: str, *, minimum: int = 2) -> tuple[float, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be a numeric sequence")
    try:
        result = tuple(float(value) for value in values)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise TypeError(f"{name} must be a numeric sequence") from None
    if len(result) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} values")
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _mean(values: tuple[float, ...]) -> float:
    return sum(values) / len(values)


def _sample_variance(values: tuple[float, ...]) -> float:
    center = _mean(values)
    return sum((value - center) ** 2 for value in values) / (len(values) - 1)


def _sample_covariance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("paired sequences must have the same length")
    left_mean = _mean(left)
    right_mean = _mean(right)
    return sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    ) / (len(left) - 1)


def _interval(
    estimate: float, standard_error: float, confidence_level: float
) -> list[float]:
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0 and 1")
    if standard_error < 0.0 or not math.isfinite(standard_error):
        raise ValueError("standard_error must be finite and non-negative")
    alpha = 1.0 - confidence_level
    z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    return [estimate - z * standard_error, estimate + z * standard_error]


def _estimate_for_lambda(
    unlabeled_predictions: tuple[float, ...],
    labeled_predictions: tuple[float, ...],
    labeled_targets: tuple[float, ...],
    lam: float,
    confidence_level: float,
) -> dict[str, object]:
    residuals = tuple(
        target - lam * prediction
        for prediction, target in zip(labeled_predictions, labeled_targets)
    )
    transformed_unlabeled = tuple(lam * value for value in unlabeled_predictions)
    estimate = _mean(transformed_unlabeled) + _mean(residuals)
    variance = (
        _sample_variance(transformed_unlabeled) / len(transformed_unlabeled)
        + _sample_variance(residuals) / len(residuals)
    )
    standard_error = math.sqrt(max(variance, 0.0))
    interval = _interval(estimate, standard_error, confidence_level)
    return {
        "estimate": estimate,
        "ci": interval,
        "ci_width": interval[1] - interval[0],
        "standard_error": standard_error,
        "lambda": lam,
    }


def mean_inference(
    unlabeled_predictions: object,
    labeled_predictions: object,
    labeled_targets: object,
    *,
    confidence_level: float = 0.95,
) -> dict[str, object]:
    """Compare reference-only, basic PPI, and non-negative power-tuned PPI.

    U and L are disjoint independent hanchan samples. For a fixed lambda,

        theta_hat(lambda)
          = lambda * mean_U(X)
            + mean_L(Y - lambda X)

    Basic PPI uses lambda=1. The power-tuned value minimizes the plug-in
    asymptotic variance over this independent-U/L design and is clipped to
    [0, 1], so a useless or negatively associated predictor can be ignored.
    """
    u = _values(unlabeled_predictions, "unlabeled_predictions")
    x = _values(labeled_predictions, "labeled_predictions")
    y = _values(labeled_targets, "labeled_targets")
    if len(x) != len(y):
        raise ValueError("labeled_predictions and labeled_targets must align")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0 and 1")

    n = len(y)
    big_n = len(u)
    y_mean = _mean(y)
    reference_se = math.sqrt(_sample_variance(y) / n)
    reference_ci = _interval(y_mean, reference_se, confidence_level)
    reference = {
        "estimate": y_mean,
        "ci": reference_ci,
        "ci_width": reference_ci[1] - reference_ci[0],
        "standard_error": reference_se,
    }

    basic = _estimate_for_lambda(u, x, y, 1.0, confidence_level)

    covariance = _sample_covariance(x, y)
    denominator = _sample_variance(x) + (n / big_n) * _sample_variance(u)
    if denominator <= 0.0:
        lam = 0.0
    else:
        lam = min(1.0, max(0.0, covariance / denominator))
    power_tuned = _estimate_for_lambda(u, x, y, lam, confidence_level)

    residuals = tuple(target - prediction for prediction, target in zip(x, y))
    x_var = _sample_variance(x)
    y_var = _sample_variance(y)
    correlation = None
    if x_var > 0.0 and y_var > 0.0:
        correlation = covariance / math.sqrt(x_var * y_var)

    ref_width = float(reference["ci_width"])
    basic_width = float(basic["ci_width"])
    plus_width = float(power_tuned["ci_width"])
    basic_ratio = ref_width / basic_width if basic_width > 0.0 else None
    plus_ratio = ref_width / plus_width if plus_width > 0.0 else None
    equivalent = n * plus_ratio**2 if plus_ratio is not None else None

    return {
        "sample_size": {"cheap_only": big_n, "reference_labeled": n},
        "confidence_level": confidence_level,
        "reference_only": reference,
        "cheap_only_mean": _mean(u),
        "ppi": basic,
        "ppi_plus": power_tuned,
        "diagnostics": {
            "mean_reference_minus_cheap": _mean(residuals),
            "sd_reference_minus_cheap": math.sqrt(_sample_variance(residuals)),
            "correlation_reference_cheap": correlation,
        },
        "efficiency": {
            "reference_only_ci_width_over_ppi": basic_ratio,
            "reference_only_ci_width_over_ppi_plus": plus_ratio,
            "approx_high_fidelity_sample_equivalent": equivalent,
        },
    }
