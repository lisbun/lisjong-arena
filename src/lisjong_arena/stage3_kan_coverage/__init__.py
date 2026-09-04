"""Arena #146 kan coverage-source qualification pilot.

Arena #131 (`ENTRY GATE REFORMULATE`) のsuccessorとして、`lisjong #151` / PR #152で
追加された`KanCoverageYakuhaiCallPolicy`をHandBelief training coverage sourceとして
qualificationするためのbounded development pilotである。

```text
legal kan opportunity
    -> Policy-selected kan
    -> confirmed kan / explicitly-accounted non-confirm path
    -> rinshan / physical accounting
    -> HandBelief raw corpus / dataset
```

このpilotはstrength evaluationではなく、final training populationの選定でもない。
positive outcomeは次のpopulation-mix designへ進む根拠になるだけである。
#131のhistorical protocol / seeds / artifact identity / validatorsは変更しない。
"""

from .population import (
    KanCoveragePopulationError,
    KanCoveragePopulationPlan,
    kan_coverage_population_plan,
)
from .protocol import (
    ORDERED_SEEDS,
    OUTCOMES,
    PILOT_HANCHAN,
    PILOT_ROLE,
    POLICY_IDENTITY,
    POLICY_IMPORT_REFERENCE,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

__all__ = [
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PILOT_HANCHAN",
    "PILOT_ROLE",
    "POLICY_IDENTITY",
    "POLICY_IMPORT_REFERENCE",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "KanCoveragePopulationError",
    "KanCoveragePopulationPlan",
    "kan_coverage_population_plan",
]
