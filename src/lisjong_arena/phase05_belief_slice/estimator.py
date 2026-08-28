"""Phase 0.5のdisposable bucketed empirical-mean snapshot estimator。

外部ML frameworkを導入せず、training partitionから

```text
mean(realized concealed count | public feature bucket)
```

を学習するだけのlookup estimatorである。predictionはtraining labelsのmeanな
ので、semantic range 0..4をsilent clippingなしで自然に維持する。

backoff hierarchyはIssue #22がmodel resultを見る前にlockした順序であり、
validation / test resultを見て変更しない。hyperparameter searchは行わない。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from lisjong.belief import tile_type_index

from lisjong_arena.phase05_belief_slice.feature import Phase05Feature
from lisjong_arena.phase05_belief_slice.sample import Phase05Sample

BACKOFF_LEVEL_KEYS: tuple[tuple[str, ...], ...] = (
    (
        "opponent_wind",
        "tile_type",
        "remaining_tile_count",
        "opponent_meld_count",
        "opponent_riichi_state",
        "turn_bucket",
        "opponent_discard_bucket",
    ),
    (
        "opponent_wind",
        "tile_type",
        "remaining_tile_count",
        "opponent_meld_count",
        "turn_bucket",
    ),
    ("tile_type", "remaining_tile_count", "opponent_meld_count", "turn_bucket"),
    ("tile_type", "remaining_tile_count", "opponent_meld_count"),
    ("tile_type", "remaining_tile_count"),
    ("tile_type",),
)
"""Issue #22でlockしたhierarchical backoff。level 0がfull keyである。"""

BACKOFF_LEVEL_COUNT = len(BACKOFF_LEVEL_KEYS)


class Phase05EstimatorError(Exception):
    """estimatorのfitまたはpredictionが成立しない場合。"""


def _key_component(feature: Phase05Feature, name: str) -> object:
    if name == "opponent_wind":
        return feature.opponent_wind
    if name == "tile_type":
        return tile_type_index(feature.tile_type)
    if name == "remaining_tile_count":
        return feature.remaining_tile_count
    if name == "opponent_meld_count":
        return feature.opponent_meld_count
    if name == "opponent_riichi_state":
        return feature.opponent_riichi_state
    if name == "turn_bucket":
        return feature.turn_bucket
    if name == "opponent_discard_bucket":
        return feature.opponent_discard_bucket
    raise Phase05EstimatorError(f"unknown backoff key component {name!r}")


def _level_key(feature: Phase05Feature, level: int) -> tuple[object, ...]:
    return tuple(_key_component(feature, name) for name in BACKOFF_LEVEL_KEYS[level])


@dataclass(frozen=True, slots=True)
class Phase05Prediction:
    """1 cellのpredictionと、実際に使われたbackoff level。"""

    expected_count: float
    backoff_level: int


class BucketedExpectedCountEstimator:
    """training partitionのbucket meanだけを保持するdisposable estimator。"""

    __slots__ = ("_cells",)

    def __init__(self, cells: tuple[dict[tuple[object, ...], float], ...]) -> None:
        if len(cells) != BACKOFF_LEVEL_COUNT:
            raise Phase05EstimatorError(
                f"cells must contain exactly {BACKOFF_LEVEL_COUNT} levels"
            )
        self._cells = cells

    @classmethod
    def fit(cls, samples: Iterable[Phase05Sample]) -> "BucketedExpectedCountEstimator":
        """training sampleのlabelsからlevelごとのbucket meanを学習する。"""
        sums: tuple[dict[tuple[object, ...], int], ...] = tuple(
            {} for _ in range(BACKOFF_LEVEL_COUNT)
        )
        counts: tuple[dict[tuple[object, ...], int], ...] = tuple(
            {} for _ in range(BACKOFF_LEVEL_COUNT)
        )
        observed = 0
        for sample in samples:
            if not isinstance(sample, Phase05Sample):
                raise TypeError("samples must contain only Phase05Sample values")
            for offset, row in enumerate(sample.labels.counts):
                for tile_index, realized in enumerate(row):
                    feature = sample.features.feature(offset, tile_index)
                    observed += 1
                    for level in range(BACKOFF_LEVEL_COUNT):
                        key = _level_key(feature, level)
                        sums[level][key] = sums[level].get(key, 0) + realized
                        counts[level][key] = counts[level].get(key, 0) + 1
        if observed == 0:
            raise Phase05EstimatorError("estimator requires at least one training cell")

        return cls(
            tuple(
                {
                    key: sums[level][key] / counts[level][key]
                    for key in sorted(sums[level], key=repr)
                }
                for level in range(BACKOFF_LEVEL_COUNT)
            )
        )

    @property
    def training_cell_counts(self) -> tuple[int, ...]:
        """backoff levelごとのtraining cell数。"""
        return tuple(len(cells) for cells in self._cells)

    def predict(self, feature: Phase05Feature) -> Phase05Prediction:
        """full keyから順にbackoffし、最初に見つかったbucket meanを返す。"""
        if not isinstance(feature, Phase05Feature):
            raise TypeError("feature must be a Phase05Feature")
        for level in range(BACKOFF_LEVEL_COUNT):
            value = self._cells[level].get(_level_key(feature, level))
            if value is not None:
                return Phase05Prediction(expected_count=value, backoff_level=level)
        raise Phase05EstimatorError(
            "no training cell is available even at the coarsest backoff level"
        )

    def predict_sample(
        self,
        sample: Phase05Sample,
    ) -> tuple[tuple[tuple[float, ...], ...], tuple[int, ...]]:
        """1 sampleの3 x 34 predictionと、使用backoff levelのlevel別件数を返す。"""
        if not isinstance(sample, Phase05Sample):
            raise TypeError("sample must be a Phase05Sample")
        level_counts = [0] * BACKOFF_LEVEL_COUNT
        rows: list[tuple[float, ...]] = []
        for offset in range(len(sample.features.opponent_winds)):
            values: list[float] = []
            for tile_index in range(len(sample.labels.counts[offset])):
                prediction = self.predict(sample.features.feature(offset, tile_index))
                level_counts[prediction.backoff_level] += 1
                values.append(prediction.expected_count)
            rows.append(tuple(values))
        return tuple(rows), tuple(level_counts)
