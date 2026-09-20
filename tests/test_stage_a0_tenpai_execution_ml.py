"""Focused Torch tests for #262 checkpoint -> Stage 3 serving handoff."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch

from lisjong_arena.learned_policy_offline_q.artifact import feature_block, vocabulary_block
from lisjong_arena.learned_policy_stage2.network import create_model
from lisjong_arena.learned_policy_stage3.artifact import load_serving_checkpoint
from lisjong_arena.learned_policy_stage3.protocol import ArtifactClass
from lisjong_arena.stage_a0_tenpai_execution.protocol import Arm
from lisjong_arena.stage_a0_tenpai_execution.training import (
    EpochRecord,
    TrainingResult,
    load_checkpoint,
    save_checkpoint,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked


class StageA0CheckpointServingTest(unittest.TestCase):
    def _scientific(self):
        return SimpleNamespace(
            lock_b={"lock_identity": (
                "a7f1c471a8c2f983c5efab9147651eb48b8788eecb1aa296222e21fd9418e93e"
            )},
            dataset=SimpleNamespace(
                identity=locked.RETAINED_DATASET_IDENTITY,
                manifest={
                    "feature": feature_block(),
                    "vocabulary": vocabulary_block(),
                },
            ),
            sidecar=SimpleNamespace(identity="1" * 64),
            public_keys=SimpleNamespace(identity="2" * 64),
        )

    def _result(self, arm: Arm) -> TrainingResult:
        torch.manual_seed(0)
        model = create_model()
        auxiliary = (
            torch.nn.Linear(locked.HIDDEN_WIDTH, 3) if arm is Arm.T else None
        )
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

    def test_t_checkpoint_strict_loads_and_serves_as_policy_only(self) -> None:
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
