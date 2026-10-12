"""Issue #455 aggregate-only deal-in-risk discard disagreement diagnostic."""

from .analysis import (
    COMPARATOR_CLASS_NAME,
    COMPARATOR_IDENTITY,
    LISJONG_REVISION,
    OUTCOME_BLOCKED,
    OUTCOME_COMPLETE,
    OUTCOME_INVALID,
    REGIONS,
    SCHEMA_VERSION,
    SHANTEN_BACKEND,
    DealInRiskDisagreementAnalyzer,
    classify_region,
    collect_ronned_discards,
    ronned_discard_keys,
    run_diagnostic,
)

__all__ = [
    "COMPARATOR_CLASS_NAME",
    "COMPARATOR_IDENTITY",
    "DealInRiskDisagreementAnalyzer",
    "LISJONG_REVISION",
    "OUTCOME_BLOCKED",
    "OUTCOME_COMPLETE",
    "OUTCOME_INVALID",
    "REGIONS",
    "SCHEMA_VERSION",
    "SHANTEN_BACKEND",
    "classify_region",
    "collect_ronned_discards",
    "ronned_discard_keys",
    "run_diagnostic",
]
