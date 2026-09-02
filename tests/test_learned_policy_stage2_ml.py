"""PyTorch-specific Learned Policy Stage 2 model, training, and safety tests."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _learned_policy_stage2_fixtures import (
    RIICHI_INDEX,
    write_synthetic_dataset,
)

from lisjong_arena.learned_policy_stage2.artifact import load_dataset
from lisjong_arena.learned_policy_stage2.errors import (
    Stage2ArtifactError,
    Stage2ProtocolError,
)
from lisjong_arena.learned_policy_stage2.protocol import (
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage2NetworkTest(unittest.TestCase):
    def test_locked_model_has_the_locked_parameter_count(self):
        from lisjong_arena.learned_policy_stage2.network import (
            create_model,
            parameter_count,
        )

        model = create_model()
        self.assertEqual(parameter_count(model), EXPECTED_PARAMETER_COUNT)
        self.assertEqual(parameter_count(model), 1_153_698)

    def test_masked_log_probabilities_never_leak_onto_illegal_actions(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import (
            masked_argmax,
            masked_log_probabilities,
        )

        logits = torch.arange(VOCABULARY_SIZE, dtype=torch.float32).unsqueeze(0)
        mask = torch.zeros(1, VOCABULARY_SIZE, dtype=torch.bool)
        mask[0, (1, 5, RIICHI_INDEX)] = True
        log_probabilities = masked_log_probabilities(logits, mask)
        probabilities = log_probabilities.exp()
        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=5)
        self.assertAlmostEqual(float(probabilities[0][~mask[0]].sum()), 0.0)
        self.assertEqual(int(masked_argmax(logits, mask)[0]), RIICHI_INDEX)

    def test_uniform_logits_reproduce_the_conditional_uniform_reference(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import masked_cross_entropy

        logits = torch.zeros(1, VOCABULARY_SIZE)
        mask = torch.zeros(1, VOCABULARY_SIZE, dtype=torch.bool)
        mask[0, (0, 2, 4, 6)] = True
        losses = masked_cross_entropy(logits, mask, torch.tensor([2]))
        self.assertAlmostEqual(
            float(losses[0]), torch.tensor(4.0).log().item(), places=6
        )

    def test_output_dimension_mismatch_fails_closed(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import (
            masked_log_probabilities,
        )

        logits = torch.zeros(1, VOCABULARY_SIZE - 1)
        mask = torch.ones(1, VOCABULARY_SIZE - 1, dtype=torch.bool)
        with self.assertRaises(Stage2ProtocolError):
            masked_log_probabilities(logits, mask)

    def test_mask_shape_and_dtype_mismatch_fail_closed(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import (
            masked_log_probabilities,
        )

        logits = torch.zeros(1, VOCABULARY_SIZE)
        with self.assertRaises(Stage2ProtocolError):
            masked_log_probabilities(
                logits, torch.ones(1, VOCABULARY_SIZE, dtype=torch.uint8)
            )
        with self.assertRaises(Stage2ProtocolError):
            masked_log_probabilities(
                logits, torch.ones(2, VOCABULARY_SIZE, dtype=torch.bool)
            )

    def test_a_row_without_any_legal_action_fails_closed(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import (
            masked_log_probabilities,
        )

        with self.assertRaises(Stage2ProtocolError):
            masked_log_probabilities(
                torch.zeros(1, VOCABULARY_SIZE),
                torch.zeros(1, VOCABULARY_SIZE, dtype=torch.bool),
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage2TrainingTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.dataset = write_synthetic_dataset(self.root / "dataset")

    def test_split_tensors_partition_the_dataset_by_whole_hanchan(self):
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        tensors = load_split_tensors(self.dataset)
        total = sum(tensors[split].row_count for split in Split)
        self.assertEqual(total, self.dataset.row_count)
        seen = set()
        for split in Split:
            entry = tensors[split]
            self.assertEqual(entry.features.shape[1], FEATURE_DIMENSION)
            self.assertEqual(entry.legal_mask.shape[1], VOCABULARY_SIZE)
            seeds = {self.dataset.rows[index].seed for index in entry.row_indices}
            self.assertEqual(seeds & seen, set())
            seen |= seeds

    def test_teacher_labels_are_legal_in_every_split(self):
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        tensors = load_split_tensors(self.dataset)
        for split in Split:
            entry = tensors[split]
            legal = entry.legal_mask.gather(1, entry.targets.unsqueeze(1))
            self.assertTrue(bool(legal.all()))

    def test_training_selects_a_checkpoint_and_round_trips_it(self):
        import torch

        from lisjong_arena.learned_policy_stage2.training import (
            load_checkpoint,
            save_checkpoint,
            train_stage2_model,
        )

        run = train_stage2_model(self.dataset)
        self.assertGreaterEqual(run.selected_epoch, 1)
        self.assertLessEqual(run.selected_epoch, 20)
        self.assertEqual(
            run.selected_validation_choice_masked_ce,
            min(record.validation_choice_masked_ce for record in run.history),
        )
        self.assertTrue(run.runtime["deterministic_algorithms"])
        self.assertEqual(run.runtime["torch_threads"], 1)

        checkpoint = save_checkpoint(self.root / "checkpoint", self.dataset, run)
        self.assertEqual(checkpoint.manifest["dataset_identity"], self.dataset.identity)
        self.assertEqual(
            checkpoint.manifest["parameter_count"], EXPECTED_PARAMETER_COUNT
        )
        reloaded = load_checkpoint(self.root / "checkpoint")
        self.assertEqual(reloaded.identity, checkpoint.identity)
        self.assertEqual(reloaded.weights_sha256, checkpoint.weights_sha256)
        expected = run.model.state_dict()
        actual = reloaded.model.state_dict()
        self.assertEqual(set(actual), set(expected))
        for name in expected:
            self.assertTrue(bool(torch.equal(actual[name], expected[name])))

    def test_frozen_inference_is_deterministic(self):
        import torch

        from lisjong_arena.learned_policy_stage2.network import masked_argmax
        from lisjong_arena.learned_policy_stage2.training import (
            load_checkpoint,
            load_split_tensors,
            save_checkpoint,
            train_stage2_model,
        )

        run = train_stage2_model(self.dataset)
        save_checkpoint(self.root / "checkpoint", self.dataset, run)
        first = load_checkpoint(self.root / "checkpoint").model
        second = load_checkpoint(self.root / "checkpoint").model
        tensors = load_split_tensors(self.dataset)[Split.TEST]
        with torch.no_grad():
            left = first(tensors.features)
            right = second(tensors.features)
        self.assertTrue(bool(torch.equal(left, right)))
        self.assertTrue(
            bool(
                torch.equal(
                    masked_argmax(left, tensors.legal_mask),
                    masked_argmax(right, tensors.legal_mask),
                )
            )
        )

    def test_tampered_weights_fail_closed(self):
        from lisjong_arena.learned_policy_stage2.training import (
            WEIGHTS_FILENAME,
            load_checkpoint,
            save_checkpoint,
            train_stage2_model,
        )

        run = train_stage2_model(self.dataset)
        save_checkpoint(self.root / "checkpoint", self.dataset, run)
        target = self.root / "checkpoint" / WEIGHTS_FILENAME
        payload = bytearray(target.read_bytes())
        payload[-1] ^= 0xFF
        target.write_bytes(bytes(payload))
        with self.assertRaises(Stage2ArtifactError):
            load_checkpoint(self.root / "checkpoint")

    def test_tampered_model_config_fails_closed(self):
        from lisjong_arena.learned_policy_stage2.training import (
            MANIFEST_FILENAME,
            load_checkpoint,
            save_checkpoint,
            train_stage2_model,
        )

        run = train_stage2_model(self.dataset)
        save_checkpoint(self.root / "checkpoint", self.dataset, run)
        target = self.root / "checkpoint" / MANIFEST_FILENAME
        document = json.loads(target.read_text(encoding="utf-8"))
        document["model"]["hidden_width"] = 256
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(Stage2ArtifactError):
            load_checkpoint(self.root / "checkpoint")

    def test_existing_checkpoint_destination_is_never_overwritten(self):
        from lisjong_arena.learned_policy_stage2.training import (
            save_checkpoint,
            train_stage2_model,
        )

        run = train_stage2_model(self.dataset)
        save_checkpoint(self.root / "checkpoint", self.dataset, run)
        with self.assertRaises(FileExistsError):
            save_checkpoint(self.root / "checkpoint", self.dataset, run)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage2EvaluationTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.dataset = write_synthetic_dataset(self.root / "dataset")

    def test_metrics_separate_forced_rows_and_never_select_illegal_actions(self):
        from lisjong_arena.learned_policy_stage2.evaluation import evaluate_split
        from lisjong_arena.learned_policy_stage2.network import create_model
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        model = create_model()
        tensors = load_split_tensors(self.dataset)[Split.TEST]
        metrics = evaluate_split(model, self.dataset, tensors)
        self.assertEqual(metrics.split, Split.TEST)
        self.assertEqual(metrics.forced_rows + metrics.choice_rows, metrics.total_rows)
        self.assertGreater(metrics.choice_rows, 0)
        self.assertEqual(metrics.illegal_selection_count, 0)
        self.assertEqual([k for k, _ in metrics.choice_top_k_agreement], [3, 5])
        for _, value in metrics.choice_top_k_agreement:
            self.assertGreaterEqual(value, metrics.choice_exact_agreement)
        self.assertEqual(
            {metric.seed for metric in metrics.per_hanchan}, {213, 214, 215}
        )
        self.assertEqual(
            sum(metric.choice_rows for metric in metrics.per_hanchan),
            metrics.choice_rows,
        )

    def test_untrained_model_does_not_beat_the_uniform_reference(self):
        from lisjong_arena.learned_policy_stage2.evaluation import evaluate_split
        from lisjong_arena.learned_policy_stage2.network import create_model
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        model = create_model()
        tensors = load_split_tensors(self.dataset)[Split.TEST]
        metrics = evaluate_split(model, self.dataset, tensors)
        self.assertGreater(metrics.uniform_choice_cross_entropy, 0.0)
        self.assertLess(
            abs(
                metrics.choice_masked_cross_entropy
                - metrics.uniform_choice_cross_entropy
            ),
            1.0,
        )

    def test_reported_metrics_serialize_to_json_safe_documents(self):
        from lisjong_arena.learned_policy_stage2.evaluation import evaluate_split
        from lisjong_arena.learned_policy_stage2.network import create_model
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        model = create_model()
        tensors = load_split_tensors(self.dataset)[Split.TEST]
        document = evaluate_split(model, self.dataset, tensors).to_document()
        json.dumps(document, allow_nan=False)
        self.assertEqual(document["split"], "TEST")


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage2DecisionRuleTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.dataset = load_dataset(write_synthetic_dataset(self.root / "dataset").path)

    def _serving(self, **overrides):
        from lisjong_arena.learned_policy_stage2.serving_check import (
            InferenceLatency,
            ServingPathReport,
        )

        values = {
            "seeds": (213, 214, 215),
            "decisions": 36,
            "teacher_label_legal": 36,
            "illegal_selected_actions": 0,
            "resolve_failures": 0,
            "feature_mismatches": 0,
            "legal_mask_mismatches": 0,
            "teacher_index_mismatches": 0,
            "nondeterministic_logits": 0,
            "nondeterministic_selections": 0,
            "latency": InferenceLatency(36, 0.001, 0.001, 0.001, 0.003),
        }
        values.update(overrides)
        return ServingPathReport(**values)

    def _classify(self, serving, *, beats_uniform: bool, exposures: int = 1):
        from lisjong_arena.learned_policy_stage2.coverage import build_coverage
        from lisjong_arena.learned_policy_stage2.decision_rule import classify_outcome
        from lisjong_arena.learned_policy_stage2.evaluation import evaluate_split
        from lisjong_arena.learned_policy_stage2.network import create_model
        from lisjong_arena.learned_policy_stage2.training import load_split_tensors

        tensors = load_split_tensors(self.dataset)[Split.TEST]
        metrics = evaluate_split(create_model(), self.dataset, tensors)
        uniform = metrics.uniform_choice_cross_entropy
        adjusted = type(metrics)(
            **{
                **{
                    field: getattr(metrics, field)
                    for field in metrics.__dataclass_fields__
                },
                "choice_masked_cross_entropy": (
                    uniform - 0.5 if beats_uniform else uniform + 0.5
                ),
            }
        )
        return classify_outcome(
            dataset_identity=self.dataset.identity,
            non_finite_feature_count=0,
            coverage=build_coverage(self.dataset),
            serving=serving,
            test_metrics=adjusted,
            test_exposure_count=exposures,
        )

    def test_passing_gates_classify_as_vertical_slice_viable(self):
        from lisjong_arena.learned_policy_stage2.protocol import Stage2Outcome

        report = self._classify(self._serving(), beats_uniform=True)
        self.assertTrue(report.hard_gate_passed)
        self.assertTrue(report.model_learning_gate_passed)
        self.assertIs(report.outcome, Stage2Outcome.VERTICAL_SLICE_VIABLE)

    def test_model_gate_failure_classifies_as_model_capacity_insufficient(self):
        from lisjong_arena.learned_policy_stage2.protocol import Stage2Outcome

        report = self._classify(self._serving(), beats_uniform=False)
        self.assertTrue(report.hard_gate_passed)
        self.assertFalse(report.model_learning_gate_passed)
        self.assertIs(report.outcome, Stage2Outcome.MODEL_CAPACITY_INSUFFICIENT)

    def test_any_hard_gate_failure_classifies_as_stop_invalid(self):
        from lisjong_arena.learned_policy_stage2.protocol import Stage2Outcome

        for override in (
            {"teacher_label_legal": 35},
            {"illegal_selected_actions": 1},
            {"resolve_failures": 1},
            {"nondeterministic_logits": 1},
            {"nondeterministic_selections": 1},
            {"feature_mismatches": 1},
            {"legal_mask_mismatches": 1},
            {"teacher_index_mismatches": 1},
        ):
            with self.subTest(override=override):
                report = self._classify(self._serving(**override), beats_uniform=True)
                self.assertFalse(report.hard_gate_passed)
                self.assertIs(report.outcome, Stage2Outcome.STOP_INVALID)

    def test_repeated_test_exposure_classifies_as_stop_invalid(self):
        from lisjong_arena.learned_policy_stage2.protocol import Stage2Outcome

        report = self._classify(self._serving(), beats_uniform=True, exposures=2)
        self.assertFalse(report.hard_gate_passed)
        self.assertIs(report.outcome, Stage2Outcome.STOP_INVALID)


if __name__ == "__main__":
    unittest.main()
