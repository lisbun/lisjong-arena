"""Torch-backed tests for Issue #181 fixed P6 conservative-Q formulation."""

import importlib.util
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_p1_gate_a_fixtures import write_hand_dataset

from lisjong_arena.learned_policy_offline_q.errors import OfflineQProtocolError
from lisjong_arena.learned_policy_offline_q.p1_features import derive_all_split_tensors
from lisjong_arena.learned_policy_offline_q.p1_gate_a import P1GateARole, P1RolePopulation
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    create_p1_model,
    model_weights_digest,
)
from lisjong_arena.learned_policy_offline_q.p6_conservative_q import (
    CQL_ALPHA,
    CQL_TEMPERATURE,
    conservative_action_mask,
    cql_gap,
    load_p6_checkpoint,
    save_p6_checkpoint,
    train_p6_conservative_q,
)
from lisjong_arena.learned_policy_offline_q.p6_gate_a import evaluate_p6_role
from lisjong_arena.learned_policy_offline_q.protocol import (
    MAXIMUM_EPOCHS,
    Split,
    VOCABULARY_SIZE,
)
from lisjong_arena.learned_policy_offline_q.split_tensors import load_split_tensors
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "PyTorch optional dependency is not installed")
class P6ConservativeQTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="p6-181-tests-"))
        cls.dataset = write_hand_dataset(cls.root / "dataset", rows_per_game=2)
        cls.base_tensors = load_split_tensors(cls.dataset)
        cls.p1_tensors, cls.coverage = derive_all_split_tensors(cls.base_tensors)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_cql_gap_uses_only_current_legal_and_train_supported_actions(self):
        import torch

        q = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.float32)
        legal = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.bool)
        support = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        behavior = torch.tensor([1], dtype=torch.long)
        legal[0, 1] = True
        legal[0, 2] = True
        legal[0, 3] = True
        support[1] = True
        support[2] = True
        q[0, 1] = 1.0
        q[0, 2] = 2.0
        q[0, 3] = 1000.0

        mask = conservative_action_mask(legal, behavior, support)
        self.assertTrue(bool(mask[0, 1]))
        self.assertTrue(bool(mask[0, 2]))
        self.assertFalse(bool(mask[0, 3]))
        observed = float(cql_gap(q, legal, behavior, support)[0])
        expected = math.log(math.exp(1.0) + math.exp(2.0)) - 1.0
        self.assertAlmostEqual(observed, expected, places=6)
        self.assertEqual(CQL_ALPHA, 0.1)
        self.assertEqual(CQL_TEMPERATURE, 1.0)

    def test_behavior_action_outside_conservative_set_fails_closed(self):
        import torch

        q = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.float32)
        legal = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.bool)
        support = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        legal[0, 1] = True
        legal[0, 2] = True
        support[2] = True
        behavior = torch.tensor([1], dtype=torch.long)
        with self.assertRaises(OfflineQProtocolError):
            cql_gap(q, legal, behavior, support)

    def test_non_finite_q_fails_closed(self):
        import torch

        q = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.float32)
        legal = torch.zeros((1, VOCABULARY_SIZE), dtype=torch.bool)
        support = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        legal[0, 1] = legal[0, 2] = True
        support[1] = support[2] = True
        q[0, 2] = float("nan")
        with self.assertRaises(OfflineQProtocolError):
            cql_gap(q, legal, torch.tensor([1], dtype=torch.long), support)

    def test_training_is_deterministic_and_keeps_exact_p1_model_budget(self):
        first = train_p6_conservative_q(self.p1_tensors)
        second = train_p6_conservative_q(self.p1_tensors)
        self.assertEqual(first.selected_epoch, MAXIMUM_EPOCHS)
        self.assertEqual(second.selected_epoch, MAXIMUM_EPOCHS)
        self.assertEqual(parameter_count(first.model), P1_EXPECTED_PARAMETER_COUNT)
        self.assertEqual(
            model_weights_digest(first.model), model_weights_digest(second.model)
        )
        self.assertEqual(
            [entry.to_document() for entry in first.history],
            [entry.to_document() for entry in second.history],
        )

    def test_checkpoint_is_write_once_and_strictly_read_back(self):
        run = train_p6_conservative_q(self.p1_tensors)
        destination = self.root / "checkpoint-test"
        if destination.exists():
            shutil.rmtree(destination)
        loaded = save_p6_checkpoint(destination, self.dataset, run)
        strict = load_p6_checkpoint(destination)
        self.assertEqual(loaded.candidate_identity, strict.candidate_identity)
        self.assertEqual(
            strict.manifest["canonical_model_weights_digest"],
            model_weights_digest(run.model),
        )
        with self.assertRaises(FileExistsError):
            save_p6_checkpoint(destination, self.dataset, run)

    def test_gate_a_evaluates_candidate_and_control_on_exact_common_rows(self):
        run = train_p6_conservative_q(self.p1_tensors)
        split = Split.TEST
        population = P1RolePopulation(
            role=P1GateARole.DATASET_TEST,
            tensors=self.base_tensors[split],
            p1_tensors=self.p1_tensors[split],
            rows=tuple(
                self.dataset.rows[index]
                for index in self.base_tensors[split].row_indices
            ),
            coverage=self.coverage[split],
        )
        control = create_p1_model()
        bc = create_model()
        document = evaluate_p6_role(
            population,
            p6_model=run.model,
            p1_control_model=control,
            bc_model=bc,
            support_mask=run.support_mask,
        )
        self.assertTrue(document["common_row_identity"])
        self.assertEqual(
            document["action_comparison"]["eligible_row_count"],
            document["row_counts"]["eligible_row_count"],
        )
        self.assertIn(
            document["hand_progression"]["status"], {"AVAILABLE", "UNAVAILABLE"}
        )
        self.assertEqual(
            document["conservative_q_diagnostics"]["finite_q_rate"], 1.0
        )


if __name__ == "__main__":
    unittest.main()
