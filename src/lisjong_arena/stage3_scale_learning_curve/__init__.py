"""Arena #150 Phase 10 bounded scale learning curve。

Arena #148 (`MIX LOCKED — 12.5% AUGMENTATION`) のsuccessorとして、locked
first-party population recipeとselected sequential S2 familyを維持したまま

```text
TRAIN hanchan   16 -> 32 -> 64
```

だけを変え、fresh fixed VALIDATION 16 hanchan上のlearning curveを測る。

```text
S16   360..375    16 hanchan
S32   360..391    32 hanchan
S64   360..423    64 hanchan
VAL   424..439    16 hanchan (shared / fixed)
```

3 scaleは1つのlocked 80-hanchan corpusと1つのdatasetを共有するnested TRAIN
subsetであり、subset membershipはseedだけから決まる。label / metric / resultに
よるselectionは持たない。formal TESTは存在しない。

このchildはPolicy strength comparisonでもarchitecture searchでもHPOでもない。
#131 / #146 / #148のhistorical protocol / seeds / artifact identity /
validatorsは変更しない。

torchを必要とするtraining / evaluation orchestrationは`experiment` /
`artifact`側にあり、そこでもtorchはfunction-local importである。通常の
`import lisjong_arena`はtorchを要求しない。

generated data、dataset、weights、resultはGit repositoryへcommitしない。
"""

from .population import (
    coverage_seat_index,
    occupancy,
    plan_value,
    population_identity,
    recipe_value,
)
from .protocol import (
    BOOTSTRAP,
    CURVE,
    DECISION_RULE,
    ORDERED_SEEDS,
    OUTCOMES,
    PRIMARY_CURVE_PAIR,
    ROLE,
    SCALES,
    SCHEMA,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    ScaleError,
    check_freshness,
    train_seeds,
    training_lock,
)

__all__ = [
    "BOOTSTRAP",
    "CURVE",
    "DECISION_RULE",
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PRIMARY_CURVE_PAIR",
    "ROLE",
    "SCALES",
    "SCHEMA",
    "SPLIT_POLICY",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "ScaleError",
    "check_freshness",
    "coverage_seat_index",
    "occupancy",
    "plan_value",
    "population_identity",
    "recipe_value",
    "train_seeds",
    "training_lock",
]
