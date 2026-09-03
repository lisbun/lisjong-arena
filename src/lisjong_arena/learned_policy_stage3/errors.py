"""Fail-closed errors for the Learned Policy Stage 3 serving boundary."""


class Stage3Error(Exception):
    """Base error for the Stage 3 serving-integration slice."""


class Stage3ProtocolError(Stage3Error):
    """A locked Stage 3 protocol value (seed population, mode, role) was violated."""


class Stage3ArtifactError(Stage3Error):
    """A serving checkpoint is missing, malformed, or not the locked contract."""


class Stage3ServingError(Stage3Error):
    """A serving-time precondition or safety contract failed."""


__all__ = [
    "Stage3ArtifactError",
    "Stage3Error",
    "Stage3ProtocolError",
    "Stage3ServingError",
]
