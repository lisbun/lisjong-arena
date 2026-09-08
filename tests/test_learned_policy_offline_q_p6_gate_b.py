"""Issue #183 P6 Gate B non-ML protocol tests."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _single_round_artifact_fixtures import provenance

from lisjong_arena.learned_policy_offline_q import p6_gate_b as gate_b
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    comparator_block,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import (
    fallback_policy_block,
    hybrid_activation_block,
)
from lisjong_arena.single_round_artifact import execution_provenance_to_dict


def _result(lower: float, upper: float):
    document = {
        "result_schema_version": gate_b.RESULT_SCHEMA_VERSION,
        "experiment_id": gate_b.EXPERIMENT_ID,
        "source_issue": gate_b.SOURCE_ISSUE,
        "parent_issue": gate_b.PARENT_ISSUE,
        "lock_identity": "a" * 64,
        "lock_comment_url": (
            "https://github.com/lisbun/lisjong-arena/issues/183#issuecomment-1"
        ),
        "candidate": gate_b._expected_candidate_block(),
        "gate_a_binding": {
            "unclassified_result_identity": gate_b.EXPECTED_GATE_A_RESULT_IDENTITY,
            "classified_result_identity": (gate_b.EXPECTED_GATE_A_CLASSIFIED_IDENTITY),
            "classification": gate_b.EXPECTED_GATE_A_CLASSIFICATION,
        },
        "serving": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "comparator": comparator_block(),
        "plan": gate_b.plan_block(),
        "strength_artifact": {
            "schema_version": 1,
            "evaluation_protocol": "abbb-single-round-v1",
            "filename": "strength.json",
            "sha256": "b" * 64,
            "game_count": 100,
            "retention": {
                "backend": gate_b.RETENTION_BACKEND,
                "key": gate_b.ARTIFACT_RETENTION_KEY,
            },
        },
        "canonical_summary": {
            "seed_block_statistics": {
                "seed_block_count": 25,
                "normal_approx_95_interval_lower": lower,
                "normal_approx_95_interval_upper": upper,
            }
        },
        "serving_diagnostics": {
            "policy_instance_count": 100,
            "total_decisions": 1000,
            "total_activations": 700,
            "activation_rate": 0.7,
            "total_scaffold_fallbacks": 300,
            "scaffold_fallback_rate": 0.3,
            "total_support_fallbacks": 0,
            "support_fallback_rate": 0.0,
            "illegal_selection_count": 0,
            "non_finite_q_output_count": 0,
            "resolve_failure_count": 0,
            "fail_closed_at_decision_time": True,
        },
        "classification_rule": dict(gate_b.CLASSIFICATION_RULE),
        "limitations": list(gate_b.LIMITATIONS),
        "no_rescue_boundary": gate_b.NO_RESCUE_BOUNDARY,
        "provenance": execution_provenance_to_dict(provenance()),
        "classification": None,
        "result_identity": None,
    }
    document["result_identity"] = gate_b._result_identity(document)
    return document


class PopulationContractTest(unittest.TestCase):
    def test_default_population_is_fresh_contiguous_25x4(self):
        self.assertEqual(gate_b.DEFAULT_ORDERED_SEEDS, tuple(range(597, 622)))
        self.assertEqual(gate_b.plan_block()["game_count"], 100)
        self.assertEqual(gate_b.plan_block()["rotation_count"], 4)
        self.assertEqual(gate_b.plan_block()["game_mode"], "4p-red-single")
        self.assertEqual(gate_b.plan_block()["max_workers"], 1)
        self.assertIs(gate_b.plan_block()["formal_test"], False)
        self.assertFalse(
            gate_b.declared_allocated_seeds().intersection(gate_b.DEFAULT_ORDERED_SEEDS)
        )

    def test_issue_179_population_is_declared_consumed(self):
        for seed in range(572, 597):
            self.assertIn(seed, gate_b.declared_allocated_seeds())

    def test_external_collision_reformulates_before_lock(self):
        with self.assertRaisesRegex(gate_b.P6GateBError, "SEED PLAN REFORMULATE"):
            gate_b.seed_freshness_block(
                external_freshness_confirmed=True,
                additional_allocated_seeds=[600],
            )

    def test_external_review_is_required(self):
        with self.assertRaises(gate_b.P6GateBError):
            gate_b.seed_freshness_block(external_freshness_confirmed=False)


class ExecutionTargetTest(unittest.TestCase):
    def test_execution_target_is_issue_183_specific(self):
        current = provenance()
        with mock.patch.object(
            gate_b,
            "_require_clean_arena_head",
            return_value=current.lisjong_arena_revision,
        ):
            target = gate_b.execution_target_block(current)
        self.assertEqual(target["source_pr"], "lisbun/lisjong-arena#184")
        self.assertEqual(target["source_issue"], gate_b.SOURCE_ISSUE)
        self.assertIs(target["head_equals_origin_main"], True)
        self.assertEqual(target["merged_main_revision"], current.lisjong_arena_revision)

    def test_execution_target_rejects_provenance_revision_drift(self):
        current = provenance()
        with mock.patch.object(
            gate_b, "_require_clean_arena_head", return_value="f" * 40
        ):
            with self.assertRaises(gate_b.P6GateBError):
                gate_b.execution_target_block(current)


class LockSurfaceTest(unittest.TestCase):
    def test_lock_comment_must_belong_to_issue_183(self):
        accepted = "https://github.com/lisbun/lisjong-arena/issues/183#issuecomment-123"
        self.assertEqual(gate_b.require_pre_execution_comment_url(accepted), accepted)
        with self.assertRaises(gate_b.P6GateBError):
            gate_b.require_pre_execution_comment_url(
                "https://github.com/lisbun/lisjong-arena/issues/181#issuecomment-123"
            )

    def test_all_three_outputs_must_be_absent_with_existing_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locations = {
                "strength_artifact": str(root / "strength.json"),
                "result": str(root / "result.json"),
                "classified_result": str(root / "classified.json"),
            }
            gate_b._require_output_destinations_ready(locations)
            (root / "result.json").write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(gate_b.P6GateBError, "already exists"):
                gate_b._require_output_destinations_ready(locations)


class ClassificationTest(unittest.TestCase):
    def _derive(self, lower, upper):
        with mock.patch.object(gate_b, "require_gate_b_provenance"):
            return gate_b.derive_classification(_result(lower, upper))

    def test_positive_requires_lower_bound_above_zero(self):
        self.assertIs(self._derive(0.01, 2.0), gate_b.P6GateBOutcome.POSITIVE_SIGNAL)

    def test_negative_requires_upper_bound_below_zero(self):
        self.assertIs(self._derive(-2.0, -0.01), gate_b.P6GateBOutcome.NEGATIVE_SIGNAL)

    def test_crossing_or_touching_zero_is_inconclusive(self):
        for lower, upper in ((-1.0, 1.0), (0.0, 1.0), (-1.0, 0.0)):
            with self.subTest(lower=lower, upper=upper):
                self.assertIs(
                    self._derive(lower, upper), gate_b.P6GateBOutcome.INCONCLUSIVE
                )

    def test_recorded_classification_is_rederived(self):
        document = _result(0.1, 1.0)
        with mock.patch.object(gate_b, "require_gate_b_provenance"):
            classified = gate_b.record_classification(
                document, gate_b.P6GateBOutcome.POSITIVE_SIGNAL
            )
            self.assertEqual(
                classified["classification"],
                gate_b.P6GateBOutcome.POSITIVE_SIGNAL.value,
            )
            with self.assertRaises(gate_b.P6GateBError):
                gate_b.record_classification(
                    document, gate_b.P6GateBOutcome.INCONCLUSIVE
                )

    def test_secondary_diagnostics_cannot_change_classification(self):
        first = _result(0.1, 1.0)
        second = _result(0.1, 1.0)
        second["serving_diagnostics"]["total_activations"] = 1
        second["serving_diagnostics"]["activation_rate"] = 0.001
        second["result_identity"] = gate_b._result_identity(second)
        with mock.patch.object(gate_b, "require_gate_b_provenance"):
            self.assertIs(
                gate_b.derive_classification(first),
                gate_b.P6GateBOutcome.POSITIVE_SIGNAL,
            )
            self.assertIs(
                gate_b.derive_classification(second),
                gate_b.P6GateBOutcome.POSITIVE_SIGNAL,
            )


class ProvenanceBindingTest(unittest.TestCase):
    def test_established_162_dependency_revisions_are_locked(self):
        current = provenance()
        exact = type(current)(
            execution_environment=current.execution_environment,
            lisjong_arena_version=current.lisjong_arena_version,
            lisjong_arena_revision=current.lisjong_arena_revision,
            lisjong_version=current.lisjong_version,
            lisjong_revision=gate_b.GATE_B_LISJONG_REVISION,
            lisjong_engine_version=current.lisjong_engine_version,
            lisjong_engine_revision=gate_b.GATE_B_ENGINE_REVISION,
            riichienv_version=current.riichienv_version,
            python_version=current.python_version,
        )
        gate_b.require_gate_b_provenance(exact)

    def test_dependency_revision_drift_is_rejected(self):
        current = provenance()
        wrong = type(current)(
            execution_environment=current.execution_environment,
            lisjong_arena_version=current.lisjong_arena_version,
            lisjong_arena_revision=current.lisjong_arena_revision,
            lisjong_version=current.lisjong_version,
            lisjong_revision="0" * 40,
            lisjong_engine_version=current.lisjong_engine_version,
            lisjong_engine_revision=gate_b.GATE_B_ENGINE_REVISION,
            riichienv_version=current.riichienv_version,
            python_version=current.python_version,
        )
        with self.assertRaises(gate_b.P6GateBError):
            gate_b.require_gate_b_provenance(wrong)


class BindingTest(unittest.TestCase):
    def test_comparator_is_the_established_gate_b_comparator(self):
        self.assertEqual(comparator_block()["identity"], PASSIVE_TSUMOGIRI_IDENTITY)

    def test_guard_is_not_part_of_serving_binding(self):
        block = hybrid_activation_block()
        self.assertEqual(block["selection"], "legal-masked-argmax-q")
        self.assertNotIn("guard", block["semantics_id"])

    def test_result_rejects_nested_candidate_binding_drift(self):
        document = _result(0.1, 1.0)
        document["candidate"]["training"] = {"tampered": True}
        document["result_identity"] = gate_b._result_identity(document)
        with mock.patch.object(gate_b, "require_gate_b_provenance"):
            with self.assertRaises(gate_b.P6GateBError):
                gate_b.validate_result(document)

    def test_gate_a_signal_identity_is_fixed(self):
        self.assertEqual(
            gate_b.EXPECTED_GATE_A_CLASSIFICATION,
            "P6 CONSERVATIVE-Q GATE A SIGNAL",
        )
        self.assertEqual(len(gate_b.EXPECTED_GATE_A_RESULT_IDENTITY), 64)
        self.assertEqual(len(gate_b.EXPECTED_GATE_A_CLASSIFIED_IDENTITY), 64)


if __name__ == "__main__":
    unittest.main()
