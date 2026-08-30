"""Formal TRAIN/VALIDATION data boundary for Phase 8."""

from dataclasses import dataclass

from lisjong_arena.phase5_belief_dataset.model import (
    BeliefDataset,
    DatasetPartition,
)
from lisjong_arena.phase6_snapshot.training import (
    LOCKED_DATASET_IDENTITY,
    LOCKED_RAW_CORPUS_IDENTITY,
    materialize_snapshot_example,
)

from .protocol import build_inventory, build_sequences

LOCKED_TRAIN_SEEDS = tuple(range(100, 140))
LOCKED_VALIDATION_SEEDS = tuple(range(140, 150))
LOCKED_TRAIN_ANCHOR_COUNT = 18_890
LOCKED_VALIDATION_ANCHOR_COUNT = 4_558


@dataclass(frozen=True, slots=True)
class _ReferenceOnlyStep:
    example: object


def validate_formal_dataset(dataset: BeliefDataset) -> None:
    """Validate identities and the development population without touching samples."""
    if dataset.raw_corpus_identity != LOCKED_RAW_CORPUS_IDENTITY:
        raise ValueError("formal Phase 8 requires the locked raw corpus identity")
    if dataset.dataset_identity != LOCKED_DATASET_IDENTITY:
        raise ValueError("formal Phase 8 requires the locked dataset identity")
    expected_seeds = {
        DatasetPartition.TRAIN: LOCKED_TRAIN_SEEDS,
        DatasetPartition.VALIDATION: LOCKED_VALIDATION_SEEDS,
    }
    for partition, seeds in expected_seeds.items():
        actual = tuple(
            assignment.game.game_seed
            for assignment in dataset.games
            if assignment.partition is partition
        )
        if actual != seeds:
            raise ValueError(
                f"formal {partition.value} GameIdentity population differs"
            )
    expected_counts = {
        DatasetPartition.TRAIN: LOCKED_TRAIN_ANCHOR_COUNT,
        DatasetPartition.VALIDATION: LOCKED_VALIDATION_ANCHOR_COUNT,
    }
    for partition, count in expected_counts.items():
        actual = sum(value.partition is partition for value in dataset.examples)
        if actual != count:
            raise ValueError(f"formal {partition.value} TURN anchor count differs")


def materialize_development_sequences(
    references: tuple,
    samples: tuple,
    *,
    example_builder=materialize_snapshot_example,
):
    """Exclude TEST before calling the Phase 6 feature/model-facing builder."""
    if len(references) != len(samples):
        raise ValueError("references and samples must align")
    development = []
    for reference, sample in zip(references, samples, strict=True):
        if reference.partition is DatasetPartition.TEST:
            continue
        if reference.partition not in (
            DatasetPartition.TRAIN,
            DatasetPartition.VALIDATION,
        ):
            raise ValueError("unsupported Phase 8 partition")
        development.append(example_builder(reference, sample))
    return build_sequences(tuple(development))


def inventory_from_dataset(dataset: BeliefDataset):
    """Build the pre-training inventory from compact references only."""
    validate_formal_dataset(dataset)
    development = tuple(
        _ReferenceOnlyStep(reference)
        for reference in dataset.examples
        if reference.partition is not DatasetPartition.TEST
    )
    sequences = build_sequences(development)
    return build_inventory(
        sequences,
        raw_corpus_identity=dataset.raw_corpus_identity,
        dataset_identity=dataset.dataset_identity,
    )


def prepare_formal_sequences(dataset: BeliefDataset, samples: tuple):
    validate_formal_dataset(dataset)
    return materialize_development_sequences(dataset.examples, samples)


__all__ = [
    "LOCKED_TRAIN_ANCHOR_COUNT",
    "LOCKED_TRAIN_SEEDS",
    "LOCKED_VALIDATION_ANCHOR_COUNT",
    "LOCKED_VALIDATION_SEEDS",
    "materialize_development_sequences",
    "inventory_from_dataset",
    "prepare_formal_sequences",
    "validate_formal_dataset",
]
