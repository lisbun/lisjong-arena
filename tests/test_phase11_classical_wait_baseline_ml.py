"""Synthetic ML tests for Arena #222's locked TRAIN-only convex fit."""

import importlib.util
import math
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_engine.wind import Wind
from test_phase11_classical_wait_baseline import _other_target, _snapshot, _target

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_classical_wait_baseline.artifact import (
    load_model,
    save_model,
)
from lisjong_arena.phase11_classical_wait_baseline.data import ClassicalExample
from lisjong_arena.phase11_classical_wait_baseline.lock import (
    _runtime,
    validate_runtime,
)
from lisjong_arena.phase11_classical_wait_baseline.model import (
    fit_offset_logistic,
    predict_probability,
)
from lisjong_arena.phase11_classical_wait_baseline.protocol import (
    FEATURE_DIM,
    ClassicalWaitError,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _train_record(seed: int, candidate_unseen: int, positive_tile: int):
    remaining = list((4,) * 34)
    remaining[positive_tile] = candidate_unseen
    mask = [0] * 34
    mask[positive_tile] = 1
    return ClassicalExample(
        partition=DatasetPartition.TRAIN,
        source_class="fixture",
        game_seed=seed,
        round_index=0,
        anchor_identity=f"{seed:064x}",
        depth=1,
        snapshot=_snapshot(remaining=tuple(remaining)),
        targets=(
            _target(mask=tuple(mask), junme=4 + seed),
            _other_target(Wind.SOUTH),
            _other_target(Wind.WEST),
        ),
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class RuntimeContractTest(unittest.TestCase):
    def test_runtime_normalizes_torch_version_to_plain_string(self):
        runtime = _runtime()
        self.assertIs(type(runtime["torch"]), str)
        self.assertTrue(runtime["torch"].startswith("2.13.0"))
        self.assertIs(validate_runtime(runtime), runtime)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class DeterministicFitTest(unittest.TestCase):
    def setUp(self):
        self.records = (
            _train_record(1, 4, 4),
            _train_record(2, 3, 4),
            _train_record(3, 2, 13),
            _train_record(4, 1, 22),
        )
        positives = [0] * 34
        for record in self.records:
            for target in record.targets:
                if target.eligible:
                    for index, label in enumerate(target.mask):
                        positives[index] += label
        rows = 4
        self.baseline = {
            "fit_partition": "train",
            "eligible_rows": rows,
            "positive_cells": sum(positives),
            "per_tile_positives": positives,
            "probabilities": [
                (positive + 0.5) / (rows + 1.0) for positive in positives
            ],
        }
        self.lock_identity = "a" * 64

    def test_fit_is_deterministic_with_zero_intercept_and_train_only_input(self):
        first, first_summary = fit_offset_logistic(
            self.records,
            self.baseline,
            execution_lock_identity=self.lock_identity,
        )
        second, second_summary = fit_offset_logistic(
            self.records,
            self.baseline,
            execution_lock_identity=self.lock_identity,
        )
        self.assertEqual(first, second)
        self.assertEqual(first_summary, second_summary)
        self.assertEqual(len(first["weights"]), FEATURE_DIM)
        self.assertFalse(first["architecture"]["free_intercept"])
        self.assertEqual(first["architecture"]["hidden_layers"], [])
        self.assertFalse(first["architecture"]["learned_embedding"])
        self.assertFalse(first["architecture"]["e160_latent"])
        self.assertLessEqual(
            first["final_train_log_loss"], first["initial_train_log_loss"] + 1e-12
        )
        self.assertTrue(all(math.isfinite(value) for value in first["weights"]))

    def test_validation_record_is_rejected_by_fit(self):
        validation = (replace(self.records[0], partition=DatasetPartition.VALIDATION),)
        with self.assertRaisesRegex(ClassicalWaitError, "TRAIN records only"):
            fit_offset_logistic(
                validation,
                self.baseline,
                execution_lock_identity=self.lock_identity,
            )

    def test_persisted_model_is_self_identifying_and_tamper_evident(self):
        model, _summary = fit_offset_logistic(
            self.records,
            self.baseline,
            execution_lock_identity=self.lock_identity,
        )
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "classical-model"
            save_model(
                destination,
                model,
                self.lock_identity,
                self.baseline,
            )
            loaded = load_model(
                destination,
                self.lock_identity,
                self.baseline,
            )
            self.assertEqual(loaded, model)

            path = destination / "model.json"
            payload = path.read_text(encoding="utf-8")
            path.write_text(
                payload.replace(
                    '"initial_train_log_loss":',
                    '"initial_train_log_loss":0.123,"tampered":',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises((ClassicalWaitError, ValueError)):
                load_model(
                    destination,
                    self.lock_identity,
                    self.baseline,
                )

    def test_predictions_remain_finite_and_strictly_inside_unit_interval(self):
        model, _summary = fit_offset_logistic(
            self.records,
            self.baseline,
            execution_lock_identity=self.lock_identity,
        )
        for probability in self.baseline["probabilities"]:
            predicted = predict_probability(
                probability,
                model["weights"],
                (0.5,) * FEATURE_DIM,
            )
            self.assertTrue(math.isfinite(predicted))
            self.assertGreater(predicted, 0.0)
            self.assertLess(predicted, 1.0)


if __name__ == "__main__":
    unittest.main()
