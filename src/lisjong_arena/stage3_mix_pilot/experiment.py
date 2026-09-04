"""Arena #148 fixed-budget S2 training and 3 x 3 cross-population evaluation。

本pilotはarchitecture searchでもHPOでもない。Phase 8でlockされたS2 /
previous-belief GRU-cell familyとその`FORMAL_TRAINING_CONFIG`、#131 Stage 3
Entry Gateで確立したconditional-uniform reference arm semanticsをそのまま
再利用し、**populationだけ** を変える。

```text
mix arm dataset
    -> Phase 8 canonical sequences (既存 build_sequences / build_inventory)
    -> conditional-uniform reference arm  (#131と同じ)
    -> Phase 8 train_candidate(S2, FORMAL_TRAINING_CONFIG)
    -> Phase 8 self_rollout / evaluate_candidate
```

したがってここに固有なのは「本pilotのlocked seed populationとしてdatasetを
検証すること」だけである。それ以外のorchestrationは
`stage3_entry_gate.experiment`をそのまま使い、model family / input / target
semantics / optimizer family / training budget / checkpoint selection rule /
deterministic conditionsを一切変えない。

## Cross-population boundary

3 armは同じordered seedsを共有するため、`TurnExampleReference.identity`は
arm間で衝突し得る。cross-population評価は必ず **評価先armのsequencesと
canonical validationだけ** を使い、predictionとreferenceをarm間で突き合わせ
ない。`Stage3PopulationData`がそのboundaryを型で持つ。
"""

from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus
from lisjong_arena.phase5_belief_dataset.model import BeliefDataset, DatasetPartition
from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    REFERENCE_ARM_ID,
    Stage3ExperimentError,
    Stage3PopulationData,
    conditional_uniform_reference,
    configure_torch_runtime,
    evaluate_on_population,
    evaluation_value,
    inventory_summary,
    train_population_candidate,
)
from lisjong_arena.stage3_entry_gate.experiment import (
    build_population_data as _build_population_data,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)


class MixExperimentError(ValueError):
    """mix pilot experiment orchestrationのcontract violation。"""


def validate_mix_dataset(dataset: BeliefDataset) -> None:
    """mix pilot development populationとしてのdatasetをfail closedで検証する。

    #131の`validate_stage3_dataset()`は`180..191`へlockされたhistorical
    validatorであり、本pilotのために緩めない。successor-specificな同型の
    validatorをここへ持つ。
    """
    if dataset.split_policy_id != SPLIT_POLICY.value:
        raise MixExperimentError("dataset does not use the mix pilot split policy")
    if any(
        reference.partition is DatasetPartition.TEST for reference in dataset.examples
    ):
        raise MixExperimentError("mix pilot datasets must not contain TEST examples")
    expected = {
        DatasetPartition.TRAIN: TRAIN_SEEDS,
        DatasetPartition.VALIDATION: VALIDATION_SEEDS,
    }
    for partition, seeds in expected.items():
        actual = tuple(
            assignment.game.game_seed
            for assignment in dataset.games
            if assignment.partition is partition
        )
        if actual != seeds:
            raise MixExperimentError(
                f"mix pilot {partition.value} population differs from the locked seeds"
            )


def build_arm_data(
    *,
    arm_id: str,
    population_identity: str,
    persisted_raw: PersistedRawCorpus,
    dataset: BeliefDataset,
) -> Stage3PopulationData:
    """1 armのPhase 8 canonical sequencesとreference armを構成する。"""
    return _build_population_data(
        population_id=arm_id,
        population_identity=population_identity,
        persisted_raw=persisted_raw,
        dataset=dataset,
        validate=validate_mix_dataset,
    )


__all__ = [
    "CANDIDATE",
    "REFERENCE_ARM_ID",
    "MixExperimentError",
    "Stage3ExperimentError",
    "Stage3PopulationData",
    "build_arm_data",
    "conditional_uniform_reference",
    "configure_torch_runtime",
    "evaluate_on_population",
    "evaluation_value",
    "inventory_summary",
    "train_population_candidate",
    "validate_mix_dataset",
]
