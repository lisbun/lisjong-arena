"""lisjong-arena package.

複数のlisjong Policyを、fixed seed setとdeterministicなseat rotationのもとで
再現可能に比較するための最小Arena。単一gameの進行は``lisjong``へ委譲する。
"""

from lisjong_arena.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    COMPARISON_PROTOCOL,
    ArtifactPlan,
    ComparisonArtifact,
    ComparisonArtifactError,
    ExecutionProvenance,
    load_comparison_artifact,
    save_comparison_artifact,
)
from lisjong_arena.comparison import (
    ROTATION_COUNT,
    ComparisonExecutionError,
    run_comparison,
)
from lisjong_arena.model import (
    ComparisonPlan,
    ComparisonResult,
    PolicyMetrics,
    PolicySpec,
    SeatResult,
)

__version__ = "0.1.0"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "COMPARISON_PROTOCOL",
    "ArtifactPlan",
    "ComparisonArtifact",
    "ComparisonArtifactError",
    "ROTATION_COUNT",
    "ComparisonExecutionError",
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
    "ExecutionProvenance",
    "load_comparison_artifact",
    "run_comparison",
    "save_comparison_artifact",
]
