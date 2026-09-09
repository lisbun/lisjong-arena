"""Locked protocol for Arena #172's frozen-E160 wait readout experiment."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase8_sequential.model import S2_LATENT_DIM, S2_PARAMETER_COUNT
from lisjong_arena.phase8_sequential.protocol import SEQUENCE_SEMANTICS_ID
from lisjong_arena.stage3_epoch_budget.protocol import (
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

SCHEMA = "phase11-public-riichi-wait-readout-v1"
ROLE = "FROZEN_E160_PUBLIC_RIICHI_STRUCTURAL_WAIT_READOUT"

PHASE150_EXECUTION_LOCK_IDENTITY = (
    "a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3"
)
PHASE167_EXECUTION_LOCK_IDENTITY = (
    "54870c07a4d6c43a36796f4c07c3a3a94ea90c5da674fcd2861727f80efb4ac1"
)
PHASE167_RESULT_IDENTITY = (
    "bb7971fccbba2b7feb318dd0b616a6430914980fcc0077ae09d3b7b1d1a3f973"
)
E160_WEIGHTS_SHA256 = "1c4af0c553370fcfb4dbe53393d7681691d02ae9c23ed66dd5886669f948075a"
E160_SELECTED_EPOCH = 113
E160_EPOCHS_RUN = 119
E160_ARM = "E160"
E160_OUTCOME = "SATURATION OBSERVED WITHIN E160"

LISJONG_REVISION = "99a30c267a3c3e301e132c8799726eb10e012a95"
ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
E160_ARENA_REVISION = "56c7a8374e71fa19bf8ce0a31723f7100879daee"
RIICHIENV_VERSION = "0.4.8"
TORCH_VERSION = "2.13.0+cpu"

LABEL_SEMANTICS_ID = "exact-concealed-count-red-structural-wait-v1"
ELIGIBILITY_SEMANTICS_ID = "public-riichi-status-established-only-v1"
TARGET_SEMANTICS_ID = "structural-wait-34-base-kind-binary-v1"
SELF_ROLLOUT_SEMANTICS_ID = (
    "phase8-public-baseline-own-prior-expected-count-next-latent-v1"
)
OPPONENT_ORDER_SEMANTICS_ID = "phase8-explicit-opponent-winds-remap-v1"

READOUT_INPUT_DIM = S2_LATENT_DIM
READOUT_HIDDEN_DIM = 64
READOUT_ROWS = 3
TILE_KIND_COUNT = 34
READOUT_OUTPUT_DIM = READOUT_ROWS * TILE_KIND_COUNT

MAX_EPOCHS = 160
PATIENCE = 6
TRAINING_SEED = 0
DATALOADER_SEED = 0
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 0.0
WORKERS = 0
TORCH_THREADS = 1

BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 148
BOOTSTRAP_LOWER_PERCENTILE = 2.5
BOOTSTRAP_UPPER_PERCENTILE = 97.5
BOOTSTRAP_ORDER_INDICES = (249, 9750)
COVERAGE_MINIMUM_HANCHAN = 8
SELECTION_EXPOSURE = 4
FORMAL_TEST = False

STOP_INVALID = "STOP / INVALID"
INSUFFICIENT_COVERAGE = "INSUFFICIENT RIICHI COVERAGE"
CLEAR_SIGNAL = "CLEAR READOUT SIGNAL"
CLEAR_REGRESSION = "CLEAR READOUT REGRESSION"
INCONCLUSIVE = "INCONCLUSIVE"
OUTCOMES = (
    STOP_INVALID,
    INSUFFICIENT_COVERAGE,
    CLEAR_SIGNAL,
    CLEAR_REGRESSION,
    INCONCLUSIVE,
)
BUDGET_BOUND_DIAGNOSTIC = "READOUT BUDGET BOUND"


class Phase11Error(ValueError):
    """A violation of the predeclared Arena #172 contract."""


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
        raise Phase11Error(f"{name} differs from the locked contract")
    return actual


def digest(value: object, name: str, length: int = 64) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase11Error(f"{name} must be a lowercase hexadecimal digest")
    return value


def finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise Phase11Error(f"{name} must be finite")
    result = float(value)
    if minimum is not None and result < minimum:
        raise Phase11Error(f"{name} is below its minimum")
    return result


