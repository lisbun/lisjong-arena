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

KAN_COVERAGE_TRAIN_SEEDS = tuple(range(306, 324))
KAN_COVERAGE_VALIDATION_SEEDS = tuple(range(324, 330))
KAN_COVERAGE_DEVELOPMENT_SEEDS = (
    KAN_COVERAGE_TRAIN_SEEDS + KAN_COVERAGE_VALIDATION_SEEDS
)
"""Arena #146 kan coverage-source qualification向けのdevelopment-only seed population。

#131の`180..191`はhistorical Stage 3 Entry Gate populationであり、本successor
pilotへ再利用しない。`306..329`は`281..305`までのlocked rangeの後続にある
freshな連続rangeであり、`180..191`と同じくdevelopment-onlyである。将来の
formal confirmatory TESTへも転用しない。TEST partitionを持たないことは
`STAGE3_DEVELOPMENT`と同じprotocol invariantである。

TRAIN / VALIDATIONというnamingは、existing Phase 5 materialization contractを
そのまま使うためのdevelopment partition名であり、Arena #146ではmodel training /
checkpoint selectionを行わない。
"""


MIX_PILOT_TRAIN_SEEDS = tuple(range(330, 348))
MIX_PILOT_VALIDATION_SEEDS = tuple(range(348, 354))
MIX_PILOT_DEVELOPMENT_SEEDS = MIX_PILOT_TRAIN_SEEDS + MIX_PILOT_VALIDATION_SEEDS
"""Arena #148 population-mix pilot向けのdevelopment-only seed population。

#131の`180..191`と#146の`306..329`はそれぞれhistorical Stage 3 Entry Gate /
coverage-source qualification populationであり、本successor pilotへ再利用しない。
`330..353`は`306..329`の直後にあるfreshな連続rangeであり、同じくdevelopment-only
である。将来のformal confirmatory TESTへも転用しない。TEST partitionを持たない
ことは`STAGE3_DEVELOPMENT` / `KAN_COVERAGE_DEVELOPMENT`と同じprotocol invariant
である。

本pilotの3 armは **意図的に同じordered seedsを共有する**。同じinitial game
randomnessに対してpopulation constructionだけを変えるdevelopment comparisonで
あり、seed reuse事故ではない。armごとに独立したpopulation identity / raw corpus /
datasetを持つ。population差でtrajectoryが分岐するため、同一seedをpaired hidden
-state sampleとしては扱わない。
"""


SCALE_LEARNING_CURVE_TRAIN_SEEDS = tuple(range(360, 424))
SCALE_LEARNING_CURVE_VALIDATION_SEEDS = tuple(range(424, 440))
SCALE_LEARNING_CURVE_SEEDS = (
    SCALE_LEARNING_CURVE_TRAIN_SEEDS + SCALE_LEARNING_CURVE_VALIDATION_SEEDS
)
"""Arena #150 Phase 10 scale learning curve向けのdevelopment-only seed population。

#131の`180..191`、#146の`306..329`、#148の`330..353`はhistorical populationで
あり、本successor childへ再利用しない。Issue #150起票時のpreferred range
`354..433`は、#140 replacement offline TESTが`354..359`をlockしたため使えない。
result exposure前の`SEED PLAN REFORMULATE`を適用し、`354..359`の直後にある
freshな連続range`360..439`へ移した。

TRAIN 64 / VALIDATION 16のwhole-hanchan splitであり、TEST partitionを持たない
ことは`STAGE3_DEVELOPMENT` / `KAN_COVERAGE_DEVELOPMENT` /
`MIX_PILOT_DEVELOPMENT`と同じprotocol invariantである。将来のformal
confirmatory TESTへも転用しない。

S16 / S32 / S64はこのTRAIN 64のnested prefixであり、同じdatasetと同じfixed
VALIDATIONを共有する。subset membershipはseedだけから決まり、Phase 10側の
protocolが所有する。
"""


class FirstPartySplitPolicy(Enum):
    ACCEPTANCE = "first-party-seeds-1000-1007-all-test-v1"
    QUANTITATIVE = "first-party-seeds-100-159-40-10-10-v1"
    STAGE3_DEVELOPMENT = "first-party-seeds-180-191-8-4-development-only-v1"
    KAN_COVERAGE_DEVELOPMENT = "first-party-seeds-306-329-18-6-development-only-v1"
    MIX_PILOT_DEVELOPMENT = "first-party-seeds-330-353-18-6-development-only-v1"
    SCALE_LEARNING_CURVE = "first-party-seeds-360-439-64-16-development-only-v1"


_EXPECTED_SEEDS = {
    FirstPartySplitPolicy.ACCEPTANCE: FIXED_SEEDS,
    FirstPartySplitPolicy.QUANTITATIVE: QUANTITATIVE_SEEDS,
    FirstPartySplitPolicy.STAGE3_DEVELOPMENT: STAGE3_DEVELOPMENT_SEEDS,
    FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT: KAN_COVERAGE_DEVELOPMENT_SEEDS,
    FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT: MIX_PILOT_DEVELOPMENT_SEEDS,
    FirstPartySplitPolicy.SCALE_LEARNING_CURVE: SCALE_LEARNING_CURVE_SEEDS,
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
    if policy is FirstPartySplitPolicy.SCALE_LEARNING_CURVE:
        if game_seed in SCALE_LEARNING_CURVE_TRAIN_SEEDS:
            return DatasetPartition.TRAIN
        if game_seed in SCALE_LEARNING_CURVE_VALIDATION_SEEDS:
            return DatasetPartition.VALIDATION
        raise ValueError("scale learning curve split requires seeds 360..439")
    if policy is FirstPartySplitPolicy.STAGE3_DEVELOPMENT:
        if game_seed in STAGE3_TRAIN_SEEDS:
            return DatasetPartition.TRAIN
        if game_seed in STAGE3_VALIDATION_SEEDS:
            return DatasetPartition.VALIDATION
        raise ValueError("stage 3 development split requires seeds 180..191")
    if policy is FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT:
        if game_seed in KAN_COVERAGE_TRAIN_SEEDS:
            return DatasetPartition.TRAIN
        if game_seed in KAN_COVERAGE_VALIDATION_SEEDS:
            return DatasetPartition.VALIDATION
        raise ValueError("kan coverage development split requires seeds 306..329")
    if policy is FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT:
        if game_seed in MIX_PILOT_TRAIN_SEEDS:
            return DatasetPartition.TRAIN
        if game_seed in MIX_PILOT_VALIDATION_SEEDS:
            return DatasetPartition.VALIDATION
        raise ValueError("mix pilot development split requires seeds 330..353")
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
    "KAN_COVERAGE_DEVELOPMENT_SEEDS",
    "KAN_COVERAGE_TRAIN_SEEDS",
    "KAN_COVERAGE_VALIDATION_SEEDS",
    "MIX_PILOT_DEVELOPMENT_SEEDS",
    "MIX_PILOT_TRAIN_SEEDS",
    "MIX_PILOT_VALIDATION_SEEDS",
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
