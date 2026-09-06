"""E80 armのtraining orchestration。

E80は#150 S64とexactに同じdata viewの上で走る。

```text
#150 retained population (80 hanchan corpus / dataset)
    -> Phase 8 canonical sequences            #150 build_data
    -> S64 nested TRAIN view (360..423)       #150 scale_data
    -> shared fixed VALIDATION (424..439)
    -> Phase 8 train_candidate(S2, config)    max_epochsだけ80
```

data構成、sequence semantics、BPTT policy、conditional-uniform reference arm、
checkpoint selection、self-rollout、physical projectionは#150のものをそのまま
使う。#157が触るのは`TrainingConfig.max_epochs`だけであり、他をcaller optionに
しない。

新規generationは行わない。

```text
new hanchan             0
new seed                0
new corpus generation   0
formal TEST exposure    0
```
"""

from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    configure_torch_runtime,
)
from lisjong_arena.stage3_scale_learning_curve.experiment import (
    build_data,
    scale_data,
    training_binding,
)

from .protocol import (
    RETAINED_SCALE,
    budget_training_config,
)


def budget_population_data(raw, dataset):
    """retained corpusからfull 80-hanchan populationのPhase 8 dataを構成する。

    BPTT policyもshared canonical VALIDATIONもfull inventoryから決まるので、
    #150 S64と同じsemanticsになる。
    """
    return build_data(raw, dataset)


def budget_train_view(full):
    """full populationから#150 S64とexactに同じnested TRAIN viewを作る。"""
    return scale_data(full, RETAINED_SCALE)


def train_budget_arm(data):
    """E80をlocked S2 familyで1回だけtrainingする。

    `max_epochs`以外はPhase 8 `FORMAL_TRAINING_CONFIG`のままである。RNG stream
    はconfig seedだけで決まり、`max_epochs`はloop上限を動かすだけなので、
    先頭40 epochは#150 S64のhistoryとexactに一致しなければならない。
    """
    from lisjong_arena.phase8_sequential.training import train_candidate

    return train_candidate(
        CANDIDATE,
        data.sequences,
        dataset_identity=data.dataset_identity,
        bptt_policy=data.inventory.bptt_policy,
        canonical_validation=data.canonical_validation,
        config=budget_training_config(),
    )


def budget_binding(full, provenance: dict[str, object]) -> dict[str, object]:
    """E80をexact TRAIN subset / dataset / source provenanceへbindするvalue。

    #150 S64 manifestの`subset` / `train_anchor_identities` / `full_inventory`と
    同じ導出を使うので、E80がE40と同じTRAIN membershipで学習したことを
    artifact自身が証明できる。
    """
    return training_binding(full, RETAINED_SCALE, provenance)


__all__ = [
    "CANDIDATE",
    "budget_binding",
    "budget_population_data",
    "budget_train_view",
    "configure_torch_runtime",
    "train_budget_arm",
]
