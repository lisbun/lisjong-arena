"""BC hybrid / Q hybrid serving Policy tests (Issue #140)."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset
from _learned_policy_offline_q_fixtures import (
    eligible_discard_decision,
    forced_discard_decision,
    make_round_state,
    riichi_choice_decision,
)
from lisjong.policy_contract import DecisionContext, Seat, Wind

from lisjong_arena.learned_policy_offline_q.bc_training import (
    save_checkpoint as save_bc_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint as save_q_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.q_training import train_q_model
from lisjong_arena.learned_policy_offline_q.serving import (
    HybridServingError,
    create_bc_hybrid_runtime,
    create_q_hybrid_runtime,
)

_SUPPORTED = frozenset({0, 1, 2, 3})


def _context(observation) -> DecisionContext:
    return DecisionContext(
        input=observation.policy_input,
        legal_actions=observation.decision_trace.legal_actions,
    )


class HybridServingTestBase:
    arm_name: str

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)

    def _bc_runtime(self, supported=_SUPPORTED):
        run = train_bc_model(self.dataset)
        checkpoint = save_bc_checkpoint(self._tmp / "bc-checkpoint", self.dataset, run)
        return create_bc_hybrid_runtime(checkpoint.path, supported_indices=supported)

    def _q_runtime(self, supported=_SUPPORTED):
        run = train_q_model(self.dataset)
        checkpoint = save_q_checkpoint(self._tmp / "q-checkpoint", self.dataset, run)
        return create_q_hybrid_runtime(checkpoint.path, supported_indices=supported)

    def _runtime(self):
        return self._bc_runtime() if self.arm_name == "bc" else self._q_runtime()

    def test_eligible_and_support_complete_decision_uses_the_learned_model(self):
        runtime = self._runtime()
        policy = runtime.create_policy()
        round_state = make_round_state(Wind.EAST, 1)
        observation = eligible_discard_decision(
            Seat.SEAT_0, round_state, (25000,) * 4, legal_ranks=(1, 2), selected_rank=1
        )
        decision = _context(observation)
        action = policy.choose_action(decision)
        self.assertIn(action, decision.legal_actions)
        self.assertEqual(policy.activation_count, 1)
        self.assertEqual(policy.scaffold_fallback_count, 0)
        self.assertEqual(policy.support_fallback_count, 0)

    def test_forced_decision_falls_back_to_the_scaffold(self):
        runtime = self._runtime()
        policy = runtime.create_policy()
        round_state = make_round_state(Wind.EAST, 1)
        observation = forced_discard_decision(
            Seat.SEAT_0, round_state, (25000,) * 4, rank=1
        )
        decision = _context(observation)
        action = policy.choose_action(decision)
        self.assertIn(action, decision.legal_actions)
        self.assertEqual(policy.activation_count, 0)
        self.assertEqual(policy.scaffold_fallback_count, 1)

    def test_non_discard_choice_falls_back_to_the_scaffold(self):
        runtime = self._runtime()
        policy = runtime.create_policy()
        round_state = make_round_state(Wind.EAST, 1)
        observation = riichi_choice_decision(Seat.SEAT_0, round_state, (25000,) * 4)
        decision = _context(observation)
        action = policy.choose_action(decision)
        self.assertIn(action, decision.legal_actions)
        self.assertEqual(policy.activation_count, 0)
        self.assertEqual(policy.scaffold_fallback_count, 1)

    def test_eligible_but_unsupported_legal_action_falls_back_to_the_scaffold(self):
        runtime = self._runtime()
        policy = runtime.create_policy()
        round_state = make_round_state(Wind.EAST, 1)
        # manzu ranks 1..5 with tsumogiri=False encode to vocabulary indices
        # 0, 2, 4, 6, 8 -- 4/6/8 fall outside the {0,1,2,3} TRAIN support set.
        observation = eligible_discard_decision(
            Seat.SEAT_0,
            round_state,
            (25000,) * 4,
            legal_ranks=(1, 2, 3, 4, 5),
            selected_rank=1,
        )
        decision = _context(observation)
        action = policy.choose_action(decision)
        self.assertIn(action, decision.legal_actions)
        self.assertEqual(policy.activation_count, 0)
        self.assertEqual(policy.support_fallback_count, 1)


TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class BcHybridServingTest(HybridServingTestBase, unittest.TestCase):
    arm_name = "bc"


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class QHybridServingTest(HybridServingTestBase, unittest.TestCase):
    arm_name = "q"


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class QHybridSupportBindingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)

    def test_mismatched_expected_support_set_fails_closed(self):
        run = train_q_model(self.dataset)
        checkpoint = save_q_checkpoint(self._tmp / "q-checkpoint", self.dataset, run)
        with self.assertRaises(HybridServingError):
            create_q_hybrid_runtime(
                checkpoint.path, supported_indices=frozenset({0, 1})
            )


if __name__ == "__main__":
    unittest.main()