@dataclass(frozen=True, slots=True)
class ReadoutTrainingConfig:
    seed: int = TRAINING_SEED
    dataloader_seed: int = DATALOADER_SEED
    learning_rate: float = LEARNING_RATE
    weight_decay: float = WEIGHT_DECAY
    max_epochs: int = MAX_EPOCHS
    patience: int = PATIENCE
    workers: int = WORKERS
    deterministic_algorithms: bool = True
    torch_threads: int = TORCH_THREADS

    def __post_init__(self) -> None:
        if type(self.seed) is not int or type(self.dataloader_seed) is not int:
            raise TypeError("training seeds must be ints")
        if type(self.learning_rate) not in (int, float) or self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if type(self.weight_decay) not in (int, float) or self.weight_decay != 0:
            raise ValueError("weight_decay differs from the locked Phase 8 default")
        for name in ("max_epochs", "patience", "torch_threads"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive int")
        if type(self.workers) is not int or self.workers != 0:
            raise ValueError("readout training requires workers=0")
        if type(self.deterministic_algorithms) is not bool:
            raise TypeError("deterministic_algorithms must be bool")


FORMAL_TRAINING_CONFIG = ReadoutTrainingConfig()


def retained_value() -> dict[str, object]:
    return {
        "phase150_execution_lock_identity": PHASE150_EXECUTION_LOCK_IDENTITY,
        "population_identity": RETAINED_POPULATION_IDENTITY,
        "raw_corpus_identity": RETAINED_RAW_CORPUS_IDENTITY,
        "dataset_identity": RETAINED_DATASET_IDENTITY,
        "phase167_execution_lock_identity": PHASE167_EXECUTION_LOCK_IDENTITY,
        "phase167_result_identity": PHASE167_RESULT_IDENTITY,
        "e160_weights_sha256": E160_WEIGHTS_SHA256,
        "e160_selected_epoch": E160_SELECTED_EPOCH,
        "e160_epochs_run": E160_EPOCHS_RUN,
        "e160_arm": E160_ARM,
        "e160_outcome": E160_OUTCOME,
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "formal_test": FORMAL_TEST,
    }


def representation_value() -> dict[str, object]:
    return {
        "family": "previous-belief-gru-cell",
        "parameter_count": S2_PARAMETER_COUNT,
        "latent_dimension": S2_LATENT_DIM,
        "feature_semantics": FEATURE_SEMANTICS_ID,
        "sequence_semantics": SEQUENCE_SEMANTICS_ID,
        "self_rollout_semantics": SELF_ROLLOUT_SEMANTICS_ID,
        "opponent_order_semantics": OPPONENT_ORDER_SEMANTICS_ID,
        "latent_alignment": "same-step next_latent after recurrent update",
        "frozen_components": ["recurrent", "expected-count head"],
    }


def readout_value() -> dict[str, object]:
    return {
        "architecture": [READOUT_INPUT_DIM, READOUT_HIDDEN_DIM, READOUT_OUTPUT_DIM],
        "activation": "ReLU",
        "output": "logits reshaped as canonical 3 opponents x 34 base tile kinds",
        "trainable_components": ["readout head"],
    }


def training_value() -> dict[str, object]:
    return {
        "config": asdict(FORMAL_TRAINING_CONFIG),
        "optimizer": "Adam",
        "objective": "unweighted BCEWithLogits over eligible target-tile cells",
        "checkpoint_selection": "lowest VALIDATION binary log loss; earliest 1e-12 tie",
        "batch_semantics": "pooled eligible cells; one Adam update per epoch",
    }


def evaluation_value() -> dict[str, object]:
    return {
        "eligibility_semantics": ELIGIBILITY_SEMANTICS_ID,
        "label_semantics": LABEL_SEMANTICS_ID,
        "target_semantics": TARGET_SEMANTICS_ID,
        "unavailable_label_semantics": "masked out, never zero-filled",
        "baseline": {
            "fit_partition": "train",
            "pooling": "opponent rows",
            "smoothing": "Jeffreys",
            "formula": "(positive(tile) + 0.5) / (eligible_rows + 1.0)",
        },
        "primary_metric": "mean binary log loss over eligible target-tile cells",
        "secondary_metrics": [
            "Brier score",
            "per-hanchan log loss",
            "per-hanchan Brier score",
            "10-bin reliability",
            "per-tile support",
            "riichi junme subgroup",
            "seat subgroup",
            "open-closed subgroup",
        ],
        "paired_unit": "whole VALIDATION hanchan",
        "delta": "logloss(baseline) - logloss(readout)",
        "bootstrap": {
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "lower_percentile": BOOTSTRAP_LOWER_PERCENTILE,
            "upper_percentile": BOOTSTRAP_UPPER_PERCENTILE,
            "order_statistic_indices": list(BOOTSTRAP_ORDER_INDICES),
        },
        "coverage_minimum_validation_hanchan": COVERAGE_MINIMUM_HANCHAN,
        "classification": {
            "lower > 0": CLEAR_SIGNAL,
            "upper < 0": CLEAR_REGRESSION,
            "otherwise": INCONCLUSIVE,
        },
        "outcomes": list(OUTCOMES),
        "selection_exposure": SELECTION_EXPOSURE,
        "formal_test": FORMAL_TEST,
        "no_rescue": True,
        "no_e320": True,
    }


__all__ = [
    "BOOTSTRAP_ORDER_INDICES",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "BUDGET_BOUND_DIAGNOSTIC",
    "CLEAR_REGRESSION",
    "CLEAR_SIGNAL",
    "COVERAGE_MINIMUM_HANCHAN",
    "DATALOADER_SEED",
    "E160_SELECTED_EPOCH",
    "E160_WEIGHTS_SHA256",
    "ELIGIBILITY_SEMANTICS_ID",
    "FORMAL_TEST",
    "FORMAL_TRAINING_CONFIG",
    "INCONCLUSIVE",
    "INSUFFICIENT_COVERAGE",
    "LABEL_SEMANTICS_ID",
    "MAX_EPOCHS",
    "OUTCOMES",
    "PATIENCE",
    "PHASE167_EXECUTION_LOCK_IDENTITY",
    "PHASE167_RESULT_IDENTITY",
    "Phase11Error",
    "READOUT_HIDDEN_DIM",
    "READOUT_INPUT_DIM",
    "READOUT_OUTPUT_DIM",
    "READOUT_ROWS",
    "ROLE",
    "SCHEMA",
    "SELECTION_EXPOSURE",
    "STOP_INVALID",
    "TARGET_SEMANTICS_ID",
    "TILE_KIND_COUNT",
    "ReadoutTrainingConfig",
    "canonical_json_bytes",
    "digest",
    "evaluation_value",
    "exact",
    "finite",
    "identity",
    "readout_value",
    "representation_value",
    "retained_value",
    "training_value",
]
