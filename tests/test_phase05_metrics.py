"""lisjong-project #22 Phase 0.5 prediction metrics tests。"""

import unittest

from lisjong_arena.phase05_belief_slice.metrics import (
    CONSERVATION_VIOLATION_TOLERANCE,
    evaluate_predictions,
)
from tests import _phase05_fixtures as fixtures

_TILE_TYPE_COUNT = 34


def _row(value: float) -> tuple[float, ...]:
    return (value,) * _TILE_TYPE_COUNT


def _label_row(index: int) -> tuple[int, ...]:
    values = [0] * _TILE_TYPE_COUNT
    values[index] = 4
    values[index + 1] = 4
    values[index + 2] = 4
    values[index + 3] = 1
    return tuple(values)


class PredictionMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sample = fixtures.sample(
            label_counts=(_label_row(0), _label_row(4), _label_row(8)),
        )

    def test_exact_prediction_scores_zero_error(self) -> None:
        metrics = evaluate_predictions(
            (self.sample,),
            (
                tuple(
                    tuple(float(count) for count in row)
                    for row in self.sample.labels.counts
                ),
            ),
        )

        self.assertEqual(metrics.sample_count, 1)
        self.assertEqual(metrics.opponent_row_count, 3)
        self.assertEqual(metrics.cell_count, 102)
        self.assertEqual(metrics.per_tile_mae, 0.0)
        self.assertEqual(metrics.per_hand_l1, 0.0)
        self.assertEqual(metrics.concealed_size_mean_inconsistency, 0.0)
        self.assertEqual(metrics.conservation_violation_samples, 0)

    def test_per_tile_mae_and_per_hand_l1_are_consistent(self) -> None:
        metrics = evaluate_predictions(
            (self.sample,),
            ((_row(0.0), _row(0.0), _row(0.0)),),
        )

        self.assertEqual(metrics.per_hand_l1, 13.0)
        self.assertAlmostEqual(metrics.per_tile_mae, 13.0 / _TILE_TYPE_COUNT)

    def test_concealed_size_inconsistency_uses_public_meld_count(self) -> None:
        features = fixtures.anchor_features(opponent_meld_counts=(1, 0, 0))
        sample = fixtures.sample(
            features=features,
            label_counts=(
                fixtures.row(),
                fixtures.row(thirteen_of=4),
                fixtures.row(thirteen_of=8),
            ),
        )

        metrics = evaluate_predictions(
            (sample,),
            ((_row(10.0 / _TILE_TYPE_COUNT), _row(13.0 / 34), _row(13.0 / 34)),),
        )

        self.assertAlmostEqual(metrics.concealed_size_mean_inconsistency, 0.0)

    def test_conservation_excess_counts_only_positive_overflow(self) -> None:
        features = fixtures.anchor_features(
            remaining_tile_counts=(2,) + (4,) * (_TILE_TYPE_COUNT - 1)
        )
        sample = fixtures.sample(features=features)
        prediction_row = (1.5,) + (0.0,) * (_TILE_TYPE_COUNT - 1)

        metrics = evaluate_predictions(
            (sample,),
            ((prediction_row, prediction_row, prediction_row),),
        )

        self.assertEqual(metrics.conservation_violation_samples, 1)
        self.assertEqual(metrics.conservation_violation_rate, 1.0)
        self.assertAlmostEqual(metrics.conservation_total_excess, 2.5)
        self.assertAlmostEqual(metrics.conservation_mean_excess, 2.5)

    def test_fixed_point_rounding_noise_is_not_counted_as_a_violation(self) -> None:
        features = fixtures.anchor_features(
            remaining_tile_counts=(3,) + (4,) * (_TILE_TYPE_COUNT - 1)
        )
        sample = fixtures.sample(features=features)
        noise = CONSERVATION_VIOLATION_TOLERANCE / 4.0
        prediction_row = (1.0 + noise,) + (0.0,) * (_TILE_TYPE_COUNT - 1)

        metrics = evaluate_predictions(
            (sample,),
            ((prediction_row, prediction_row, prediction_row),),
        )

        self.assertEqual(metrics.conservation_violation_samples, 0)
        self.assertGreater(metrics.conservation_total_excess, 0.0)

    def test_prediction_shape_mismatch_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_predictions((self.sample,), ((_row(0.0), _row(0.0)),))
        with self.assertRaises(ValueError):
            evaluate_predictions(
                (self.sample,),
                (((0.0,) * 33, _row(0.0), _row(0.0)),),
            )

    def test_sample_and_prediction_counts_must_match(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_predictions((self.sample,), ())

    def test_empty_input_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_predictions((), ())


if __name__ == "__main__":
    unittest.main()
