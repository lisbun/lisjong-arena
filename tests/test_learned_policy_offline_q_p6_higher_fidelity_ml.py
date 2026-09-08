"""Issue #185 focused ML serving-construction tests."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from test_learned_policy_offline_q_p6_higher_fidelity import _lock

from lisjong_arena.learned_policy_offline_q.p1_q_training import create_p1_model
from lisjong_arena.learned_policy_offline_q.p6_conservative_q import LoadedP6Checkpoint
from lisjong_arena.learned_policy_offline_q.p6_higher_fidelity import (
    BASELINE_IDENTITY,
    DEFAULT_ORDERED_SEEDS,
    EXPECTED_CANDIDATE_IDENTITY,
    build_evaluation_plan,
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
class P6HigherFidelityServingTest(unittest.TestCase):
    def test_exact_unguarded_runtime_and_yakuhai_baseline_are_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = _lock(Path(directory))
        checkpoint = _checkpoint()
        plan, candidate_registry, baseline_registry = build_evaluation_plan(
            checkpoint, lock
        )
        self.assertEqual(plan.candidate.identity, EXPECTED_CANDIDATE_IDENTITY)
        self.assertEqual(plan.baseline.identity, BASELINE_IDENTITY)
        self.assertEqual(plan.seeds, DEFAULT_ORDERED_SEEDS)

        first_candidate = candidate_registry.create_policy()
        second_candidate = candidate_registry.create_policy()
        self.assertIsInstance(first_candidate, HybridPolicy)
        self.assertIsNot(first_candidate, second_candidate)
        self.assertIsNot(first_candidate._scaffold, second_candidate._scaffold)
        self.assertIs(first_candidate._runtime.model, checkpoint.model)

        first_baseline = baseline_registry.create_policy()
        second_baseline = baseline_registry.create_policy()
        self.assertIsNot(first_baseline, second_baseline)


if __name__ == "__main__":
    unittest.main()
