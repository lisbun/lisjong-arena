"""Arena #291 frozen-E160 prevalence-offset linear probe."""

from .data import (
    centered_latent,
    centering_receipt,
    coverage_value,
    latent_reference,
    load_latent_records,
)
from .evaluation import (
    assemble_result,
    baseline_log_loss,
    classify_interval,
    evaluate_probe,
    fit_train_prevalence,
    paired_comparison,
)
from .model import (
    fit_probe,
    predict_probability,
    validate_model,
)
from .protocol import (
    E160_OFFSET_INCONCLUSIVE,
    E160_OFFSET_REGRESSION,
    E160_OFFSET_SIGNAL,
    E160OffsetProbeError,
)

__all__ = [
    "E160_OFFSET_INCONCLUSIVE",
    "E160_OFFSET_REGRESSION",
    "E160_OFFSET_SIGNAL",
    "E160OffsetProbeError",
    "assemble_result",
    "baseline_log_loss",
    "centered_latent",
    "centering_receipt",
    "classify_interval",
    "coverage_value",
    "evaluate_probe",
    "fit_probe",
    "fit_train_prevalence",
    "latent_reference",
    "load_latent_records",
    "paired_comparison",
    "predict_probability",
    "validate_model",
]
