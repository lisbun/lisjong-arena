"""Stage 3 Entry Gate development-only first-party population pilot.

Arena #131 / `lisjong-project#36`のStage 3 Entry Gateは、Phase 10へ渡すtraining
populationをevidence付きで選定するためのbounded development pilotである。formal
TESTではなく、Stage 2 formal holdout結果と統計的に累積しない。

torchを必要とするtraining / evaluation orchestrationは`experiment` /
`artifact`側にあり、そこでもtorchはfunction-local importである。通常の
`import lisjong_arena`はtorchを要求しない。
"""

from .population import (
    PILOT_HANCHAN_PER_POPULATION,
    PILOT_ROLE,
    GameSeatAssignment,
    PopulationPlan,
    SeatPolicyReference,
    Stage3PopulationError,
    plan_for_population_id,
    population_a_plan,
    population_b_plan,
    population_c_plan,
    stage3_population_plans,
)

__all__ = [
    "PILOT_HANCHAN_PER_POPULATION",
    "PILOT_ROLE",
    "GameSeatAssignment",
    "PopulationPlan",
    "SeatPolicyReference",
    "Stage3PopulationError",
    "plan_for_population_id",
    "population_a_plan",
    "population_b_plan",
    "population_c_plan",
    "stage3_population_plans",
]
