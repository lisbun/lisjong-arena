"""Arena #148 population-mix pilot.

Arena #131 (`ENTRY GATE REFORMULATE`) と Arena #146
(`KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN`) のsuccessorとして、

```text
yakuhai-call primary
+ bounded KanCoverageYakuhaiCallPolicy augmentation
```

というpopulation constructionをdevelopment-only pilotで比較し、Stage 3 /
Phase 10へ渡すfirst-party training population **recipe** をlockできるかを
判断する。

```text
A   augmentation  0.0%   yakuhai-call x4
B   augmentation 12.5%   12 / 96 seat slots
C   augmentation 25.0%   24 / 96 seat slots
```

このpilotはPolicy strength comparisonでもKanCoverage Policy adoptionでも
architecture searchでもPhase 10 large-scale generationでもない。#131 / #146の
historical protocol / seeds / artifact identity / validatorsは変更しない。

torchを必要とするtraining / evaluation orchestrationは`experiment` /
`artifact`側にあり、そこでもtorchはfunction-local importである。通常の
`import lisjong_arena`はtorchを要求しない。
"""

from .population import (
    MixArmPlan,
    MixPopulationError,
    coverage_seat_index,
    mix_arm_plan,
    mix_arm_plans,
)
from .protocol import (
    ARM_IDS,
    AUGMENTATION_IDENTITY,
    AUGMENTATION_REFERENCE,
    AUGMENTATION_SLOTS_BY_ARM,
    ORDERED_SEEDS,
    OUTCOMES,
    PILOT_HANCHAN_PER_ARM,
    PILOT_ROLE,
    PRIMARY_IDENTITY,
    PRIMARY_REFERENCE,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

__all__ = [
    "ARM_IDS",
    "AUGMENTATION_IDENTITY",
    "AUGMENTATION_REFERENCE",
    "AUGMENTATION_SLOTS_BY_ARM",
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PILOT_HANCHAN_PER_ARM",
    "PILOT_ROLE",
    "PRIMARY_IDENTITY",
    "PRIMARY_REFERENCE",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "MixArmPlan",
    "MixPopulationError",
    "coverage_seat_index",
    "mix_arm_plan",
    "mix_arm_plans",
]
