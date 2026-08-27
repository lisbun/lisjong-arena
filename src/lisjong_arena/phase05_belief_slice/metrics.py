"""Phase 0.5のprediction metrics。

baselineとlearned estimatorへ同じmetric definitionを適用し、train結果だけで
結論を出さずvalidation / testへも同じ定義で報告する。row-sumやglobal
conservationはarchitectureで強制せず、違反量を測るだけにする。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from lisjong_arena.phase05_belief_slice.feature import OPPONENT_COUNT
from lisjong_arena.phase05_belief_slice.sample import Phase05Sample

STABLE_EQUIVALENT_TILE_COUNT = 13
MELD_EQUIVALENT_TILE_COUNT = 3

CONSERVATION_VIOLATION_TOLERANCE = 1e-3
"""violation判定のtolerance。

conditional-uniform baselineはfixed-point表現（`SCALE = 8192`）で丸められる
ため、conservationを満たす配分でも3 opponents合計で最大 3 / (2 * 8192)
≒ 1.8e-4 程度の見かけ上の超過が生じる。violation *rate* がこの表現誤差で
支配されないよう、rate判定にだけtoleranceを適用する。total / mean excessは
toleranceなしのraw excessで報告する。
"""


@dataclass(frozen=True, slots=True)
class Phase05PredictionMetrics:
    """1 partition / 1 predictorのprediction metrics。"""

    sample_count: int
    opponent_row_count: int
    cell_count: int
    per_tile_mae: float
    per_hand_l1: float
    concealed_size_mean_inconsistency: float
    concealed_size_max_inconsistency: float
    conservation_violation_samples: int
    conservation_total_excess: float

    @property
    def conservation_violation_rate(self) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.conservation_violation_samples / self.sample_count

    @property
    def conservation_mean_excess(self) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.conservation_total_excess / self.sample_count


def _validate_prediction(prediction: Sequence[Sequence[float]]) -> None:
    if len(prediction) != OPPONENT_COUNT:
        raise ValueError("prediction must contain exactly 3 opponent rows")


def evaluate_predictions(
    samples: Iterable[Phase05Sample],
    predictions: Iterable[Sequence[Sequence[float]]],
) -> Phase05PredictionMetrics:
    """sampleごとの3 x 34 predictionをrealized labelsと突き合わせる。"""
    sample_count = 0
    row_count = 0
    cell_count = 0
    absolute_error_sum = 0.0
    concealed_size_error_sum = 0.0
    concealed_size_error_max = 0.0
    conservation_violation_samples = 0
    conservation_total_excess = 0.0

    for sample, prediction in zip(samples, predictions, strict=True):
        if not isinstance(sample, Phase05Sample):
            raise TypeError("samples must contain only Phase05Sample values")
        _validate_prediction(prediction)

        sample_count += 1
        tile_count = len(sample.labels.counts[0])
        summed_by_tile = [0.0] * tile_count
        for offset, row in enumerate(sample.labels.counts):
            predicted_row = prediction[offset]
            if len(predicted_row) != tile_count:
                raise ValueError("prediction rows must match the label row length")

            row_count += 1
            row_l1 = 0.0
            row_sum = 0.0
            for tile_index, realized in enumerate(row):
                predicted = predicted_row[tile_index]
                row_l1 += abs(predicted - realized)
                row_sum += predicted
                summed_by_tile[tile_index] += predicted
            cell_count += tile_count
            absolute_error_sum += row_l1

            expected_stable_size = (
                STABLE_EQUIVALENT_TILE_COUNT
                - MELD_EQUIVALENT_TILE_COUNT
                * sample.features.opponent_meld_counts[offset]
            )
            size_error = abs(row_sum - expected_stable_size)
            concealed_size_error_sum += size_error
            concealed_size_error_max = max(concealed_size_error_max, size_error)

        sample_violates = False
        for tile_index, summed in enumerate(summed_by_tile):
            excess = summed - sample.features.remaining_tile_counts[tile_index]
            if excess <= 0.0:
                continue
            conservation_total_excess += excess
            if excess > CONSERVATION_VIOLATION_TOLERANCE:
                sample_violates = True
        if sample_violates:
            conservation_violation_samples += 1

    if sample_count == 0:
        raise ValueError("samples must not be empty")

    return Phase05PredictionMetrics(
        sample_count=sample_count,
        opponent_row_count=row_count,
        cell_count=cell_count,
        per_tile_mae=absolute_error_sum / cell_count,
        per_hand_l1=absolute_error_sum / row_count,
        concealed_size_mean_inconsistency=concealed_size_error_sum / row_count,
        concealed_size_max_inconsistency=concealed_size_error_max,
        conservation_violation_samples=conservation_violation_samples,
        conservation_total_excess=conservation_total_excess,
    )
