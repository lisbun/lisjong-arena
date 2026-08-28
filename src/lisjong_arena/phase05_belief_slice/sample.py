"""Phase 0.5のexperiment-local sample表現とgame-grouped split定義。

同一game由来のall viewer seats / all TURN anchorsは必ず同じpartitionへ置く。
partitionはseedだけから決まるため、row-level random splitは構造上できない。

seed rangeはIssue #22でmodel resultを見る前にlockしたprotocol条件であり、
caller-configurable defaultにしない。
"""

import json
from dataclasses import dataclass
from enum import Enum

from lisjong.belief import TILE_TYPE_COUNT
from lisjong.policy_contract import Wind

from lisjong_arena.phase05_belief_slice.feature import (
    OPPONENT_COUNT,
    Phase05AnchorFeatures,
)
from lisjong_arena.phase05_belief_slice.label import Phase05Labels

TRAIN_SEEDS = tuple(range(100, 140))
VALIDATION_SEEDS = tuple(range(140, 150))
TEST_SEEDS = tuple(range(150, 160))
EXPERIMENT_SEEDS = TRAIN_SEEDS + VALIDATION_SEEDS + TEST_SEEDS


class Phase05Partition(Enum):
    """game-grouped partition。同一seedのsampleは必ず同じpartitionへ入る。"""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


_PARTITION_BY_SEED: dict[int, Phase05Partition] = {
    **{seed: Phase05Partition.TRAIN for seed in TRAIN_SEEDS},
    **{seed: Phase05Partition.VALIDATION for seed in VALIDATION_SEEDS},
    **{seed: Phase05Partition.TEST for seed in TEST_SEEDS},
}


def partition_for_seed(seed: int) -> Phase05Partition:
    """lockしたseed rangeからpartitionを決める。未登録seedはfail closedする。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    partition = _PARTITION_BY_SEED.get(seed)
    if partition is None:
        raise ValueError(f"seed {seed} is outside the locked Phase 0.5 seed ranges")
    return partition


@dataclass(frozen=True, slots=True)
class Phase05Sample:
    """1 TURN anchor / 1 viewerのfeature・label・baseline predictionの組。

    `baseline_expected_counts`はcurrent
    `estimate_conditional_uniform_hand_belief()`をこのanchorへ適用した結果で
    あり、featureと同じくPolicy-visible情報だけから決まる。
    """

    seed: int
    partition: Phase05Partition
    anchor_index: int
    features: Phase05AnchorFeatures
    labels: Phase05Labels
    baseline_expected_counts: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if not isinstance(self.partition, Phase05Partition):
            raise TypeError("partition must be a Phase05Partition")
        if type(self.anchor_index) is not int or self.anchor_index < 0:
            raise ValueError("anchor_index must be a non-negative int")
        if not isinstance(self.features, Phase05AnchorFeatures):
            raise TypeError("features must be a Phase05AnchorFeatures")
        if not isinstance(self.labels, Phase05Labels):
            raise TypeError("labels must be a Phase05Labels")
        if self.features.opponent_winds != self.labels.opponent_winds:
            raise ValueError("features and labels must share the same opponent order")
        if len(self.baseline_expected_counts) != OPPONENT_COUNT:
            raise ValueError("baseline_expected_counts must contain exactly 3 rows")
        for row in self.baseline_expected_counts:
            if len(row) != TILE_TYPE_COUNT:
                raise ValueError(
                    "each baseline_expected_counts row must contain exactly "
                    f"{TILE_TYPE_COUNT} values"
                )

    @property
    def viewer_wind(self) -> Wind:
        return self.features.viewer_wind


def sample_to_json_object(sample: Phase05Sample) -> dict[str, object]:
    """storage measurement用のexperiment-local serialization。

    production raw-corpus formatの決定ではない。生成物はrepositoryへ
    commitしない。
    """
    if not isinstance(sample, Phase05Sample):
        raise TypeError("sample must be a Phase05Sample")

    return {
        "seed": sample.seed,
        "partition": sample.partition.value,
        "anchor_index": sample.anchor_index,
        "viewer_wind": sample.features.viewer_wind.value,
        "opponent_winds": [wind.value for wind in sample.features.opponent_winds],
        "remaining_tile_counts": list(sample.features.remaining_tile_counts),
        "opponent_meld_counts": list(sample.features.opponent_meld_counts),
        "opponent_riichi_states": [
            sample.features.feature(offset, 0).opponent_riichi_state.value
            for offset in range(OPPONENT_COUNT)
        ],
        "turn_bucket": sample.features.feature(0, 0).turn_bucket.value,
        "opponent_discard_buckets": [
            [
                sample.features.feature(
                    offset, tile_index
                ).opponent_discard_bucket.value
                for tile_index in range(TILE_TYPE_COUNT)
            ]
            for offset in range(OPPONENT_COUNT)
        ],
        "labels": [list(row) for row in sample.labels.counts],
        "concealed_sizes": list(sample.labels.concealed_sizes),
        "baseline_expected_counts": [
            list(row) for row in sample.baseline_expected_counts
        ],
    }


def serialize_samples_to_jsonl(samples: object) -> bytes:
    """sample列をJSON Lines bytesへserializeする。"""
    lines = [
        json.dumps(sample_to_json_object(sample), separators=(",", ":"), sort_keys=True)
        for sample in samples
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
