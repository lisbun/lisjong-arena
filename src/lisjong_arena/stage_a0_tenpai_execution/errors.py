"""Errors for the locked #262 Stage A0 execution."""


class StageA0ExecutionError(ValueError):
    """The locked Stage A0 execution cannot proceed safely."""


class StageA0PreflightError(StageA0ExecutionError):
    """The Phase E0 preflight failed."""


class StageA0CheckpointError(StageA0ExecutionError):
    """A trained checkpoint is missing, corrupt or contract-incompatible."""


class StageA0GateError(StageA0ExecutionError):
    """The locked Tenpai learnability gate cannot be evaluated safely."""


class StageA0DownstreamError(StageA0ExecutionError):
    """The bounded downstream execution/result is invalid."""
