"""Fail-closed errors for the Offline Q vertical slice (Issue #140)."""


class OfflineQError(Exception):
    """Base error for the BC-vs-Offline-Q controlled comparison."""


class OfflineQProtocolError(OfflineQError):
    """A locked protocol value (seed, split, teacher, model config) was violated."""


class OfflineQRecordingError(OfflineQError):
    """One teacher decision cannot be recorded as a valid macro-transition source row."""


class OfflineQTransitionError(OfflineQError):
    """A macro-transition cannot be bound unambiguously from the recorded execution."""


class OfflineQSupportError(OfflineQError):
    """The TRAIN behavior-support gate was violated or could not be evaluated."""


class OfflineQArtifactError(OfflineQError):
    """An Offline Q artifact is missing, malformed, or internally inconsistent."""


class OfflineQDiagnosisError(OfflineQError):
    """A failure-diagnosis input binding, measurement, or result artifact is invalid."""


class OfflineQAmbiguousStateError(OfflineQDiagnosisError):
    """A retained row cannot be reconstructed unambiguously into a player-safe state."""


__all__ = [
    "OfflineQAmbiguousStateError",
    "OfflineQArtifactError",
    "OfflineQDiagnosisError",
    "OfflineQError",
    "OfflineQProtocolError",
    "OfflineQRecordingError",
    "OfflineQSupportError",
    "OfflineQTransitionError",
]
