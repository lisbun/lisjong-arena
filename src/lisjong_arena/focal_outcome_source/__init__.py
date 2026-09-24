"""L0.3 B focal outcome source producer（lisbun/lisjong-arena#359）。

lisjong-engine backendの別lineage（#370）は``engine_source``にある。
"""

from .accounting import (
    FocalOutcomeSourceError,
    GameAccount,
    KyokuAccount,
    account_events,
    account_game,
)
from .adapter import FocalAdapterError, FocalExplorationPolicy, FocalSelectionCapture
from .exploration_token import (
    EXPLORATION_TOKEN_IDENTITY,
    exploration_token,
    exploration_token_payload,
)
from .source import (
    BEHAVIOR,
    FOCAL_ROTATION_RULE,
    OUTCOME_SOURCE_KIND,
    OUTCOME_SOURCE_SCHEMA,
    PINNED_LISJONG_REVISION,
    FocalGameExecution,
    build_source_contract,
    focal_seat_for,
    generate_focal_outcome_source,
    run_focal_game,
    verify_focal_outcome_source,
)

__all__ = [
    "BEHAVIOR",
    "EXPLORATION_TOKEN_IDENTITY",
    "FOCAL_ROTATION_RULE",
    "OUTCOME_SOURCE_KIND",
    "OUTCOME_SOURCE_SCHEMA",
    "PINNED_LISJONG_REVISION",
    "FocalAdapterError",
    "FocalExplorationPolicy",
    "FocalGameExecution",
    "FocalOutcomeSourceError",
    "FocalSelectionCapture",
    "GameAccount",
    "KyokuAccount",
    "account_events",
    "account_game",
    "build_source_contract",
    "exploration_token",
    "exploration_token_payload",
    "focal_seat_for",
    "generate_focal_outcome_source",
    "run_focal_game",
    "verify_focal_outcome_source",
]
