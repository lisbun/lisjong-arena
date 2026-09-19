"""Errors for the Issue #253 RiichiLab longitudinal diagnostic."""


class LongitudinalAnalysisError(ValueError):
    """An input or derived value violates the longitudinal contract."""


__all__ = ["LongitudinalAnalysisError"]
