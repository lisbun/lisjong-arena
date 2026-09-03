"""Stage 3 Entry Gate fixed-budget S2 training and cross-population evaluation.

Stage 3はarchitecture searchではない。Phase 8でlockされたS2 / previous-belief
GRU-cell familyとその`FORMAL_TRAINING_CONFIG`をそのまま再利用し、population
だけを変える。

```text
Stage 3 population dataset
    -> Phase 8 canonical sequences (既存 build_sequences / build_inventory)
    -> conditional-uniform reference arm
    -> Phase 8 train_candidate(S2, FORMAL_TRAINING_CONFIG)
    -> Phase 8 self_rollout / evaluate_candidate
```

## Reference arm

Phase 8の`CanonicalValidation`はfrozen Phase 6 snapshotをreference armに取る。
Stage 3の3 populationにはPhase 6 snapshotが存在せず、Arena #131が要求する
comparatorは各VALIDATION population上のconditional-uniform baselineである。
したがってStage 3はreference armへconditional-uniform baseline predictionを
bindし、

```text
Delta MAE = conditional-uniform VALIDATION MAE - S2 VALIDATION MAE
```

とする。Phase 8のsnapshot semantics、`SNAPSHOT_VALIDATION_MAE`、formal
population validator、artifact validatorは変更も再利用もしない。
`CandidateSummary.advancement_eligible`はPhase 8 formal population前提の
判定であり、Stage 3 classificationには使わない。

## Cross-population boundary

`TurnExampleReference.identity`はpopulationを含まないため、同じseedを使う
3 populationのanchor identityは衝突し得る。cross-population評価は必ず
**評価先populationのsequencesとcanonical validationだけ** を使い、prediction
とreferenceをpopulation間で突き合わせない。`Stage3PopulationData`がその
boundaryを型で持つ。
"""

from dataclasses import dataclass

