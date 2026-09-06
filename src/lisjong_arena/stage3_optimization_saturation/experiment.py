"""E160 armのtraining orchestration。

E160は#157 E80とexactに同じdata viewの上で走る。

```text
#150 retained population (80 hanchan corpus / dataset)
    -> Phase 8 canonical sequences            #150 build_data
    -> S64 nested TRAIN view (360..423)       #150 scale_data
    -> shared fixed VALIDATION (424..439)
    -> Phase 8 train_candidate(S2, config)    max_epochsだけ160
```

data構成、sequence semantics、BPTT policy、conditional-uniform reference arm、
checkpoint selection、self-rollout、physical projectionは#150 / #157のものを
そのまま使う。#167が触るのは`TrainingConfig.max_epochs`だけであり、他をcaller
optionにしない。

trainingはepoch 1から始める。#157 E80 checkpointからのresumeは行わない。現在の
training artifact contractはoptimizer state / RNG continuation stateをlockして
おらず、resume contractを本childで新設しないためである。先頭80 epochの一致は
determinism gateが実測で確認する。

新規generationは行わない。

```text
new hanchan             0
new seed                0
new corpus generation   0
formal TEST exposure    0
baseline retraining     0
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
    saturation_training_config,
)


def saturation_population_data(raw, dataset):
    """retained corpusからfull 80-hanchan populationのPhase 8 dataを構成する。

    BPTT policyもshared canonical VALIDATIONもfull inventoryから決まるので、
    #150 S64 / #157 E80と同じsemanticsになる。
    """
    return build_data(raw, dataset)


def saturation_train_view(full):
    """full populationから#157 E80とexactに同じnested TRAIN viewを作る。"""
    return scale_data(full, RETAINED_SCALE)


def train_saturation_arm(data):
    """E160をlocked S2 familyで1回だけepoch 1からtrainingする。

    `max_epochs`以外はPhase 8 `FORMAL_TRAINING_CONFIG`のままである。RNG stream
    はconfig seedだけで決まり、`max_epochs`はloop上限を動かすだけなので、
    先頭80 epochは#157 E80のhistoryとexactに一致しなければならない。
    """
    from lisjong_arena.phase8_sequential.training import train_candidate

    return train_candidate(
        CANDIDATE,
        data.sequences,
        dataset_identity=data.dataset_identity,
        bptt_policy=data.inventory.bptt_policy,
        canonical_validation=data.canonical_validation,
        config=saturation_training_config(),
    )


def saturation_binding(full, provenance: dict[str, object]) -> dict[str, object]:
    """E160をexact TRAIN subset / dataset / source provenanceへbindするvalue。

    #150 S64 / #157 E80 manifestの`subset` / `train_anchor_identities` /
    `full_inventory`と同じ導出を使うので、E160がE80と同じTRAIN membershipで
    学習したことをartifact自身が証明できる。
    """
    return training_binding(full, RETAINED_SCALE, provenance)


__all__ = [
    "CANDIDATE",
    "configure_torch_runtime",
    "saturation_binding",
    "saturation_population_data",
    "saturation_train_view",
    "train_saturation_arm",
]
