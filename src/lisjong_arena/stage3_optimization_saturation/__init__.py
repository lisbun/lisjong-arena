"""Arena #167 bounded optimization-budget saturation study。

Arena #157 (`BUDGET BOUND`) は`max_epochs 40 -> 80`だけを動かし、Phase 10の
40 epoch capが実際にoptimizationを打ち切っていたことをboundedに確認して
completedした。ただしE80自身も`80 / 80`を選んだため、

```text
80 epoch budget is enough
vs
the optimization trajectory is still truncated at 80
```

の区別がつかないまま残った。本childは **新規dataを一切生成せず**、#150の
retained S64 corpusと#157のretained E80 evidenceだけを使って
`max_epochs 80 -> 160`だけをexperimental axisとし、`optimization-budget
saturationが160 epoch以内に観測されるか`をboundedに確認する。

```text
E80    #157 retained E80 artifact             max_epochs 80
E160   同一config / 同一corpus                 max_epochs 160
```

E80は再trainingしない。E160はepoch 1から走らせ、E80 checkpointからresumeしない
（現在のtraining artifact contractがoptimizer state / RNG continuation stateを
lockしていないため）。`E160.loss_history[0:80] == #157 E80.loss_history`が
exactに成立することをhard gateとし、成立しない場合は`STOP / INVALID`として
training reproducibility defectへ回す。

exhaustive outcomeは`STOP / INVALID` / `E80 SUFFICIENT` /
`SATURATION OBSERVED WITHIN E160` / `E160 STILL BOUND` / `E160 BOUND / MARGINAL`
の5つだけである。どのoutcomeでも、fresh holdout上のgeneralization improvementや
formal superiorityは主張しない。同じVALIDATION 16 hanchanのselection exposureは
#150 / #157と累積しており、本childが3件目である。どのoutcomeでも同一Issue内で
320 epochへ自動extensionしない。

このchildはPolicy strength comparisonでもarchitecture searchでもHPOでもなく、
Phase 11 head expansionでもなく、#166 throughput optimizationの取り込みでも
ない。#131 / #146 / #148 / #150 / #157のhistorical protocol / seeds / artifact
identity / validatorsは変更しない。

torchを必要とするtraining / evaluation orchestrationは`experiment` /
`artifact`側にあり、そこでもtorchはfunction-local importである。通常の
`import lisjong_arena`はtorchを要求しない。

generated weights、result、costはGit repositoryへcommitしない。
"""

from .protocol import (
    ARMS,
    BASELINE_ARM,
    BASELINE_LOSS_HISTORY_DIGEST,
    BASELINE_MAX_EPOCHS,
    BASELINE_POOLED_MAE,
    BASELINE_SELECTED_EPOCH,
    BASELINE_SUFFICIENT,
    BASELINE_WEIGHTS_SHA256,
    BOOTSTRAP,
    BOUND_MARGINAL,
    CLASSIFICATIONS,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    DECISION_RULE,
    DETERMINISM_PREFIX_EPOCHS,
    HARD_CAP_EPOCHS,
    INCONCLUSIVE,
    INTERPRETATION_BOUNDARY,
    NO_EXTENSION_RULE,
    OUTCOMES,
    PATIENCE,
    PREDECESSOR_EXECUTION_LOCK_IDENTITY,
    PREDECESSOR_OUTCOME,
    PREDECESSOR_RESULT_IDENTITY,
    PRIMARY_AXIS,
    RETRY_RULE,
    ROLE,
    SATURATION_ARM,
    SATURATION_MAX_EPOCHS,
    SATURATION_OBSERVED,
    SCHEMA,
    SELECTION_EXPOSURE,
    STILL_BOUND,
    STOP_INVALID,
    SaturationError,
    assert_bootstrap_constants_are_locked,
    assert_single_axis,
    baseline_training_lock,
    saturation_training_lock,
)
from .retained import retained_value

__all__ = [
    "ARMS",
    "BASELINE_ARM",
    "BASELINE_LOSS_HISTORY_DIGEST",
    "BASELINE_MAX_EPOCHS",
    "BASELINE_POOLED_MAE",
    "BASELINE_SELECTED_EPOCH",
    "BASELINE_SUFFICIENT",
    "BASELINE_WEIGHTS_SHA256",
    "BOOTSTRAP",
    "BOUND_MARGINAL",
    "CLASSIFICATIONS",
    "CLEAR_IMPROVEMENT",
    "CLEAR_REGRESSION",
    "DECISION_RULE",
    "DETERMINISM_PREFIX_EPOCHS",
    "HARD_CAP_EPOCHS",
    "INCONCLUSIVE",
    "INTERPRETATION_BOUNDARY",
    "NO_EXTENSION_RULE",
    "OUTCOMES",
    "PATIENCE",
    "PREDECESSOR_EXECUTION_LOCK_IDENTITY",
    "PREDECESSOR_OUTCOME",
    "PREDECESSOR_RESULT_IDENTITY",
    "PRIMARY_AXIS",
    "RETRY_RULE",
    "ROLE",
    "SATURATION_ARM",
    "SATURATION_MAX_EPOCHS",
    "SATURATION_OBSERVED",
    "SCHEMA",
    "SELECTION_EXPOSURE",
    "STILL_BOUND",
    "STOP_INVALID",
    "SaturationError",
    "assert_bootstrap_constants_are_locked",
    "assert_single_axis",
    "baseline_training_lock",
    "retained_value",
    "saturation_training_lock",
]