from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import (
    evaluate_expected_count_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import BeliefDataset, DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import (
    STAGE3_TRAIN_SEEDS,
    STAGE3_VALIDATION_SEEDS,
    FirstPartySplitPolicy,
)
from lisjong_arena.phase6_snapshot.training import expected_count_baseline_prediction
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.phase8_sequential.evaluation import (
    CandidateEvaluation,
    CanonicalValidation,
    evaluate_candidate,
    metrics_value,
)
from lisjong_arena.phase8_sequential.protocol import (
    Candidate,
    SequenceInventory,
    build_inventory,
    build_sequences,
    inventory_value,
)

CANDIDATE = Candidate.S2
REFERENCE_ARM_ID = "stage3-conditional-uniform-reference-arm-v1"


class Stage3ExperimentError(ValueError):
    """Stage 3 experiment orchestrationのcontract violation。"""


def validate_stage3_dataset(dataset: BeliefDataset) -> None:
    """Stage 3 development populationとしてのdatasetをfail closedで検証する。"""
    if dataset.split_policy_id != FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value:
        raise Stage3ExperimentError("dataset does not use the Stage 3 split policy")
    if any(
        reference.partition is DatasetPartition.TEST for reference in dataset.examples
    ):
        raise Stage3ExperimentError("Stage 3 datasets must not contain TEST examples")
    expected = {
        DatasetPartition.TRAIN: STAGE3_TRAIN_SEEDS,
        DatasetPartition.VALIDATION: STAGE3_VALIDATION_SEEDS,
    }
    for partition, seeds in expected.items():
        actual = tuple(
            assignment.game.game_seed
            for assignment in dataset.games
            if assignment.partition is partition
        )
        if actual != seeds:
            raise Stage3ExperimentError(
                f"Stage 3 {partition.value} population differs from the locked seeds"
            )


def conditional_uniform_reference(
    dataset_identity: str, validation_examples: tuple
) -> CanonicalValidation:
    """VALIDATION populationのconditional-uniform reference armを構成する。

    Arena自身がbelief mathを複製せず、pinnedな
    `lisjong.belief.estimate_conditional_uniform_hand_belief()`をPhase 5/6の
    既存seam経由で呼ぶ。
    """
    if not validation_examples:
        raise Stage3ExperimentError("reference arm requires VALIDATION examples")
    references = tuple(value.example for value in validation_examples)
    samples = tuple(value.sample for value in validation_examples)
    predictions = tuple(
        expected_count_baseline_prediction(reference, sample)
        for reference, sample in zip(references, samples, strict=True)
    )
    report = evaluate_expected_count_predictions(
        dataset_identity, references, samples, predictions
    )
    return CanonicalValidation(
        validation_examples, predictions, report.partition_metrics[0].metrics
    )


@dataclass(frozen=True, slots=True)
class Stage3PopulationData:
    """1 populationのmaterialized sequences、inventory、reference arm。

    population identityを保持し、cross-population評価でtrain側とvalidation側
    のpopulationを取り違えないようにする。
    """

    population_id: str
    population_identity: str
    raw_corpus_identity: str
    dataset_identity: str
    inventory: SequenceInventory
    train_sequences: tuple
    validation_sequences: tuple
    canonical_validation: CanonicalValidation

    @property
    def sequences(self) -> tuple:
        return self.train_sequences + self.validation_sequences

    @property
    def baseline_metrics(self):
        return self.canonical_validation.snapshot_metrics


def build_population_data(
    *,
    population_id: str,
    population_identity: str,
    persisted_raw: PersistedRawCorpus,
    dataset: BeliefDataset,
) -> Stage3PopulationData:
    """datasetからPhase 8 canonical sequencesとreference armを構成する。"""
    validate_stage3_dataset(dataset)
    samples = resolve_training_samples(dataset, persisted_raw)
    examples = materialize_development_examples(dataset.examples, samples)
    sequences = build_sequences(examples)
    inventory = build_inventory(
        sequences,
        raw_corpus_identity=dataset.raw_corpus_identity,
        dataset_identity=dataset.dataset_identity,
    )
    train_sequences = tuple(
        value for value in sequences if value.partition is DatasetPartition.TRAIN
    )
    validation_sequences = tuple(
        value for value in sequences if value.partition is DatasetPartition.VALIDATION
    )
    if not train_sequences or not validation_sequences:
        raise Stage3ExperimentError("Stage 3 requires TRAIN and VALIDATION sequences")
    validation_examples = tuple(
        value
        for value in examples
        if value.example.partition is DatasetPartition.VALIDATION
    )
    return Stage3PopulationData(
        population_id=population_id,
        population_identity=population_identity,
        raw_corpus_identity=dataset.raw_corpus_identity,
        dataset_identity=dataset.dataset_identity,
        inventory=inventory,
        train_sequences=train_sequences,
        validation_sequences=validation_sequences,
        canonical_validation=conditional_uniform_reference(
            dataset.dataset_identity, validation_examples
        ),
    )


def configure_torch_runtime() -> None:
    """locked training configと同じCPU runtimeをprocessへ適用する。

    `train_candidate()`はtraining process内でthread数とdeterministic algorithms
    を設定する。cross-population evaluationは別processで動くため、同じ設定を
    明示的に適用しないとrolloutがtraining時と別のruntimeで走る。値は
    `FORMAL_TRAINING_CONFIG`から取り、Stage 3側で別の値を選ばない。
    """
    import torch

    from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

    torch.set_num_threads(FORMAL_TRAINING_CONFIG.torch_threads)
    torch.use_deterministic_algorithms(FORMAL_TRAINING_CONFIG.deterministic_algorithms)


def train_population_candidate(data: Stage3PopulationData):
    """fixed Phase 8 budgetでS2をtrainingする。

    `TrainingConfig`はPhase 8 `FORMAL_TRAINING_CONFIG`をそのまま使い、Stage 3
    側でoverrideできるcaller optionにしない。
    """
    from lisjong_arena.phase8_sequential.training import (
        FORMAL_TRAINING_CONFIG,
        train_candidate,
    )

    return train_candidate(
        CANDIDATE,
        data.sequences,
        dataset_identity=data.dataset_identity,
        bptt_policy=data.inventory.bptt_policy,
        canonical_validation=data.canonical_validation,
        config=FORMAL_TRAINING_CONFIG,
    )


def evaluate_on_population(model, data: Stage3PopulationData) -> CandidateEvaluation:
    """1つのtrained modelを、指定populationのVALIDATIONで自己rolloutする。

    rolloutもmetricsも評価先populationのvaluesだけで閉じる。
    """
    from lisjong_arena.phase8_sequential.rollout import self_rollout

    rollout = self_rollout(model, CANDIDATE, data.validation_sequences)
    return evaluate_candidate(
        CANDIDATE,
        data.validation_sequences,
        rollout,
        data.canonical_validation,
        dataset_identity=data.dataset_identity,
    )


def evaluation_value(
    evaluation: CandidateEvaluation, data: Stage3PopulationData
) -> dict[str, object]:
    """1 cellの結果を、population identityつきのplain valueへ落とす。"""
    return {
        "validation_population_id": data.population_id,
        "validation_population_identity": data.population_identity,
        "validation_dataset_identity": data.dataset_identity,
        "sequential_metrics": metrics_value(evaluation.metrics),
        "conditional_uniform_metrics": metrics_value(evaluation.snapshot_metrics),
        "sequential_validation_mae": evaluation.metrics.per_tile_mae,
        "conditional_uniform_validation_mae": (
            evaluation.snapshot_metrics.per_tile_mae
        ),
        "delta_mae_vs_conditional_uniform": evaluation.delta_mae,
        "per_game": list(evaluation.per_game),
        "game_macro_mean_delta_mae": evaluation.game_macro_mean_delta_mae,
        "median_per_game_delta_mae": evaluation.median_per_game_delta_mae,
        "positive_game_count": evaluation.positive_game_count,
        "validation_game_count": len(evaluation.per_game),
        "depth_diagnostics": list(evaluation.depth_diagnostics),
        "physical_consistency": dict(evaluation.physical_consistency),
    }


def inventory_summary(data: Stage3PopulationData) -> dict[str, object]:
    return inventory_value(data.inventory)


__all__ = [
    "CANDIDATE",
    "REFERENCE_ARM_ID",
    "Stage3ExperimentError",
    "Stage3PopulationData",
    "build_population_data",
    "conditional_uniform_reference",
    "configure_torch_runtime",
    "evaluate_on_population",
    "evaluation_value",
    "inventory_summary",
    "train_population_candidate",
    "validate_stage3_dataset",
]
