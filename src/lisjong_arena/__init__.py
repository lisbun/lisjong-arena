"""lisjong-arena package.

複数のlisjong Policyを、fixed seed setとdeterministicなseat rotationのもとで
再現可能に比較するための最小Arena。単一gameの進行は``lisjong``へ委譲する。
"""

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
    "ROTATION_COUNT",
    "ComparisonExecutionError",
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
    "run_comparison",
]
