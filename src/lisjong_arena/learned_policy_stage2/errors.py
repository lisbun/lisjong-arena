"""Fail-closed errors for the Learned Policy Stage 2 vertical slice."""


class Stage2Error(Exception):
    """Base error for the Stage 2 behavior-cloning experiment."""


class Stage2ContractIdentityError(Stage2Error):
    """An installed feature or action contract is not the locked Stage 2 one."""


class Stage2ProtocolError(Stage2Error):
    """A locked protocol value (seed, split, teacher, model config) was violated."""


class Stage2RecordingError(Stage2Error):
    """One teacher decision cannot be recorded as a valid Stage 2 row."""


class Stage2ArtifactError(Stage2Error):
    """A Stage 2 artifact is missing, malformed, or internally inconsistent."""


class Stage2EvaluationError(Stage2Error):
    """A Stage 2 evaluation precondition or safety check failed."""


__all__ = [
    "Stage2ArtifactError",
    "Stage2ContractIdentityError",
    "Stage2Error",
    "Stage2EvaluationError",
    "Stage2ProtocolError",
    "Stage2RecordingError",
]
