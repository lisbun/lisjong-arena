"""Locked protocol for Arena #222's classical public-state wait baseline."""

import hashlib
import json
import math

from lisjong_arena.phase11_public_riichi_wait_readout.protocol import (
    ELIGIBILITY_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    PHASE150_EXECUTION_LOCK_IDENTITY,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    TARGET_SEMANTICS_ID,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

SCHEMA = "phase11-classical-wait-baseline-v1"
ROLE = "PUBLIC_INFORMATION_CLASSICAL_STRUCTURAL_WAIT_BASELINE"
TILE_KIND_COUNT = 34

FEATURE_NAMES = (
    "candidate_unseen_fraction",
    "penchan_possible",
    "kanchan_possible",
    "ryanmen_low_possible",
    "ryanmen_high_possible",
    "penchan_support",
    "kanchan_support",
    "ryanmen_low_support",
    "ryanmen_high_support",
    "candidate_seen_in_opponent_river",
    "ryanmen_low_counterpart_seen_in_opponent_river",
    "ryanmen_high_counterpart_seen_in_opponent_river",
    "riichi_declaration_turn_normalized",
)
FEATURE_DIM = len(FEATURE_NAMES)
FEATURE_SEMANTICS_ID = "phase11-classical-public-state-features-v1"
LOCAL_SUPPORT_FORMULA = "min(unseen(required_a), unseen(required_b)) / 4"
RIICHI_TIMING_FORMULA = "riichi_junme / 18"

SOLVER = {
    "family": "torch.optim.LBFGS full-batch logistic correction",
    "dtype": "float64",
    "initialization": "all-zero correction weights",
    "free_intercept": False,
    "learning_rate": 1.0,
    "max_iterations": 100,
    "max_evaluations": 125,
    "tolerance_grad": 1e-9,
    "tolerance_change": 1e-12,
    "line_search": "strong_wolfe",
    "l2_regularization": 0.0,
    "training_seed": None,
    "feature_scaling": "none; all v1 features have fixed mathematical normalization",
}

BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 148
BOOTSTRAP_ORDER_INDICES = (249, 9750)
SELECTION_EXPOSURE_BEFORE = 4
SELECTION_EXPOSURE_AFTER = 5
FORMAL_TEST = False

EXPECTED_VALIDATION_HANCHAN = 16
EXPECTED_VALIDATION_ROWS = 2_447
EXPECTED_VALIDATION_CELLS = 83_198
EXPECTED_VALIDATION_UNAVAILABLE_ROWS = 0
EXPECTED_VALIDATION_ALL_ZERO_ROWS = 0
EXPECTED_BASELINE_LOG_LOSS = 0.16936299644382038
BASELINE_TOLERANCE = 1e-12

STOP_INVALID = "STOP / INVALID"
CLASSICAL_SIGNAL = "CLASSICAL BASELINE SIGNAL"
CLASSICAL_REGRESSION = "CLASSICAL BASELINE REGRESSION"
CLASSICAL_INCONCLUSIVE = "CLASSICAL BASELINE INCONCLUSIVE"
OUTCOMES = (
    STOP_INVALID,
    CLASSICAL_SIGNAL,
    CLASSICAL_REGRESSION,
    CLASSICAL_INCONCLUSIVE,
)


class ClassicalWaitError(ValueError):
    """A violation of the predeclared Arena #222 contract."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def identity(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def exact(actual: object, expected: object, name: str) -> None:
    if actual != expected:
        raise ClassicalWaitError(f"{name} differs from the locked contract")


def digest(value: object, name: str, length: int = 64) -> str:
    if type(value) is not str or len(value) != length:
        raise ClassicalWaitError(f"{name} must be a {length}-character string")
    return value


def retained_value() -> dict[str, object]:
    return {
        "source_issue": "lisbun/lisjong-arena#150",
        "semantic_reference_issue": "lisbun/lisjong-arena#172",
        "phase150_execution_lock_identity": PHASE150_EXECUTION_LOCK_IDENTITY,
        "population_identity": RETAINED_POPULATION_IDENTITY,
        "raw_corpus_identity": RETAINED_RAW_CORPUS_IDENTITY,
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "formal_test": False,
        "label_semantics_id": LABEL_SEMANTICS_ID,
        "eligibility_semantics_id": ELIGIBILITY_SEMANTICS_ID,
        "target_semantics_id": TARGET_SEMANTICS_ID,
        "regenerated": False,
    }


def feature_value() -> dict[str, object]:
    return {
        "semantics_id": FEATURE_SEMANTICS_ID,
        "names": list(FEATURE_NAMES),
        "dimension": FEATURE_DIM,
        "candidate_unseen": "remaining_tile_counts[candidate] / 4",
        "local_support_formula": LOCAL_SUPPORT_FORMULA,
        "riichi_timing_formula": RIICHI_TIMING_FORMULA,
        "static_tile_class_terms_in_primary_model": False,
        "empirical_normalization": False,
        "hidden_or_future_information": False,
        "danger_weights_reused": False,
        "river_or_suji_hard_zero": False,
    }


def solver_value() -> dict[str, object]:
    return dict(SOLVER)


def evaluation_value() -> dict[str, object]:
    return {
        "primary_metric": "mean binary log loss over eligible VALIDATION target-tile cells",
        "delta": "logloss(prevalence) - logloss(classical)",
        "paired_unit": "whole VALIDATION hanchan",
        "bootstrap": {
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "percentiles": [2.5, 97.5],
            "order_statistic_indices": list(BOOTSTRAP_ORDER_INDICES),
        },
        "classification": {
            "lower > 0": CLASSICAL_SIGNAL,
            "upper < 0": CLASSICAL_REGRESSION,
            "otherwise": CLASSICAL_INCONCLUSIVE,
        },
        "formal_test": FORMAL_TEST,
        "selection_exposure_before": SELECTION_EXPOSURE_BEFORE,
        "selection_exposure_after": SELECTION_EXPOSURE_AFTER,
        "expected_validation": {
            "hanchan": EXPECTED_VALIDATION_HANCHAN,
            "eligible_rows": EXPECTED_VALIDATION_ROWS,
            "eligible_cells": EXPECTED_VALIDATION_CELLS,
            "unavailable_rows": EXPECTED_VALIDATION_UNAVAILABLE_ROWS,
            "all_zero_rows": EXPECTED_VALIDATION_ALL_ZERO_ROWS,
        },
        "baseline_reference_log_loss": EXPECTED_BASELINE_LOG_LOSS,
        "baseline_tolerance": BASELINE_TOLERANCE,
        "historical_e160_context": {
            "outcome": "CLEAR READOUT REGRESSION",
            "baseline_log_loss": EXPECTED_BASELINE_LOG_LOSS,
            "readout_log_loss": 0.17610778973612368,
            "delta": -0.006744793292303292,
            "interval": [-0.00877228761131818, -0.00487814201622519],
            "role": "historical context only",
        },
    }


def validate_probability(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
        raise ClassicalWaitError(f"{name} must be finite and strictly between 0 and 1")
    return float(value)


__all__ = [
    "BASELINE_TOLERANCE",
    "BOOTSTRAP_ORDER_INDICES",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "CLASSICAL_INCONCLUSIVE",
    "CLASSICAL_REGRESSION",
    "CLASSICAL_SIGNAL",
    "ClassicalWaitError",
    "EXPECTED_BASELINE_LOG_LOSS",
    "EXPECTED_VALIDATION_ALL_ZERO_ROWS",
    "EXPECTED_VALIDATION_CELLS",
    "EXPECTED_VALIDATION_HANCHAN",
    "EXPECTED_VALIDATION_ROWS",
    "EXPECTED_VALIDATION_UNAVAILABLE_ROWS",
    "FEATURE_DIM",
    "FEATURE_NAMES",
    "FEATURE_SEMANTICS_ID",
    "FORMAL_TEST",
    "OUTCOMES",
    "ROLE",
    "SCHEMA",
    "SELECTION_EXPOSURE_AFTER",
    "SELECTION_EXPOSURE_BEFORE",
    "SOLVER",
    "STOP_INVALID",
    "TILE_KIND_COUNT",
    "canonical_json_bytes",
    "digest",
    "evaluation_value",
    "exact",
    "feature_value",
    "identity",
    "retained_value",
    "solver_value",
    "validate_probability",
]
