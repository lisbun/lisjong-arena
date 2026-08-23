"""RiichiEnv 0.4.8とlisjong内部型を変換するAdapter境界。

`docs/architecture.md`の「RiichiEnv Adapter」責務のうち、lisbun/lisjong#28で
実装されたseat-visible materialized state同期と`PolicyInput`生成、
lisbun/lisjong#29の legal Action変換とdecision-local mapping、および
lisbun/lisjong#23の`DecisionContext`最終組み立てをArena-local canonical
implementationとして引き継ぐ。Policy呼び出しとLocal game runnerは対象外である。

`lisjong.policy_contract`とは異なり、このpackageは`riichienv`へ依存する。
`policy_contract` / `policies`側からの依存は逆流させない。
"""

from lisjong_arena.riichienv.adapter.action_mapping import (
    ActionAdapterError,
    ActorMismatchError,
    ContextResolutionError,
    EmptyLegalActionsError,
    RepresentativeSelectionError,
    RiichiEnvActionMapping,
    RiichiEnvActionMappingSession,
    StaleActionMappingError,
    UnmappedActionError,
    UnsupportedActionError,
)
from lisjong_arena.riichienv.adapter.decision import RiichiEnvDecision, build_decision
from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import (
    KyokuIdentity,
    SeatMaterializedState,
)
from lisjong_arena.riichienv.adapter.policy_input import build_policy_input
from lisjong_arena.riichienv.adapter.seat_conversion import seat_from_player_index
from lisjong_arena.riichienv.adapter.tile_conversion import (
    tile_from_mjai,
    tile_from_physical_id,
    tile_to_mjai,
)

__all__ = [
    "ActionAdapterError",
    "ActorMismatchError",
    "AdapterSyncError",
    "ContextResolutionError",
    "EmptyLegalActionsError",
    "KyokuIdentity",
    "RepresentativeSelectionError",
    "RiichiEnvActionMapping",
    "RiichiEnvActionMappingSession",
    "RiichiEnvDecision",
    "SeatMaterializedState",
    "StaleActionMappingError",
    "UnmappedActionError",
    "UnsupportedActionError",
    "build_decision",
    "build_policy_input",
    "seat_from_player_index",
    "tile_from_mjai",
    "tile_from_physical_id",
    "tile_to_mjai",
]
