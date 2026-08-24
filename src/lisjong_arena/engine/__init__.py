"""first-party `lisjong-engine`上でlisjong Policyを実行するArena-owned bridge。

```text
lisjong-engine
    |
    | SeatObservation
    | ActionDescriptor[]
    v
Arena first-party bridge
    |
    v
lisjong DecisionContext
    |
    v
Policy / execute_policy()
    |
    v
InternalAction
    |
    v
Arena decision-local mapping
    |
    v
original ActionDescriptor
    |
    v
lisjong-engine
```

`lisjong.policy_contract`が所有するPolicy契約のsemanticsは変更しない。
RiichiEnv integrationとは独立したexecution pathであり、共通のbackend
abstractionへは統合しない。RiichiEnv固有のmaterialized state、synthetic
decision identity、event lag補正等もこのpathへ持ち込まない。
"""

from lisjong_arena.engine.action_mapping import (
    EngineActionMapping,
    build_action_mapping,
    internal_action_from_descriptor,
)
from lisjong_arena.engine.decision import EngineDecision, build_decision
from lisjong_arena.engine.domain_conversion import (
    meld_kind_from_engine_meld_type,
    public_meld_from_engine_meld,
    riichi_state_from_engine_status,
    seat_from_engine_seat,
    tile_from_public_tile,
    tiles_from_public_tiles,
    wind_from_engine_wind,
)
from lisjong_arena.engine.errors import (
    AmbiguousActionMappingError,
    EngineBridgeError,
    KakanProvenanceError,
    ObservationProjectionError,
    SeatIdentityError,
    UnmappedActionError,
    UnsupportedEngineValueError,
)
from lisjong_arena.engine.hanchan import run_policy_hanchan
from lisjong_arena.engine.policy_input import build_policy_input
from lisjong_arena.engine.policy_selector import (
    PolicySeatSelector,
    build_seat_selectors,
)

__all__ = [
    "AmbiguousActionMappingError",
    "EngineActionMapping",
    "EngineBridgeError",
    "EngineDecision",
    "KakanProvenanceError",
    "ObservationProjectionError",
    "PolicySeatSelector",
    "SeatIdentityError",
    "UnmappedActionError",
    "UnsupportedEngineValueError",
    "build_action_mapping",
    "build_decision",
    "build_policy_input",
    "build_seat_selectors",
    "internal_action_from_descriptor",
    "meld_kind_from_engine_meld_type",
    "public_meld_from_engine_meld",
    "riichi_state_from_engine_status",
    "run_policy_hanchan",
    "seat_from_engine_seat",
    "tile_from_public_tile",
    "tiles_from_public_tiles",
    "wind_from_engine_wind",
]
