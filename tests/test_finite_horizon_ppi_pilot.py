"""Issue #279 bounded PPI pilot tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from lisjong.policy_contract import DiscardAction, Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType

from lisjong_arena.finite_horizon_ppi_pilot import experiment
from lisjong_arena.finite_horizon_ppi_pilot.protocol import (
    CALIBRATION_SEEDS,
    create_protocol_document,
    validate_protocol_document,
)
from lisjong_arena.finite_horizon_ppi_pilot.statistics import mean_inference
from lisjong_arena.finite_horizon_ppi_pilot.synthetic import (
    run_synthetic_validation,
    synthetic_validation_passes,
)


class MeanInferenceTest(unittest.TestCase):
    def test_lambda_zero_recovers_reference_only(self) -> None:
        result = mean_inference(
            [10.0, -10.0, 8.0, -8.0],
            [-3.0, -1.0, 1.0, 3.0],
            [3.0, 1.0, -1.0, -3.0],
        )
        plus = result["ppi_plus"]
        reference = result["reference_only"]
        self.assertEqual(plus["lambda"], 0.0)
        self.assertAlmostEqual(plus["estimate"], reference["estimate"])
        self.assertAlmostEqual(plus["ci_width"], reference["ci_width"])

    def test_basic_ppi_corrects_constant_prediction_bias(self) -> None:
        result = mean_inference(
            [6.0, 7.0, 8.0, 9.0],
            [6.0, 7.0, 8.0, 9.0],
            [1.0, 2.0, 3.0, 4.0],
        )
        self.assertAlmostEqual(result["cheap_only_mean"], 7.5)
        self.assertAlmostEqual(result["ppi"]["estimate"], 2.5)


class SyntheticValidationTest(unittest.TestCase):
    def test_locked_four_cases_pass_with_monte_carlo_derived_tolerance(self) -> None:
        summary = run_synthetic_validation(500)
        self.assertTrue(synthetic_validation_passes(summary))
        self.assertEqual(
            set(summary["cases"]),
            {
                "informative",
                "biased_informative",
                "uninformative",
                "negative_pathological",
            },
        )
        for case in summary["cases"].values():
            self.assertGreater(case["monte_carlo_standard_error"], 0.0)


class ProtocolTest(unittest.TestCase):
    def test_lock_uses_disjoint_samples_and_geometric_prefixes(self) -> None:
        synthetic = run_synthetic_validation(500)
        timing = [
            {
                "seed": seed,
                "source_generation_seconds": 1.0,
                "candidate_selection_seconds": 0.1,
                "cheap_evaluation_seconds": 0.2,
                "reference_evaluation_seconds": 0.7,
                "unlabeled_total_seconds": 1.3,
                "labeled_total_seconds": 2.0,
            }
            for seed in CALIBRATION_SEEDS
        ]
        document = create_protocol_document(
            timing_rows=timing,
            synthetic_validation=synthetic,
            total_compute_budget_seconds=1_000.0,
            provenance={
                "execution_environment": "test",
                "python_version": "3.14.0",
                "riichienv_version": "0.4.10",
            },
            arena_revision="a" * 40,
            cheap_multiplier=4,
        )
        validated = validate_protocol_document(document)
        u = validated["U_seed_specification"]
        l_spec = validated["L_seed_specification"]
        self.assertLess(u["end"], l_spec["start"])
        u_range = set(range(u["start"], u["end"] + 1))
        self.assertFalse(set(CALIBRATION_SEEDS) & u_range)
        grid = validated["labeled_budget_grid"]
        self.assertEqual(grid, sorted(set(grid)))
        self.assertEqual(grid[-1], l_spec["count"])
        self.assertEqual(validated["cheap_sample_size"], 4 * l_spec["count"])


class ExperimentIsolationTest(unittest.TestCase):
    def test_unlabeled_path_never_computes_h3(self) -> None:
        m3 = DiscardAction(Seat.SEAT_0, Tile(TileType(TileCategory.MANZU, 3)), False)
        m4 = DiscardAction(Seat.SEAT_0, Tile(TileType(TileCategory.MANZU, 4)), False)
        observation = SimpleNamespace(
            decision_trace=SimpleNamespace(
                legal_actions=(m3, m4),
                selected_action=m3,
            ),
            policy_input=object(),
        )
        inspection = SimpleNamespace(
            step_observations=(SimpleNamespace(seat_decisions=(observation,)),)
        )
        horizons: list[int] = []

        def fake_delta(*args, horizon: int, **kwargs) -> float:
            horizons.append(horizon)
            return 0.1

        with (
            mock.patch.object(
                experiment, "_source_inspection", return_value=(inspection, 0.0)
            ),
            mock.patch.object(experiment, "DecisionContext", return_value=object()),
            mock.patch.object(
                experiment.HandValueAwareTwoStepUkeirePolicy,
                "choose_action",
                return_value=m4,
            ),
            mock.patch.object(
                experiment,
                "_completion_probability_delta",
                side_effect=fake_delta,
            ),
        ):
            measured = experiment.measure_hanchan(123, include_reference=False)

        self.assertEqual(horizons, [experiment.CHEAP_HORIZON])
        self.assertIsNone(measured.reference_score)
        self.assertEqual(measured.eligible_decision_count, 1)


if __name__ == "__main__":
    unittest.main()
