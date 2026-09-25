"""Issue #375 Heuristic candidate vs Heuristic Champion AABB half-game protocol v1.

family-internal candidate評価用の独立protocol(``arena-heuristic-candidate-aabb-half-v1``)。
primary metricはuma/oka込みfinal scoreのseed-block mean deltaである。実行
substrateは既存generic AABB comparisonをreuseし、このpackageはprotocol固有の
participant binding、seed allocation binding、pre-execution lock、seed-block
統計、classification、bundle verificationだけを所有する。

このpackageはformal executionを自動で開始しない。
"""

from .experiment import CandidateEvaluationOutcome, run_candidate_evaluation
from .lock import (
    HeuristicCandidateLockError,
    build_lock_document,
    load_lock_document,
    save_lock_document,
)
from .protocol import (
    CANDIDATE_SUPERIOR_LABEL,
    GAME_MODE,
    HANCHAN_COUNT,
    INCONCLUSIVE_LABEL,
    INCUMBENT_SUPERIOR_LABEL,
    OKA,
    PROTOCOL_ID,
    RETURN_POINTS,
    SEED_BLOCK_COUNT,
    STOP_INVALID_LABEL,
    UMA,
    HeuristicCandidateProtocolError,
    final_score_units,
)
from .result import (
    HeuristicCandidateResultError,
    build_candidate_result,
    load_candidate_result,
    save_candidate_result,
    verify_candidate_bundle,
)
from .statistics import (
    CandidateSeedBlock,
    HeuristicCandidateStatisticsError,
    classify,
    derive_secondary_diagnostics,
    derive_seed_blocks,
    summarize_seed_blocks,
)

__all__ = [
    "CANDIDATE_SUPERIOR_LABEL",
    "GAME_MODE",
    "HANCHAN_COUNT",
    "INCONCLUSIVE_LABEL",
    "INCUMBENT_SUPERIOR_LABEL",
    "OKA",
    "PROTOCOL_ID",
    "RETURN_POINTS",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_LABEL",
    "UMA",
    "CandidateEvaluationOutcome",
    "CandidateSeedBlock",
    "HeuristicCandidateLockError",
    "HeuristicCandidateProtocolError",
    "HeuristicCandidateResultError",
    "HeuristicCandidateStatisticsError",
    "build_candidate_result",
    "build_lock_document",
    "classify",
    "derive_secondary_diagnostics",
    "derive_seed_blocks",
    "final_score_units",
    "load_candidate_result",
    "load_lock_document",
    "run_candidate_evaluation",
    "save_candidate_result",
    "save_lock_document",
    "summarize_seed_blocks",
    "verify_candidate_bundle",
]
