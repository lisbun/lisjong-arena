"""Frozen-E160 public-riichi structural-wait readout experiment."""

from .coverage import build_coverage, load_coverage, validate_coverage
from .data import LatentExample, OpponentTarget, extract_frozen_latents
from .evaluation import (
    classify_interval,
    evaluate_readout,
    fit_train_prevalence,
    paired_comparison,
)
from .lock import current_receipt, require_current_lock, validate_lock
from .model import (
    assert_frozen_state_unchanged,
    create_readout_head,
    create_readout_optimizer,
    freeze_e160,
)
from .protocol import (
    FORMAL_TRAINING_CONFIG,
    OUTCOMES,
    Phase11Error,
    evaluation_value,
    readout_value,
    representation_value,
    retained_value,
    training_value,
)
from .result import assemble_result, validate_result
from .retained import load_retained
from .training import train_readout

__all__ = [
    "FORMAL_TRAINING_CONFIG",
    "LatentExample",
    "OUTCOMES",
    "OpponentTarget",
    "Phase11Error",
    "assemble_result",
    "assert_frozen_state_unchanged",
    "build_coverage",
    "classify_interval",
    "create_readout_head",
    "create_readout_optimizer",
    "current_receipt",
    "evaluate_readout",
    "evaluation_value",
    "extract_frozen_latents",
    "fit_train_prevalence",
    "freeze_e160",
    "load_coverage",
    "load_retained",
    "paired_comparison",
    "readout_value",
    "representation_value",
    "require_current_lock",
    "retained_value",
    "train_readout",
    "training_value",
    "validate_coverage",
    "validate_lock",
    "validate_result",
]
