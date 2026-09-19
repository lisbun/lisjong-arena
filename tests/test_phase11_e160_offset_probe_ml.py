"""Synthetic ML tests for Arena #291's frozen-E160 offset probe."""

import importlib.util
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_e160_offset_probe.artifact import (
    load_model,
    save_model,
)
from lisjong_arena.phase11_e160_offset_probe.data import (
    centering_receipt,
    latent_fingerprint,
)
from lisjong_arena.phase11_e160_offset_probe.evaluation import fit_train_prevalence
from lisjong_arena.phase11_e160_offset_probe.model import (
    fit_probe,
    predict_probability,
)
from lisjong_arena.phase11_e160_offset_probe.protocol import (
    LATENT_DIM,
    PARAMETER_COUNT,
    E160OffsetProbeError,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    LatentExample,
    OpponentTarget,
)
from lisjong_arena.phase11_public_riichi_wait_readout.model import freeze_e160

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _target(row: int, positive: bool) -> OpponentTarget:
    mask = [0] * 34
    if positive:
        mask[0] = 1
    return OpponentTarget(
        wind=("south", "west", "north")[row],
        seat=row + 1,
        established=True,
        mask=tuple(mask),
        unavailable_reason=None,
        riichi_junme=6,
        open_closed="closed",
    )


def _record(seed: int, latent_sign: float, positive: bool, *, validation=False):
    import torch

    latent = torch.zeros(LATENT_DIM, dtype=torch.float32)
    latent[0] = latent_sign
    return LatentExample(
        partition=(
            DatasetPartition.VALIDATION
            if validation
            else DatasetPartition.TRAIN
        ),
        source_class="fixture",
        game_seed=seed,
        round_index=0,
        anchor_identity=f"{seed:064x}",
        depth=1,
        opponent_winds=("south", "west", "north"),
        latent=latent,
        targets=tuple(_target(row, positive) for row in range(3)),
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class DeterministicProbeFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global torch
        import torch

    def setUp(self):
        self.records = (
            _record(1, -1.0, False),
            _record(2, -1.0, True),
            _record(3, 1.0, False),
            _record(4, 1.0, True),
        )
        self.baseline = fit_train_prevalence(self.records)
        self.centering = centering_receipt(self.records)
        self.fingerprint = latent_fingerprint(self.records)
        self.lock_identity = "a" * 64

    def _frozen(self):
        model = torch.nn.Linear(2, 2)
        snapshot = freeze_e160(model)
        return model, snapshot

    def _fit(self):
        frozen, snapshot = self._frozen()
        return fit_probe(
            self.records,
            self.baseline,
            self.centering,
            execution_lock_identity=self.lock_identity,
            latent_fingerprint=self.fingerprint,
            frozen_model=frozen,
            frozen_snapshot=snapshot,
        )

    def test_fit_is_deterministic_bias_free_and_exact_shape(self):
        first = self._fit()
        second = self._fit()
        self.assertEqual(first, second)
        self.assertEqual(first["architecture"]["parameter_count"], PARAMETER_COUNT)
        self.assertEqual(first["architecture"]["weight_shape"], [3, 34, 128])
        self.assertFalse(first["architecture"]["free_intercept"])
        self.assertEqual(first["architecture"]["hidden_layers"], [])
        self.assertEqual(len(first["weights"]), 3)
        self.assertEqual(len(first["weights"][0]), 34)
        self.assertEqual(len(first["weights"][0][0]), 128)
        self.assertLessEqual(
            first["final_train_log_loss"],
            first["initial_train_log_loss"] + 1e-12,
        )

    def test_validation_records_are_rejected_by_fit(self):
        validation = tuple(
            _record(index + 10, 1.0, bool(index % 2), validation=True)
            for index in range(4)
        )
        frozen, snapshot = self._frozen()
        with self.assertRaisesRegex(E160OffsetProbeError, "TRAIN records only"):
            fit_probe(
                validation,
                self.baseline,
                self.centering,
                execution_lock_identity=self.lock_identity,
                latent_fingerprint=self.fingerprint,
                frozen_model=frozen,
                frozen_snapshot=snapshot,
            )

    def test_model_artifact_is_self_identifying_and_tamper_evident(self):
        model = self._fit()
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "model"
            save_model(
                destination,
                model,
                self.lock_identity,
                self.baseline,
                self.centering,
                self.fingerprint,
            )
            loaded = load_model(
                destination,
                self.lock_identity,
                self.baseline,
                self.centering,
                self.fingerprint,
            )
            self.assertEqual(loaded, model)
            path = destination / "model.json"
            data = path.read_bytes()
            path.write_bytes(data.replace(b'"train_cells":408', b'"train_cells":409'))
            with self.assertRaises(E160OffsetProbeError):
                load_model(
                    destination,
                    self.lock_identity,
                    self.baseline,
                    self.centering,
                    self.fingerprint,
                )

    def test_predictions_are_finite_and_inside_unit_interval(self):
        model = self._fit()
        centered = [0.5] * LATENT_DIM
        for probability in self.baseline["probabilities"]:
            predicted = predict_probability(
                probability,
                model["weights"][0][0],
                centered,
            )
            self.assertTrue(math.isfinite(predicted))
            self.assertGreater(predicted, 0.0)
            self.assertLess(predicted, 1.0)


if __name__ == "__main__":
    unittest.main()
