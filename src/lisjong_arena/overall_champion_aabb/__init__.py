"""Issue #250 Overall Champion cross-family AABB half-game formal protocol v1.

ADR 0004 / ADR 0005のOverall Champion determinationを、Arena-ownedの
再現可能・fail-closed・strict-read可能なformal evaluation protocolとして
実装する。実行substrateは既存generic AABB comparisonのreuseであり、この
packageはOverall固有のparticipant binding、pre-execution lock、seed-block
統計、exhaustive classification、bundle verificationだけを所有する。

このpackageはformal executionを自動で開始しない。実際の400-hanchan
Overall eventはmerge後のfollow-up Issueでoperatorが実行する。
"""

from .experiment import OverallEvaluationOutcome, run_overall_evaluation
from .lock import (
    OverallChampionLockError,
    build_lock_document,
    load_lock_document,
    save_lock_document,
)
from .protocol import (
    GAME_MODE,
    HANCHAN_COUNT,
    HEURISTIC_SUPERIOR_LABEL,
    INCONCLUSIVE_LABEL,
    LEARNING_SUPERIOR_LABEL,
    PROTOCOL_ID,
    SEED_BLOCK_COUNT,
    STOP_INVALID_LABEL,
    OverallChampionProtocolError,
    ParticipantBinding,
)
from .result import (
    OverallChampionResultError,
    build_overall_result,
    load_overall_result,
    save_overall_result,
    verify_overall_bundle,
)
from .statistics import (
    OverallChampionStatisticsError,
    OverallSeedBlock,
    classify,
    derive_secondary_diagnostics,
    derive_seed_blocks,
    summarize_seed_blocks,
)

__all__ = [
    "GAME_MODE",
    "HANCHAN_COUNT",
    "HEURISTIC_SUPERIOR_LABEL",
    "INCONCLUSIVE_LABEL",
    "LEARNING_SUPERIOR_LABEL",
    "PROTOCOL_ID",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_LABEL",
    "OverallChampionLockError",
    "OverallChampionProtocolError",
    "OverallChampionResultError",
    "OverallChampionStatisticsError",
    "OverallEvaluationOutcome",
    "OverallSeedBlock",
    "ParticipantBinding",
    "build_lock_document",
    "build_overall_result",
    "classify",
    "derive_secondary_diagnostics",
    "derive_seed_blocks",
    "load_lock_document",
    "load_overall_result",
    "run_overall_evaluation",
    "save_lock_document",
    "save_overall_result",
    "summarize_seed_blocks",
    "verify_overall_bundle",
]
