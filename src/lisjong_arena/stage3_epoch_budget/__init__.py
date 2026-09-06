"""Arena #157 bounded epoch-budget adequacy study。

Phase 10 (#150 / `PHASE10 SCALE SIGNAL`) はS32 / S64がlocked epoch budget上限
`40 / 40`に到達した状態で完了した。そのため

```text
data scale is useful
```

は支持されたが、

```text
more data is the next bottleneck
vs
current optimization budget is truncating learning
```

の区別がつかないまま残った。本childは **新規dataを一切生成せず**、#150の
retained S64 corpusだけを使って`max_epochs 40 -> 80`だけをexperimental axisと
し、`40 epoch capが実際にoptimizationを打ち切っていたか`をboundedに確認する。

```text
E40   #150 retained S64 artifact           max_epochs 40
E80   同一config / 同一corpus               max_epochs 80
```

E40は再trainingしない。`E80.loss_history[0:40] == #150 S64.loss_history`が
exactに成立することをhard gateとし、成立しない場合は`STOP / INVALID`として
Phase 10 training reproducibility defectへ回す。

exhaustive outcomeは`STOP / INVALID` / `BUDGET SUFFICIENT` / `BUDGET BOUND` /
`BUDGET MARGINAL`の4つだけである。`BUDGET BOUND`でも、fresh holdout上の
generalization improvementやformal superiorityは主張しない。同じVALIDATION 16
hanchanのselection exposureは#150と累積している。

このchildはPolicy strength comparisonでもarchitecture searchでもHPOでもなく、
Phase 11 head expansionでもない。#131 / #146 / #148 / #150のhistorical
protocol / seeds / artifact identity / validatorsは変更しない。

torchを必要とするtraining / evaluation orchestrationは`experiment` /
`artifact`側にあり、そこでもtorchはfunction-local importである。通常の
`import lisjong_arena`はtorchを要求しない。

generated weights、result、costはGit repositoryへcommitしない。
"""

from .protocol import (
    ARMS,
    BASELINE_ARM,
    BASELINE_MAX_EPOCHS,
    BOOTSTRAP,
    BUDGET_ARM,
    BUDGET_BOUND,
    BUDGET_MARGINAL,
    BUDGET_MAX_EPOCHS,
    BUDGET_SUFFICIENT,
    CLASSIFICATIONS,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    DECISION_RULE,
    DETERMINISM_PREFIX_EPOCHS,
    INCONCLUSIVE,
    INTERPRETATION_BOUNDARY,
    OUTCOMES,
    PATIENCE,
    PRIMARY_AXIS,
    ROLE,
    SCHEMA,
    SELECTION_EXPOSURE,
    STOP_INVALID,
    BudgetError,
    assert_bootstrap_constants_are_locked,
    assert_single_axis,
    baseline_training_lock,
    budget_training_lock,
)
from .retained import retained_value

__all__ = [
    "ARMS",
    "BASELINE_ARM",
    "BASELINE_MAX_EPOCHS",
    "BOOTSTRAP",
    "BUDGET_ARM",
    "BUDGET_BOUND",
    "BUDGET_MARGINAL",
    "BUDGET_MAX_EPOCHS",
    "BUDGET_SUFFICIENT",
    "CLASSIFICATIONS",
    "CLEAR_IMPROVEMENT",
    "CLEAR_REGRESSION",
    "DECISION_RULE",
    "DETERMINISM_PREFIX_EPOCHS",
    "INCONCLUSIVE",
    "INTERPRETATION_BOUNDARY",
    "OUTCOMES",
    "PATIENCE",
    "PRIMARY_AXIS",
    "ROLE",
    "SCHEMA",
    "SELECTION_EXPOSURE",
    "STOP_INVALID",
    "BudgetError",
    "assert_bootstrap_constants_are_locked",
    "assert_single_axis",
    "baseline_training_lock",
    "budget_training_lock",
    "retained_value",
]
