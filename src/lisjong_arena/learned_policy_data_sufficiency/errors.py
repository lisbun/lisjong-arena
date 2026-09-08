"""Purpose-specific Issue #190 errors."""


class DataSufficiencyError(ValueError):
    """The Issue #190 protocol or retained result is invalid."""


class DataSufficiencyEvidenceBlocked(DataSufficiencyError):
    """The exact retained #140 source evidence cannot be used."""


__all__ = ["DataSufficiencyError", "DataSufficiencyEvidenceBlocked"]
