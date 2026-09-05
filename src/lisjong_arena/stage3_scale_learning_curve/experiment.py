"""1つのdataset、nested TRAIN sequences、1つのshared canonical VALIDATION。

```text
one locked 80-hanchan raw corpus
    -> one versioned Phase 5-compatible dataset
    -> S16 / S32 / S64 nested TRAIN
    -> shared fixed VALIDATION 16
```

80 hanchanをscaleごとに再生成しない。3 scaleは同じdataset、同じcanonical
VALIDATION、同じPhase 8 inventory / BPTT policyを共有し、TRAIN sequenceの
membershipだけが変わる。BPTT policyはfull 80-hanchan inventoryから一度だけ
決めるので、scaleごとにtraining semanticsがadaptiveに変わらない。

orchestrationは`stage3_entry_gate.experiment`をそのまま使う。model family、
input / target semantics、optimizer family、training budget、checkpoint
selection rule、deterministic conditionsをPhase 10側で変えない。
"""

from dataclasses import replace

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    REFERENCE_ARM_ID,
    build_population_data,
    configure_torch_runtime,
    evaluate_on_population,
    train_population_candidate,
)

from .population import population_identity, subset_binding
from .protocol import (
    ORDERED_SEEDS,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    ScaleError,
    exact,
    train_seeds,
)

POPULATION_ID = "PHASE10"


def validate_dataset(dataset) -> None:
    """Phase 10 development populationとしてのdatasetをfail closedで検証する。

    #131 / #146 / #148のhistorical validatorは自分のlocked seedsへbindされて
    おり、Phase 10のために緩めない。successor-specificな同型の validatorを持つ。
    """
    exact(dataset.split_policy_id, SPLIT_POLICY.value, "Phase 10 split")
    exact(
        [assignment.game.game_seed for assignment in dataset.games],
        list(ORDERED_SEEDS),
        "whole population",
    )
    for partition, seeds in (
        (DatasetPartition.TRAIN, TRAIN_SEEDS),
        (DatasetPartition.VALIDATION, VALIDATION_SEEDS),
    ):
        exact(
            [
                assignment.game.game_seed
                for assignment in dataset.games
                if assignment.partition is partition
            ],
            list(seeds),
            partition.value,
        )
    partition_by_game = {
        assignment.game: assignment.partition for assignment in dataset.games
    }
    for reference in dataset.examples:
        if (
            reference.partition is DatasetPartition.TEST
            or partition_by_game.get(reference.game) is not reference.partition
        ):
            raise ScaleError("example crosses the locked whole-hanchan partition")
    if {reference.game for reference in dataset.examples} != set(partition_by_game):
        raise ScaleError("dataset silently dropped a game's anchors")


def build_data(raw, dataset):
    """full 80-hanchan populationのPhase 8 sequencesとreference armを構成する。"""
    return build_population_data(
        population_id=POPULATION_ID,
        population_identity=population_identity(),
        persisted_raw=raw,
        dataset=dataset,
        validate=validate_dataset,
    )


def scale_data(full, scale: str):
    """full populationからnested TRAIN subsetのviewを作る。

    membershipはseedだけから決まる。VALIDATION sequences、canonical
    validation、inventory / BPTT policyはfull populationのものをそのまま共有
    するので、training semanticsがscaleごとに変わらない。
    """
    expected = train_seeds(scale)
    selected = tuple(
        sequence
        for sequence in full.train_sequences
        if sequence.key.game.game_seed in expected
    )
    exact(
        sorted({sequence.key.game.game_seed for sequence in selected}),
        list(expected),
        "nested TRAIN membership",
    )
    exact(
        sorted({sequence.key.game.game_seed for sequence in full.validation_sequences}),
        list(VALIDATION_SEEDS),
        "shared VALIDATION membership",
    )
    if any(
        sequence.partition is not DatasetPartition.TRAIN for sequence in selected
    ) or any(
        sequence.partition is not DatasetPartition.VALIDATION
        for sequence in full.validation_sequences
    ):
        raise ScaleError("sequence partition leakage")
    return replace(full, population_id=scale, train_sequences=selected)


def train_scale(full, scale: str):
    """1 scaleでlocked S2をtrainingする。scale固有のconfigは存在しない。"""
    return train_population_candidate(scale_data(full, scale))


def train_anchor_identities(full, scale: str) -> list[str]:
    """そのscaleが実際に学習したTRAIN anchorのidentity。"""
    return sorted(
        step.example.identity
        for sequence in scale_data(full, scale).train_sequences
        for step in sequence.steps
    )


def validation_anchor_identities(full) -> list[str]:
    """shared fixed VALIDATIONのcanonical anchor identity列。"""
    return [value.example.identity for value in full.canonical_validation.examples]


def training_binding(full, scale: str, provenance: dict[str, object]):
    """modelをexact TRAIN subset / dataset / source provenanceへbindするvalue。"""
    from lisjong_arena.phase8_sequential.protocol import inventory_value

    return {
        "subset": subset_binding(
            scale,
            raw_corpus_identity=full.raw_corpus_identity,
            dataset_identity=full.dataset_identity,
            provenance=provenance,
        ),
        "train_anchor_identities": train_anchor_identities(full, scale),
        "full_inventory": inventory_value(full.inventory),
    }


__all__ = [
    "CANDIDATE",
    "POPULATION_ID",
    "REFERENCE_ARM_ID",
    "build_data",
    "configure_torch_runtime",
    "evaluate_on_population",
    "scale_data",
    "train_anchor_identities",
    "train_scale",
    "training_binding",
    "validate_dataset",
    "validation_anchor_identities",
]
