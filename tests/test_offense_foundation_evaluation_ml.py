"""PyTorch-facing tests for #331 frozen offense serving."""

import importlib.util
import unittest
from pathlib import Path

from lisjong_arena.offense_foundation.fixtures import probes
from lisjong_arena.offense_foundation.learner import LoadedOffenseCheckpoint
from lisjong_arena.offense_foundation.serving import (
    create_serving_runtime,
    infer_decision,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class OffenseServingTest(unittest.TestCase):
    def _checkpoint(self):
        from lisjong_arena.learned_policy_stage2.network import create_model

        model = create_model()
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)
        return LoadedOffenseCheckpoint(
            path=Path("."),
            manifest={
                "checkpoint_identity": "d" * 64,
                "scientific_corpus_identity": "c" * 64,
            },
            model=model,
        )

    def test_inference_selects_only_a_current_legal_action(self):
        checkpoint = self._checkpoint()
        runtime = create_serving_runtime(checkpoint)
        context = next(
            probe.context
            for probe in probes()
            if probe.name == "maximum_current_ukeire"
        )
        result = infer_decision(runtime.model, context)
        self.assertIn(result.action, context.legal_actions)
        self.assertEqual(result.log_probabilities.shape, (802,))

    def test_policy_uses_the_same_inference_path(self):
        checkpoint = self._checkpoint()
        runtime = create_serving_runtime(checkpoint)
        policy = runtime.create_policy()
        context = next(
            probe.context for probe in probes() if probe.name == "riichi_before_discard"
        )
        selected = policy.choose_action(context)
        self.assertIn(selected, context.legal_actions)
        self.assertEqual(policy.decisions, 1)


if __name__ == "__main__":
    unittest.main()
