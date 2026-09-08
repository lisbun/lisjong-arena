"""Issue #183 P6 Gate B focused ML tests."""

import importlib.util
import unittest
from pathlib import Path

from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import create_p1_model
from lisjong_arena.learned_policy_offline_q.p6_conservative_q import LoadedP6Checkpoint
from lisjong_arena.learned_policy_offline_q.p6_gate_b import (
    DEFAULT_ORDERED_SEEDS,
    EXPECTED_CANDIDATE_IDENTITY,
    build_gate_b_plan,
)
from lisjong_arena.learned_policy_offline_q.serving import HybridPolicy

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


def _checkpoint():
    model = create_p1_model()
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return LoadedP6Checkpoint(
        path=Path("fixture-p6"),
        manifest={"candidate_identity": EXPECTED_CANDIDATE_IDENTITY},
        model=model,
        supported_indices=frozenset(range(802)),
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class P6GateBServingTest(unittest.TestCase):
    def test_plan_reuses_p1_hybrid_serving_and_passive_comparator(self):
        plan, registry = build_gate_b_plan(_checkpoint())
        self.assertEqual(plan.candidate_identity, EXPECTED_CANDIDATE_IDENTITY)
        self.assertEqual(plan.baseline_identity, PASSIVE_TSUMOGIRI_IDENTITY)
        self.assertEqual(plan.seeds, DEFAULT_ORDERED_SEEDS)
        policy = registry.create_policy()
        self.assertIsInstance(policy, HybridPolicy)

    def test_each_candidate_policy_instance_is_fresh(self):
        _plan, registry = build_gate_b_plan(_checkpoint())
        first = registry.create_policy()
        second = registry.create_policy()
        self.assertIsNot(first, second)
        self.assertIsNot(first._scaffold, second._scaffold)
        self.assertIs(first._runtime.model, second._runtime.model)

    def test_checkpoint_model_is_reused_without_retraining(self):
        checkpoint = _checkpoint()
        _plan, registry = build_gate_b_plan(checkpoint)
        policy = registry.create_policy()
        self.assertIs(policy._runtime.model, checkpoint.model)


if __name__ == "__main__":
    unittest.main()
