"""Closed first-party game-atomic split protocols for Phase 5."""

from enum import Enum

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase4_raw_corpus.model import FIXED_SEEDS, RawCorpus
from lisjong_arena.phase5_belief_dataset.model import (
    DatasetPartition,
    GameAssignment,
    GameIdentity,
)

TRAIN_SEEDS = tuple(range(100, 140))
VALIDATION_SEEDS = tuple(range(140, 150))
TEST_SEEDS = tuple(range(150, 160))
QUANTITATIVE_SEEDS = TRAIN_SEEDS + VALIDATION_SEEDS + TEST_SEEDS

STAGE3_TRAIN_SEEDS = tuple(range(180, 188))
STAGE3_VALIDATION_SEEDS = tuple(range(188, 192))
STAGE3_DEVELOPMENT_SEEDS = STAGE3_TRAIN_SEEDS + STAGE3_VALIDATION_SEEDS
"""Stage 3 Entry Gate development-only seed population.

`150..179`はStage 1/2 TEST / Stage 2 fresh holdoutであり再利用しない。この
`180..191`はdevelopment-onlyであり、将来のformal confirmatory TESTへも転用
しない。TEST partitionを持たないことがこのsplitのprotocol invariantである。
"""


class FirstPartySplitPolicy(Enum):
    ACCEPTANCE = "first-party-seeds-1000-1007-all-test-v1"
    QUANTITATIVE = "first-party-seeds-100-159-40-10-10-v1"
    STAGE3_DEVELOPMENT = "first-party-seeds-180-191-8-4-development-only-v1"


_EXPECTED_SEEDS = {
    FirstPartySplitPolicy.ACCEPTANCE: FIXED_SEEDS,
    FirstPartySplitPolicy.QUANTITATIVE: QUANTITATIVE_SEEDS,
    FirstPartySplitPolicy.STAGE3_DEVELOPMENT: STAGE3_DEVELOPMENT_SEEDS,
}


def partition_for_first_party_game(
    source_class: str,
    game_seed: int,
    policy: FirstPartySplitPolicy,
) -> DatasetPartition:
    """Assign a whole game without accepting labels, truth, or metric values."""
    if source_class != FIRST_PARTY_SOURCE_CLASS:
        raise ValueError("Phase 5 currently supports only the first-party source")
    if type(game_seed) is not int:
        raise TypeError("game_seed must be an int")
    if not isinstance(policy, FirstPartySplitPolicy):
        raise TypeError("policy must be a FirstPartySplitPolicy")
    if policy is FirstPartySplitPolicy.ACCEPTANCE:
        if game_seed not in FIXED_SEEDS:
            raise ValueError("acceptance split requires seeds 1000..1007")
        return DatasetPartition.TEST
    if policy is FirstPartySplitPolicy.STAGE3_DEVELOPMENT:
        if game_seed in STAGE3_TRAIN_SEEDS:
            return DatasetPartition.TRAIN
        if game_seed in STAGE3_VALIDATION_SEEDS:
            return DatasetPartition.VALIDATION
        raise ValueError("stage 3 development split requires seeds 180..191")
    if game_seed in TRAIN_SEEDS:
        return DatasetPartition.TRAIN
    if game_seed in VALIDATION_SEEDS:
        return DatasetPartition.VALIDATION
    if game_seed in TEST_SEEDS:
        return DatasetPartition.TEST
    raise ValueError("quantitative split requires seeds 100..159")


def assign_first_party_games(
    corpus: RawCorpus, policy: FirstPartySplitPolicy
) -> tuple[GameAssignment, ...]:
    """Bind the exact locked population to whole-game partitions."""
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    if not isinstance(policy, FirstPartySplitPolicy):
        raise TypeError("policy must be a FirstPartySplitPolicy")
    seeds = tuple(game.seed for game in corpus.games)
    expected = _EXPECTED_SEEDS[policy]
    if seeds != expected:
        raise ValueError(
            f"{policy.name.lower()} split requires its exact locked seed population"
        )
    return tuple(
        GameAssignment(
            GameIdentity(FIRST_PARTY_SOURCE_CLASS, seed),
            partition_for_first_party_game(FIRST_PARTY_SOURCE_CLASS, seed, policy),
        )
        for seed in seeds
    )


__all__ = [
    "QUANTITATIVE_SEEDS",
    "STAGE3_DEVELOPMENT_SEEDS",
    "STAGE3_TRAIN_SEEDS",
    "STAGE3_VALIDATION_SEEDS",
    "TEST_SEEDS",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "FirstPartySplitPolicy",
    "assign_first_party_games",
    "partition_for_first_party_game",
]
