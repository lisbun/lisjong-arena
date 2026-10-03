"""Errors raised by the teacher-selectable policy source record."""


class PolicySourceRecordError(ValueError):
    """A policy source record, population or teacher binding is invalid."""


__all__ = ["PolicySourceRecordError"]
