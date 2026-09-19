"""Arena #222 classical public-information structural-wait diagnostic."""

from .data import (
    ClassicalExample,
    coverage_value,
    eligible_cells,
    feature_vector,
    load_retained_records,
)
from .evaluation import (
    assemble_result,
    baseline_log_loss,
    classify_interval,
    evaluate_classical,
    fit_train_prevalence,
    paired_comparison,
    validate_result,
)
from .model import (
    fit_offset_logistic,
    predict_probability,
    validate_model,
)
from .protocol import (
    CLASSICAL_INCONCLUSIVE,
    CLASSICAL_REGRESSION,
    CLASSICAL_SIGNAL,
    FEATURE_NAMES,
    FEATURE_SEMANTICS_ID,
    STOP_INVALID,
    ClassicalWaitError,
)

__all__ = [
    "CLASSICAL_INCONCLUSIVE",
    "CLASSICAL_REGRESSION",
    "CLASSICAL_SIGNAL",
    "ClassicalExample",
    "ClassicalWaitError",
    "FEATURE_NAMES",
    "FEATURE_SEMANTICS_ID",
    "STOP_INVALID",
    "assemble_result",
    "baseline_log_loss",
    "classify_interval",
    "coverage_value",
    "eligible_cells",
    "evaluate_classical",
    "feature_vector",
    "fit_offset_logistic",
    "fit_train_prevalence",
    "load_retained_records",
    "paired_comparison",
    "predict_probability",
    "validate_model",
    "validate_result",
]
