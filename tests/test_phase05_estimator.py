"""lisjong-project #22 Phase 0.5 bucketed estimator tests。"""

import unittest

from lisjong.policy_contract import RiichiState, Wind

from lisjong_arena.phase05_belief_slice.estimator import (
    BACKOFF_LEVEL_COUNT,
    BACKOFF_LEVEL_KEYS,
    BucketedExpectedCountEstimator,
    Phase05EstimatorError,
)
from lisjong_arena.phase05_belief_slice.feature import (
    OpponentDiscardBucket,
    Phase05Feature,
    TurnBucket,
)
from tests import _phase05_fixtures as fixtures

_OPPONENT_WINDS = (Wind.SOUTH, Wind.WEST, Wind.NORTH)


def _feature(
    *,
    viewer_wind: Wind = Wind.EAST,
    opponent_wind: Wind = Wind.SOUTH,
    tile_index: int = 0,
    remaining: int = 4,
    melds: int = 0,
    riichi: RiichiState = RiichiState.NONE,
    turn: TurnBucket = TurnBucket.EARLY,
    discards: OpponentDiscardBucket = OpponentDiscardBucket.NONE,
) -> Phase05Feature:
    from lisjong.belief import tile_type_from_index

    return Phase05Feature(
        viewer_wind=viewer_wind,
        opponent_wind=opponent_wind,
        tile_type=tile_type_from_index(tile_index),
        remaining_tile_count=remaining,
        opponent_meld_count=melds,
        opponent_riichi_state=riichi,
        turn_bucket=turn,
        opponent_discard_bucket=discards,
    )


def _uniform_sample(
    *,
    seed: int = 100,
    anchor_index: int = 0,
    riichi: RiichiState = RiichiState.NONE,
    label_counts: tuple[tuple[int, ...], ...] | None = None,
):
    def build(opponent_wind: Wind, tile_index: int) -> Phase05Feature:
        return _feature(
            opponent_wind=opponent_wind,
            tile_index=tile_index,
            riichi=riichi,
        )

    return fixtures.sample(
        seed=seed,
        anchor_index=anchor_index,
        features=fixtures.anchor_features(feature_factory=build),
        label_counts=label_counts,
    )


class BackoffDefinitionTest(unittest.TestCase):
    def test_locked_backoff_hierarchy_is_strictly_coarsening(self) -> None:
        self.assertEqual(BACKOFF_LEVEL_COUNT, 6)
        self.assertEqual(
            BACKOFF_LEVEL_KEYS[0],
            (
                "opponent_wind",
                "tile_type",
                "remaining_tile_count",
                "opponent_meld_count",
                "opponent_riichi_state",
                "turn_bucket",
                "opponent_discard_bucket",
            ),
        )
        self.assertEqual(BACKOFF_LEVEL_KEYS[-1], ("tile_type",))
        for level in range(BACKOFF_LEVEL_COUNT - 1):
            self.assertGreater(
                len(BACKOFF_LEVEL_KEYS[level]),
                len(BACKOFF_LEVEL_KEYS[level + 1]),
            )


class FitAndPredictTest(unittest.TestCase):
    def test_full_key_prediction_is_the_training_bucket_mean(self) -> None:
        rows_a = (
            (4,) + (0,) * 33,
            (0, 4) + (0,) * 32,
            (0, 0, 4) + (0,) * 31,
        )
        rows_b = (
            (2,) + (0,) * 33,
            (0, 2) + (0,) * 32,
            (0, 0, 2) + (0,) * 31,
        )
        estimator = BucketedExpectedCountEstimator.fit(
            (
                _uniform_sample(anchor_index=0, label_counts=_pad(rows_a)),
                _uniform_sample(anchor_index=1, label_counts=_pad(rows_b)),
            )
        )

        prediction = estimator.predict(_feature(tile_index=0))

        self.assertEqual(prediction.backoff_level, 0)
        self.assertEqual(prediction.expected_count, 3.0)

    def test_prediction_stays_inside_the_semantic_zero_to_four_range(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        for tile_index in range(34):
            value = estimator.predict(_feature(tile_index=tile_index)).expected_count
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 4.0)

    def test_unseen_full_key_falls_back_in_the_locked_order(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit(
            (_uniform_sample(riichi=RiichiState.NONE),)
        )

        fallback = estimator.predict(_feature(riichi=RiichiState.ACCEPTED))

        self.assertEqual(fallback.backoff_level, 1)

    def test_unseen_opponent_wind_backs_off_past_the_wind_keyed_levels(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        fallback = estimator.predict(
            _feature(viewer_wind=Wind.SOUTH, opponent_wind=Wind.EAST)
        )

        self.assertEqual(fallback.backoff_level, 2)

    def test_unseen_turn_bucket_backs_off_to_the_meld_level(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        fallback = estimator.predict(_feature(turn=TurnBucket.LATE))

        self.assertEqual(fallback.backoff_level, 3)

    def test_unseen_remaining_count_backs_off_to_the_tile_level(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        fallback = estimator.predict(_feature(remaining=1))

        self.assertEqual(fallback.backoff_level, 5)

    def test_fit_requires_at_least_one_training_cell(self) -> None:
        with self.assertRaises(Phase05EstimatorError):
            BucketedExpectedCountEstimator.fit(())

    def test_fit_rejects_non_samples(self) -> None:
        with self.assertRaises(TypeError):
            BucketedExpectedCountEstimator.fit((object(),))

    def test_predict_sample_reports_backoff_level_usage(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        rows, level_counts = estimator.predict_sample(_uniform_sample())

        self.assertEqual(len(rows), 3)
        self.assertEqual(len(rows[0]), 34)
        self.assertEqual(sum(level_counts), 102)
        self.assertEqual(level_counts[0], 102)

    def test_training_cell_counts_are_reported_per_level(self) -> None:
        estimator = BucketedExpectedCountEstimator.fit((_uniform_sample(),))

        counts = estimator.training_cell_counts

        self.assertEqual(len(counts), BACKOFF_LEVEL_COUNT)
        self.assertEqual(counts[0], 102)
        self.assertEqual(counts[-1], 34)


def _pad(rows: tuple[tuple[int, ...], ...]) -> tuple[tuple[int, ...], ...]:
    """label rowの合計が13枚になるようhonor slotで埋める。"""
    padded = []
    for row in rows:
        values = list(row)
        index = 33
        while sum(values) < 13:
            values[index] = min(4, 13 - sum(values) + values[index])
            index -= 1
        padded.append(tuple(values))
    return tuple(padded)


if __name__ == "__main__":
    unittest.main()
