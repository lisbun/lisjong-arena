"""Locked protocol for Arena #291's frozen-E160 prevalence-offset probe."""

import hashlib
import json
import math

from lisjong_arena.phase11_public_riichi_wait_readout.protocol import (
    E160_WEIGHTS_SHA256,
    ELIGIBILITY_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    PHASE150_EXECUTION_LOCK_IDENTITY,
    PHASE167_EXECUTION_LOCK_IDENTITY,
    PHASE167_RESULT_IDENTITY,
    SELF_ROLLOUT_SEMANTICS_ID,
    TARGET_SEMANTICS_ID,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

SCHEMA = "phase11-e160-offset-probe-v1"
ROLE = "FROZEN_E160_PREVALENCE_OFFSET_LINEAR_PROBE"

LATENT_DIM = 128
OUTPUT_ROWS = 3
TILE_KIND_COUNT = 34
OUTPUT_DIM = OUTPUT_ROWS * TILE_KIND_COUNT
PARAMETER_COUNT = OUTPUT_DIM * LATENT_DIM
FROZEN_E160_STATE_DIGEST = (
    "581f4d20138291ea7c6b22508105b2ac2ed40cc3b3668e680376f3b9adf0885e"
)

PHASE222_RESULT_IDENTITY = (
    "df181e2def74a49aa4e43416ad64c996d035fcfd9b8d8ef0e2a21354edfdefd7"
)
PHASE222_OUTCOME = "CLASSICAL BASELINE SIGNAL"
PHASE222_LOG_LOSS = 0.15704296935453027
PHASE222_DELTA = 0.012320027089290114
PHASE222_INTERVAL = (0.009105953280646034, 0.014637951887321116)

PHASE172_OUTCOME = "CLEAR READOUT REGRESSION"
PHASE172_LOG_LOSS = 0.17610778973612368
PHASE172_DELTA = -0.006744793292303292
PHASE172_INTERVAL = (-0.00877228761131818, -0.00487814201622519)

EXPECTED_TRAIN_HANCHAN = 64
EXPECTED_TRAIN_ROWS = 11_102
EXPECTED_TRAIN_CELLS = 377_468
EXPECTED_TRAIN_UNAVAILABLE_ROWS = 0
EXPECTED_TRAIN_ALL_ZERO_ROWS = 0
EXPECTED_VALIDATION_HANCHAN = 16
EXPECTED_VALIDATION_ROWS = 2_447
EXPECTED_VALIDATION_CELLS = 83_198
EXPECTED_VALIDATION_UNAVAILABLE_ROWS = 0
EXPECTED_VALIDATION_ALL_ZERO_ROWS = 0
EXPECTED_BASELINE_LOG_LOSS = 0.16936299644382038
BASELINE_TOLERANCE = 1e-12

CENTERING_SEMANTICS_ID = "train-only-exact-output-row-latent-mean-v1"
LATENT_FINGERPRINT_SEMANTICS_ID = (
    "phase172-same-step-next-latent-metadata-float32-v1"
)

SOLVER = {
    "family": "torch.optim.LBFGS full-batch prevalence-offset linear probe",
    "dtype": "float64 detached probe features and parameters",
    "device": "cpu",
    "torch_threads": 1,
    "deterministic_algorithms": True,
    "objective": "unweighted binary log loss with frozen per-tile logit offset",
    "initialization": "all-zero correction weights",
    "free_intercept": False,
    "learning_rate": 1.0,
    "max_iterations": 100,
    "max_evaluations": 125,
    "history_size": 100,
    "tolerance_grad": 1e-9,
    "tolerance_change": 1e-12,
    "line_search": "strong_wolfe",
    "l2_regularization": 0.0,
    "training_seed": None,
    "feature_scaling": "none",
}

BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 148
BOOTSTRAP_ORDER_INDICES = (249, 9750)
SELECTION_EXPOSURE_BEFORE = 5
SELECTION_EXPOSURE_AFTER = 6
FORMAL_TEST = False

STOP_INVALID = "STOP / INVALID"
E160_OFFSET_SIGNAL = "E160 OFFSET SIGNAL"
E160_OFFSET_REGRESSION = "E160 OFFSET REGRESSION"
E160_OFFSET_INCONCLUSIVE = "E160 OFFSET INCONCLUSIVE"
OUTCOMES = (
    STOP_INVALID,
    E160_OFFSET_SIGNAL,
    E160_OFFSET_REGRESSION,
    E160_OFFSET_INCONCLUSIVE,
)


class E160OffsetProbeError(ValueError):
    """A violation of the predeclared Arena #291 contract."""


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


def exact(actual: object, expected: object, name: str) -> object:
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise E160OffsetProbeError(f"{name} differs from the locked contract")
    return actual


def digest(value: object, name: str, length: int = 64) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise E160OffsetProbeError(f"{name} must be a lowercase hexadecimal digest")
    return value


def finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise E160OffsetProbeError(f"{name} must be finite")
    result = float(value)
    if minimum is not None and result < minimum:
        raise E160OffsetProbeError(f"{name} is below its minimum")
    return result


def validate_probability(value: object, name: str) -> float:
    result = finite(value, name)
    if not 0.0 < result < 1.0:
        raise E160OffsetProbeError(f"{name} must be strictly between 0 and 1")
    return result


def retained_value() -> dict[str, object]:
    return {
        "phase150_execution_lock_identity": PHASE150_EXECUTION_LOCK_IDENTITY,
        "population_identity": RETAINED_POPULATION_IDENTITY,
        "raw_corpus_identity": RETAINED_RAW_CORPUS_IDENTITY,
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "phase167_execution_lock_identity": PHASE167_EXECUTION_LOCK_IDENTITY,
        "phase167_result_identity": PHASE167_RESULT_IDENTITY,
        "e160_weights_sha256": E160_WEIGHTS_SHA256,
        "frozen_e160_state_digest": FROZEN_E160_STATE_DIGEST,
        "self_rollout_semantics_id": SELF_ROLLOUT_SEMANTICS_ID,
        "eligibility_semantics_id": ELIGIBILITY_SEMANTICS_ID,
        "label_semantics_id": LABEL_SEMANTICS_ID,
        "target_semantics_id": TARGET_SEMANTICS_ID,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "formal_test": FORMAL_TEST,
        "phase222_result_identity_context_only": PHASE222_RESULT_IDENTITY,
    }


def representation_value() -> dict[str, object]:
    return {
        "latent_dimension": LATENT_DIM,
        "alignment": "exact #172 same-step next_latent after recurrent update",
        "opponent_row_mapping": "exact #172 explicit logical Wind remap",
        "noneligible_steps_advance_recurrent_state": True,
        "frozen_components": ["recurrent", "expected-count head"],
        "expected_frozen_state_digest": FROZEN_E160_STATE_DIGEST,
        "latent_fingerprint_semantics_id": LATENT_FINGERPRINT_SEMANTICS_ID,
    }


def centering_value() -> dict[str, object]:
    return {
        "semantics_id": CENTERING_SEMANTICS_ID,
        "fit_partition": "train",
        "vectors": OUTPUT_ROWS,
        "dimension": LATENT_DIM,
        "row_identity": "exact #172 post-remap output row index",
        "formula": "mean same-step next_latent over TRAIN eligible target rows",
        "validation_influence": False,
        "scaling": "none",
        "pca": False,
        "whitening": False,
    }


def probe_value() -> dict[str, object]:
    return {
        "architecture": "Linear(128, 102, bias=False) represented as [3,34,128]",
        "weight_shape": [OUTPUT_ROWS, TILE_KIND_COUNT, LATENT_DIM],
        "parameter_count": PARAMETER_COUNT,
        "free_intercept": False,
        "hidden_layers": [],
        "activation": None,
        "learned_embedding": False,
        "trunk_update": False,
        "classical_features_as_input": False,
    }


def solver_value() -> dict[str, object]:
    return dict(SOLVER)


def evaluation_value() -> dict[str, object]:
    return {
        "primary_metric": (
            "mean binary log loss over eligible VALIDATION target-tile cells"
        ),
        "delta": "logloss(prevalence) - logloss(E160 offset probe)",
        "paired_unit": "whole VALIDATION hanchan",
        "bootstrap": {
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "percentiles": [2.5, 97.5],
            "order_statistic_indices": list(BOOTSTRAP_ORDER_INDICES),
        },
        "classification": {
            "lower > 0": E160_OFFSET_SIGNAL,
            "upper < 0": E160_OFFSET_REGRESSION,
            "otherwise": E160_OFFSET_INCONCLUSIVE,
        },
        "outcomes": list(OUTCOMES),
        "selection_exposure_before": SELECTION_EXPOSURE_BEFORE,
        "selection_exposure_after": SELECTION_EXPOSURE_AFTER,
        "formal_test": FORMAL_TEST,
        "baseline_reference_log_loss": EXPECTED_BASELINE_LOG_LOSS,
        "baseline_tolerance": BASELINE_TOLERANCE,
        "context_only": {
            "phase222": {
                "result_identity": PHASE222_RESULT_IDENTITY,
                "outcome": PHASE222_OUTCOME,
                "log_loss": PHASE222_LOG_LOSS,
                "delta": PHASE222_DELTA,
                "interval": list(PHASE222_INTERVAL),
            },
            "phase172": {
                "outcome": PHASE172_OUTCOME,
                "log_loss": PHASE172_LOG_LOSS,
                "delta": PHASE172_DELTA,
                "interval": list(PHASE172_INTERVAL),
            },
        },
    }


__all__ = [
    "BASELINE_TOLERANCE",
    "BOOTSTRAP_ORDER_INDICES",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "CENTERING_SEMANTICS_ID",
    "E160_OFFSET_INCONCLUSIVE",
    "E160_OFFSET_REGRESSION",
    "E160_OFFSET_SIGNAL",
    "E160OffsetProbeError",
    "EXPECTED_BASELINE_LOG_LOSS",
    "EXPECTED_TRAIN_ALL_ZERO_ROWS",
    "EXPECTED_TRAIN_CELLS",
    "EXPECTED_TRAIN_HANCHAN",
    "EXPECTED_TRAIN_ROWS",
    "EXPECTED_TRAIN_UNAVAILABLE_ROWS",
    "EXPECTED_VALIDATION_ALL_ZERO_ROWS",
    "EXPECTED_VALIDATION_CELLS",
    "EXPECTED_VALIDATION_HANCHAN",
    "EXPECTED_VALIDATION_ROWS",
    "EXPECTED_VALIDATION_UNAVAILABLE_ROWS",
    "FORMAL_TEST",
    "FROZEN_E160_STATE_DIGEST",
    "LATENT_DIM",
    "LATENT_FINGERPRINT_SEMANTICS_ID",
    "OUTCOMES",
    "OUTPUT_DIM",
    "OUTPUT_ROWS",
    "PARAMETER_COUNT",
    "ROLE",
    "SCHEMA",
    "SELECTION_EXPOSURE_AFTER",
    "SELECTION_EXPOSURE_BEFORE",
    "SOLVER",
    "STOP_INVALID",
    "TILE_KIND_COUNT",
    "canonical_json_bytes",
    "centering_value",
    "digest",
    "evaluation_value",
    "exact",
    "finite",
    "identity",
    "probe_value",
    "representation_value",
    "retained_value",
    "solver_value",
    "validate_probability",
]
