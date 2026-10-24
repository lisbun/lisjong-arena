"""lisjong-arena package.

lisjong Policyをconcrete environmentで実行・観測し、controlled /
reproducibleな条件で評価するArena。execution / observationとevaluationを
別責務として扱い、単一gameの進行は``lisjong_arena.riichienv.LocalGameRunner``
がArena-local canonical implementationとして担当する。

package importで行うのは``LISJONG_SHANTEN_BACKEND``の検査（#531）だけである。
下の再exportは最初にattributeを参照したときに読み込む（PEP 562、#537）。
そのため``python -m lisjong_arena.seed_registry``のようにlisjongを使わない
moduleはlisjong / Rust native moduleなしで動く。lisjongを読むmoduleの
Rust必須（lisjongのimport時検査）は変わらない。
"""

# #531: lisjong（#346以降）は``LISJONG_SHANTEN_BACKEND=python``をimport時に拒否する。
# どのentry pointでも、lisjongを読み込む前にArenaの移行案内付きで拒否する。
from importlib import import_module

from lisjong_arena._shanten_environment import require_execution_environment

require_execution_environment()

__version__ = "0.1.0"

_EXPORTS = {
    "ARTIFACT_SCHEMA_VERSION": ("lisjong_arena.artifact", "ARTIFACT_SCHEMA_VERSION"),
    "COMPARISON_PROTOCOL": ("lisjong_arena.artifact", "COMPARISON_PROTOCOL"),
    "ArtifactPlan": ("lisjong_arena.artifact", "ArtifactPlan"),
    "ComparisonArtifact": ("lisjong_arena.artifact", "ComparisonArtifact"),
    "ComparisonArtifactError": ("lisjong_arena.artifact", "ComparisonArtifactError"),
    "CumulativeSingleRoundStrength": (
        "lisjong_arena.single_round_artifact",
        "CumulativeSingleRoundStrength",
    ),
    "ROTATION_COUNT": ("lisjong_arena.comparison", "ROTATION_COUNT"),
    "SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION": (
        "lisjong_arena.single_round_artifact",
        "SINGLE_ROUND_ARTIFACT_SCHEMA_VERSION",
    ),
    "SINGLE_ROUND_EVALUATION_PROTOCOL": (
        "lisjong_arena.single_round_artifact",
        "SINGLE_ROUND_EVALUATION_PROTOCOL",
    ),
    "SINGLE_ROUND_GAME_MODE": ("lisjong_arena.single_round_evaluation", "GAME_MODE"),
    "SINGLE_ROUND_ROTATION_COUNT": (
        "lisjong_arena.single_round_evaluation",
        "ROTATION_COUNT",
    ),
    "ComparisonExecutionError": (
        "lisjong_arena.comparison",
        "ComparisonExecutionError",
    ),
    "ComparisonPlan": ("lisjong_arena.model", "ComparisonPlan"),
    "ComparisonResult": ("lisjong_arena.model", "ComparisonResult"),
    "DurableLocalGameRecord": (
        "lisjong_arena.durable_local_game_record",
        "DurableLocalGameRecord",
    ),
    "DurableLocalGameRecordError": (
        "lisjong_arena.durable_local_game_record",
        "DurableLocalGameRecordError",
    ),
    "DEFAULT_LOCAL_GAME_RECORD_SCHEMA_VERSION": (
        "lisjong_arena.durable_local_game_record",
        "DEFAULT_LOCAL_GAME_RECORD_SCHEMA_VERSION",
    ),
    "LOCAL_GAME_RECORD_SCHEMA_ID": (
        "lisjong_arena.durable_local_game_record",
        "LOCAL_GAME_RECORD_SCHEMA_ID",
    ),
    "LOCAL_GAME_RECORD_SCHEMA_VERSION": (
        "lisjong_arena.durable_local_game_record",
        "LOCAL_GAME_RECORD_SCHEMA_VERSION",
    ),
    "LocalGameRecordSummary": (
        "lisjong_arena.durable_local_game_record",
        "LocalGameRecordSummary",
    ),
    "PolicyFactoryNotSerializableError": (
        "lisjong_arena._parallel_execution",
        "PolicyFactoryNotSerializableError",
    ),
    "PolicyMetrics": ("lisjong_arena.model", "PolicyMetrics"),
    "PolicySpec": ("lisjong_arena.model", "PolicySpec"),
    "RoundDrawFact": ("lisjong_arena.riichienv.round_result", "RoundDrawFact"),
    "RoundResult": ("lisjong_arena.riichienv.round_result", "RoundResult"),
    "RoundWinFact": ("lisjong_arena.riichienv.round_result", "RoundWinFact"),
    "RoundWinScoring": ("lisjong_arena.riichienv.round_result", "RoundWinScoring"),
    "RoundYaku": ("lisjong_arena.riichienv.round_result", "RoundYaku"),
    "SeatResult": ("lisjong_arena.model", "SeatResult"),
    "SingleRoundArtifactError": (
        "lisjong_arena.single_round_artifact",
        "SingleRoundArtifactError",
    ),
    "SingleRoundArtifactPlan": (
        "lisjong_arena.single_round_artifact",
        "SingleRoundArtifactPlan",
    ),
    "SingleRoundCandidateMetrics": (
        "lisjong_arena.model",
        "SingleRoundCandidateMetrics",
    ),
    "SingleRoundEvaluationError": (
        "lisjong_arena.single_round_evaluation",
        "SingleRoundEvaluationError",
    ),
    "SingleRoundEvaluationPlan": ("lisjong_arena.model", "SingleRoundEvaluationPlan"),
    "SingleRoundEvaluationResult": (
        "lisjong_arena.model",
        "SingleRoundEvaluationResult",
    ),
    "SingleRoundExecutionProvenance": (
        "lisjong_arena.single_round_artifact",
        "SingleRoundExecutionProvenance",
    ),
    "SingleRoundGameResult": ("lisjong_arena.model", "SingleRoundGameResult"),
    "SingleRoundStrengthArtifact": (
        "lisjong_arena.single_round_artifact",
        "SingleRoundStrengthArtifact",
    ),
    "SingleRoundStrengthSummary": (
        "lisjong_arena.single_round_evaluation",
        "SingleRoundStrengthSummary",
    ),
    "ExecutionProvenance": ("lisjong_arena.artifact", "ExecutionProvenance"),
    "load_comparison_artifact": ("lisjong_arena.artifact", "load_comparison_artifact"),
    "load_local_game_record": (
        "lisjong_arena.durable_local_game_record",
        "load_local_game_record",
    ),
    "load_single_round_artifact": (
        "lisjong_arena.single_round_artifact",
        "load_single_round_artifact",
    ),
    "merge_single_round_artifacts": (
        "lisjong_arena.single_round_artifact",
        "merge_single_round_artifacts",
    ),
    "run_comparison": ("lisjong_arena.comparison", "run_comparison"),
    "run_comparison_parallel": ("lisjong_arena.comparison", "run_comparison_parallel"),
    "run_and_save_local_game_record": (
        "lisjong_arena.durable_local_game_record",
        "run_and_save_local_game_record",
    ),
    "run_single_round_evaluation": (
        "lisjong_arena.single_round_evaluation",
        "run_single_round_evaluation",
    ),
    "run_single_round_evaluation_parallel": (
        "lisjong_arena.single_round_evaluation",
        "run_single_round_evaluation_parallel",
    ),
    "save_comparison_artifact": ("lisjong_arena.artifact", "save_comparison_artifact"),
    "save_local_game_record": (
        "lisjong_arena.durable_local_game_record",
        "save_local_game_record",
    ),
    "save_single_round_artifact": (
        "lisjong_arena.single_round_artifact",
        "save_single_round_artifact",
    ),
    "summarize_single_round_strength": (
        "lisjong_arena.single_round_evaluation",
        "summarize_single_round_strength",
    ),
    "summarize_local_game_record": (
        "lisjong_arena.durable_local_game_record",
        "summarize_local_game_record",
    ),
}
"""再exportする名前 -> (module, attribute)。"""

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> object:
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
