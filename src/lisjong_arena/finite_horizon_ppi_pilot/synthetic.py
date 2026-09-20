"""Synthetic validation for the Issue #279 fixed mean-inference implementation."""

from __future__ import annotations

import math
import random

from .statistics import mean_inference

NOMINAL_COVERAGE = 0.95
DEFAULT_REPLICATIONS = 500
_SYNTHETIC_U = 320
_SYNTHETIC_L = 64
_TRUE_MEAN = 0.25


def _one_sample(
    case: str, rng: random.Random
) -> tuple[list[float], list[float], list[float]]:
    def pair() -> tuple[float, float]:
        signal = rng.gauss(0.0, 1.0)
        target = _TRUE_MEAN + signal + rng.gauss(0.0, 0.35)
        if case == "informative":
            prediction = _TRUE_MEAN + signal + rng.gauss(0.0, 0.20)
        elif case == "biased_informative":
            prediction = _TRUE_MEAN + 1.25 + signal + rng.gauss(0.0, 0.20)
        elif case == "uninformative":
            prediction = 1.5 + rng.gauss(0.0, 1.5)
        elif case == "negative_pathological":
            prediction = _TRUE_MEAN - signal + rng.gauss(0.0, 0.20)
        else:
            raise ValueError(f"unknown synthetic case: {case}")
        return prediction, target

    unlabeled = [pair()[0] for _ in range(_SYNTHETIC_U)]
    labeled_pairs = [pair() for _ in range(_SYNTHETIC_L)]
    return (
        unlabeled,
        [prediction for prediction, _ in labeled_pairs],
        [target for _, target in labeled_pairs],
    )


def run_synthetic_validation(
    replications: int = DEFAULT_REPLICATIONS,
) -> dict[str, object]:
    if type(replications) is not int or replications < 100:
        raise ValueError("replications must be an integer >= 100")
    cases: dict[str, object] = {}
    for case_index, case in enumerate(
        (
            "informative",
            "biased_informative",
            "uninformative",
            "negative_pathological",
        )
    ):
        rng = random.Random(279_000 + case_index)
        coverage = {"reference_only": 0, "ppi": 0, "ppi_plus": 0}
        widths = {"reference_only": 0.0, "ppi": 0.0, "ppi_plus": 0.0}
        lambdas = []
        estimates = {
            "cheap_only": 0.0,
            "reference_only": 0.0,
            "ppi": 0.0,
            "ppi_plus": 0.0,
        }
        for _ in range(replications):
            u, x, y = _one_sample(case, rng)
            result = mean_inference(u, x, y, confidence_level=NOMINAL_COVERAGE)
            estimates["cheap_only"] += float(result["cheap_only_mean"])
            for method in coverage:
                item = result[method]
                assert isinstance(item, dict)
                interval = item["ci"]
                assert isinstance(interval, list)
                coverage[method] += int(interval[0] <= _TRUE_MEAN <= interval[1])
                widths[method] += float(item["ci_width"])
                estimates[method] += float(item["estimate"])
            ppi_plus = result["ppi_plus"]
            assert isinstance(ppi_plus, dict)
            lambdas.append(float(ppi_plus["lambda"]))

        monte_carlo_se = math.sqrt(
            NOMINAL_COVERAGE * (1.0 - NOMINAL_COVERAGE) / replications
        )
        cases[case] = {
            "replications": replications,
            "nominal_coverage": NOMINAL_COVERAGE,
            "monte_carlo_standard_error": monte_carlo_se,
            "observed_coverage": {
                key: count / replications for key, count in coverage.items()
            },
            "mean_ci_width": {
                key: value / replications for key, value in widths.items()
            },
            "mean_estimate": {
                key: value / replications for key, value in estimates.items()
            },
            "mean_ppi_plus_lambda": sum(lambdas) / len(lambdas),
        }
    return {
        "true_mean": _TRUE_MEAN,
        "unlabeled_sample_size": _SYNTHETIC_U,
        "labeled_sample_size": _SYNTHETIC_L,
        "cases": cases,
    }


def synthetic_validation_passes(summary: object) -> bool:
    if type(summary) is not dict or type(summary.get("cases")) is not dict:
        return False
    cases = summary["cases"]
    assert isinstance(cases, dict)
    required = {
        "informative",
        "biased_informative",
        "uninformative",
        "negative_pathological",
    }
    if set(cases) != required:
        return False
    for value in cases.values():
        if type(value) is not dict:
            return False
        replications = value.get("replications")
        nominal = value.get("nominal_coverage")
        mcse = value.get("monte_carlo_standard_error")
        observed = value.get("observed_coverage")
        if (
            type(replications) is not int
            or type(nominal) is not float
            or type(mcse) is not float
            or type(observed) is not dict
        ):
            return False
        tolerance = 4.0 * mcse
        for method in ("reference_only", "ppi", "ppi_plus"):
            actual = observed.get(method)
            if type(actual) is not float or abs(actual - nominal) > tolerance:
                return False

    informative = cases["informative"]
    biased = cases["biased_informative"]
    uninformative = cases["uninformative"]
    pathological = cases["negative_pathological"]
    assert isinstance(informative, dict) and isinstance(biased, dict)
    assert isinstance(uninformative, dict) and isinstance(pathological, dict)
    if float(informative["mean_ppi_plus_lambda"]) <= 0.5:
        return False
    if float(biased["mean_ppi_plus_lambda"]) <= 0.5:
        return False
    if float(uninformative["mean_ppi_plus_lambda"]) >= 0.25:
        return False
    if float(pathological["mean_ppi_plus_lambda"]) >= 0.05:
        return False

    informative_width = informative["mean_ci_width"]
    uninformative_width = uninformative["mean_ci_width"]
    biased_estimate = biased["mean_estimate"]
    assert isinstance(informative_width, dict)
    assert isinstance(uninformative_width, dict)
    assert isinstance(biased_estimate, dict)
    if float(informative_width["ppi_plus"]) >= float(
        informative_width["reference_only"]
    ):
        return False
    if float(uninformative_width["ppi"]) <= float(
        uninformative_width["reference_only"]
    ):
        return False
    if abs(float(biased_estimate["cheap_only"]) - _TRUE_MEAN) <= 0.5:
        return False
    if abs(float(biased_estimate["ppi"]) - _TRUE_MEAN) >= 0.10:
        return False
    if abs(float(biased_estimate["ppi_plus"]) - _TRUE_MEAN) >= 0.10:
        return False
    return True
