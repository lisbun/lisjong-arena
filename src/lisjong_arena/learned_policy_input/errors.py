"""Fail-closed errors for the experiment-local Learned Policy input schema."""


class PolicyInputFeatureError(Exception):
    """Base error for the Arena-owned PolicyInput feature contract."""


class UnsupportedFeatureSemanticsError(PolicyInputFeatureError):
    """The requested semantic feature version is not implemented."""


class UnsupportedTensorSchemaVersionError(PolicyInputFeatureError):
    """The requested tensor schema version is not implemented."""


class PolicyInputFeatureValidationError(PolicyInputFeatureError):
    """A PolicyInput cannot be represented without changing its meaning."""


class FeatureDimensionError(PolicyInputFeatureError):
    """The emitted tensor values do not match the locked schema dimension."""
