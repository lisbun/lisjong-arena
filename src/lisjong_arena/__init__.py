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
from lisjong_arena.single_round_artifact import (
    SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
    CumulativeSingleRoundStrength,
    SingleRoundArtifactError,
    SingleRoundArtifactPlan,
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    merge_single_round_artifacts,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    GAME_MODE as SINGLE_ROUND_GAME_MODE,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT as SINGLE_ROUND_ROTATION_COUNT,
)
from lisjong_arena.single_round_evaluation import (
    SingleRoundEvaluationError,
    SingleRoundStrengthSummary,
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
    summarize_single_round_strength,
)

__version__ = "0.1.0"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "COMPARISON_PROTOCOL",
    "ArtifactPlan",
    "ComparisonArtifact",
    "ComparisonArtifactError",
    "CumulativeSingleRoundStrength",
    "ROTATION_COUNT",
    "SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION",
    "SINGLE_ROUND_EVALUATION_PROTOCOL",
    "SINGLE_ROUND_GAME_MODE",
    "SINGLE_ROUND_ROTATION_COUNT",
    "ComparisonExecutionError",
    "ComparisonPlan",
    "ComparisonResult",
    "PolicyFactoryNotSerializableError",
    "PolicyMetrics",
    "PolicySpec",
    "SeatResult",
    "SingleRoundArtifactError",
    "SingleRoundArtifactPlan",
    "SingleRoundCandidateMetrics",
    "SingleRoundEvaluationError",
    "SingleRoundEvaluationPlan",
    "SingleRoundEvaluationResult",
    "SingleRoundExecutionProvenance",
    "SingleRoundGameResult",
    "SingleRoundStrengthArtifact",
    "SingleRoundStrengthSummary",
    "ExecutionProvenance",
    "load_comparison_artifact",
    "load_single_round_artifact",
    "merge_single_round_artifacts",
    "run_comparison",
    "run_comparison_parallel",
    "run_single_round_evaluation",
    "run_single_round_evaluation_parallel",
    "save_comparison_artifact",
    "save_single_round_artifact",
    "summarize_single_round_strength",
]
