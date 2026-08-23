"""lisjong-arena package.

lisjong Policyをconcrete environmentで実行・観測し、controlled /
reproducibleな条件で評価するArena。execution / observationとevaluationを
別責務として扱い、単一gameの進行は``lisjong_arena.riichienv.LocalGameRunner``
がArena-local canonical implementationとして担当する。
"""

from lisjong_arena._parallel_execution import PolicyFactoryNotSerializableError
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
    run_comparison_parallel,
)
from lisjong_arena.model import (
    ComparisonPlan,
    ComparisonResult,
    PolicyMetrics,
    PolicySpec,
    SeatResult,
    SingleRoundCandidateMetrics,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.single_round_evaluation import (
    GAME_MODE as SINGLE_ROUND_GAME_MODE,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT as SINGLE_ROUND_ROTATION_COUNT,
)
from lisjong_arena.single_round_evaluation import (
    SingleRoundEvaluationError,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)

__version__ = "0.1.0"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "COMPARISON_PROTOCOL",
    "ArtifactPlan",
    "ComparisonArtifact",
    "ComparisonArtifactError",
    "ROTATION_COUNT",
    "SINGLE_ROUND_GAME_MODE",
    "SINGLE_ROUND_ROTATION_COUNT",
    "ComparisonExecutionError",
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyFactoryNotSerializableError",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
    "SingleRoundCandidateMetrics",
    "SingleRoundEvaluationError",
    "SingleRoundEvaluationPlan",
    "SingleRoundEvaluationResult",
    "SingleRoundGameResult",
    "ExecutionProvenance",
    "load_comparison_artifact",
    "run_comparison",
    "run_comparison_parallel",
    "run_single_round_evaluation",
    "run_single_round_evaluation_parallel",
    "save_comparison_artifact",
]
