"""Focused Torch tests for #262 checkpoint -> Stage 3 serving handoff."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from lisjong_arena.learned_policy_offline_q.artifact import (
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_stage2.network import create_model
from lisjong_arena.learned_policy_stage3.artifact import load_serving_checkpoint
from lisjong_arena.learned_policy_stage3.protocol import ArtifactClass
from lisjong_arena.stage_a0_tenpai_execution import training as stage_training
from lisjong_arena.stage_a0_tenpai_execution.protocol import (
    EXPECTED_PUBLIC_KEYS_IDENTITY,
    EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY,
    Arm,
)
from lisjong_arena.stage_a0_tenpai_execution.training import (
    EpochRecord,
    TrainingResult,
    load_checkpoint,
    save_checkpoint,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class StageA0CheckpointServingTest(unittest.TestCase):
    def _scientific(self):
        return SimpleNamespace(
            lock_b={
                "lock_identity": (
                    "a7f1c471a8c2f983c5efab9147651eb48b8788eecb1aa296222e21fd9418e93e"
                )
            },
            dataset=SimpleNamespace(
                identity=locked.RETAINED_DATASET_IDENTITY,
                manifest={
                    "feature": feature_block(),
                    "vocabulary": vocabulary_block(),
                },
            ),
            sidecar=SimpleNamespace(identity=EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY),
            public_keys=SimpleNamespace(identity=EXPECTED_PUBLIC_KEYS_IDENTITY),
        )

    def _result(self, arm: Arm) -> TrainingResult:
        import torch

        torch.manual_seed(0)
        model = create_model()
        auxiliary = torch.nn.Linear(locked.HIDDEN_WIDTH, 3) if arm is Arm.T else None
        return TrainingResult(
            arm=arm,
            seed=0,
            model=model,
            auxiliary_head=auxiliary,
            initial_policy_fingerprint="3" * 64,
            history=(
                EpochRecord(
                    epoch=1,
                    train_policy_ce=1.0,
                    train_auxiliary_bce=0.5 if arm is Arm.T else None,
                    validation_policy_ce=0.9,
                ),
            ),
            selected_epoch=1,
            selected_validation_policy_ce=0.9,
            train_policy_ce=1.0,
            train_auxiliary_bce=0.5 if arm is Arm.T else None,
            wall_clock_seconds=0.1,
            runtime={
                "torch_version": torch.__version__,
                "torch_threads": 1,
                "deterministic_algorithms": True,
                "cuda_available": False,
                "python_version": "3.14.6",
            },
        )

    @staticmethod
    def _legacy_serialization_fingerprint(state) -> str:
        import torch

        stream = io.BytesIO()
        torch.save(state, stream)
        return hashlib.sha256(stream.getvalue()).hexdigest()

    def test_semantic_fingerprint_survives_strict_load_when_torch_save_bytes_drift(
        self,
    ) -> None:
        import torch

        torch.manual_seed(0)
        source = create_model()
        initial_state = stage_training._clone_state(source.state_dict())
        loaded = create_model()
        loaded.load_state_dict(
            stage_training._clone_state(initial_state),
            strict=True,
        )

        stage_training._assert_states_equal(
            initial_state,
            loaded.state_dict(),
            label="regression fixture",
        )
        self.assertEqual(
            stage_training._state_fingerprint(initial_state),
            stage_training._state_fingerprint(loaded.state_dict()),
        )
        self.assertNotEqual(
            self._legacy_serialization_fingerprint(initial_state),
            self._legacy_serialization_fingerprint(loaded.state_dict()),
        )

    def test_semantic_fingerprint_binds_dtype_shape_and_tensor_bytes(self) -> None:
        import torch

        float_state = {"x": torch.tensor([1.0], dtype=torch.float32)}
        same_raw_bytes = {
            "x": torch.tensor([0, 0, 128, 63], dtype=torch.uint8),
        }
        reshaped = {"x": torch.tensor([[1.0]], dtype=torch.float32)}

        self.assertNotEqual(
            stage_training._state_fingerprint(float_state),
            stage_training._state_fingerprint(same_raw_bytes),
        )
        self.assertNotEqual(
            stage_training._state_fingerprint(float_state),
            stage_training._state_fingerprint(reshaped),
        )

    def test_train_seed_pair_crosses_real_initialization_equality_boundary(
        self,
    ) -> None:
        observed = {}

        def stop_before_training(
            arm,
            seed,
            model,
            tensors,
            *,
            initial_fingerprint,
        ):
            self.assertEqual(seed, 0)
            self.assertEqual(tensors, {})
            observed[arm] = stage_training._clone_state(model.state_dict())
            return SimpleNamespace(initial_policy_fingerprint=initial_fingerprint)

        with mock.patch.object(
            stage_training,
            "_train_arm_from_initial_model",
            side_effect=stop_before_training,
        ) as patched:
            a, t = stage_training.train_seed_pair(0, {})

        self.assertEqual(patched.call_count, 2)
        self.assertEqual(a.initial_policy_fingerprint, t.initial_policy_fingerprint)
        stage_training._assert_states_equal(
            observed[Arm.A],
            observed[Arm.T],
            label="regression A/T initialization",
        )
        self.assertEqual(
            stage_training._state_fingerprint(observed[Arm.A]),
            a.initial_policy_fingerprint,
        )

    def test_t_checkpoint_strict_loads_and_serves_as_policy_only(self) -> None:
        import torch

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t"
            saved = save_checkpoint(path, self._scientific(), self._result(Arm.T))
            loaded = load_checkpoint(path)
            self.assertEqual(saved.identity, loaded.identity)
            self.assertIsNotNone(loaded.auxiliary_head)

            serving = load_serving_checkpoint(path)
            self.assertIs(serving.artifact_class, ArtifactClass.STAGE_A0_TENPAI)
            self.assertEqual(serving.identity, loaded.identity)
            with torch.inference_mode():
                logits = serving.model(torch.zeros((1, locked.FEATURE_DIMENSION)))
            self.assertEqual(tuple(logits.shape), (1, locked.VOCABULARY_SIZE))


if __name__ == "__main__":
    unittest.main()
