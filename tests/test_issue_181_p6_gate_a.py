"""Non-ML contract tests for Issue #181 P6 Gate A."""

import copy
import unittest

from lisjong_arena.learned_policy_offline_q import p6_gate_a
from lisjong_arena.learned_policy_offline_q.artifact import vocabulary_block
from lisjong_arena.learned_policy_offline_q.diagnosis import LOCKED_SOURCE_IDENTITIES
from lisjong_arena.learned_policy_offline_q.errors import OfflineQProtocolError
from lisjong_arena.learned_policy_offline_q.hand_progression import MeasurementAvailability
from lisjong_arena.learned_policy_offline_q.p1_candidate import LOCKED_P1_CANDIDATE
from lisjong_arena.learned_policy_offline_q.p1_features import p1_feature_block
from lisjong_arena.learned_policy_offline_q.p1_gate_a import P1GateARole
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    p1_model_block,
    p1_training_block,
)
from lisjong_arena.learned_policy_offline_q.p6_conservative_q import (
    CQL_ALPHA,
    CQL_TEMPERATURE,
    p6_model_block,
    p6_training_block,
)


def _control_binding():
    return {
        "candidate_identity": "learned-offlineq-p1-gateb:test",
        "canonical_model_weights_digest": (
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest
        ),
        "source_dataset_identity": LOCKED_SOURCE_IDENTITIES.dataset_identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices_digest": LOCKED_SOURCE_IDENTITIES.supported_indices_digest,
        "model": p1_model_block(),
        "training": p1_training_block(),
    }


def _lock():
    document = {
        "lock_schema_version": p6_gate_a.P6_GATE_A_LOCK_SCHEMA_VERSION,
        "experiment_id": p6_gate_a.P6_GATE_A_ID,
        "source_issue": p6_gate_a.SOURCE_ISSUE,
        "parent_issue": p6_gate_a.PARENT_ISSUE,
        "result_exposed": False,
        "arena_revision": "a" * 40,
        "runtime": {"python": "3.14.6", "torch": "2.13.0+cpu", "device": "cpu"},
        "inputs": {
            "retained_artifacts": {
                **LOCKED_SOURCE_IDENTITIES.to_document(),
                "real_artifact_execution": True,
            },
            "p1_control": _control_binding(),
        },
        "source_dataset_identity": LOCKED_SOURCE_IDENTITIES.dataset_identity,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "model": p6_model_block(),
        "base_p1_model": p1_model_block(),
        "base_training": p1_training_block(),
        "p6_training": p6_training_block(),
        "cql_alpha": CQL_ALPHA,
        "cql_temperature": CQL_TEMPERATURE,
        "primary_roles": [role.value for role in p6_gate_a.PRIMARY_ROLES],
        "classification_rule": {
            "signal": "locked",
            "regression": "locked",
            "otherwise": p6_gate_a.P6GateAOutcome.INCONCLUSIVE.value,
        },
        "generation_budget": dict(p6_gate_a.GENERATION_BUDGET),
        "retention": {"backend": "operator-local-durable", "key": "p6-181"},
    }
    document["lock_identity"] = p6_gate_a._sha256_document(document)
    return document


def _arm(worsen_rate):
    return {
        "row_count": 10,
        "post_discard_shanten": {"count": 10, "mean": 1.0, "quantiles": {}},
        "keep_shanten_count": 5,
        "keep_shanten_rate": 0.5,
        "worsen_shanten_count": int(worsen_rate * 10),
        "worsen_shanten_rate": worsen_rate,
    }


def _role(role, *, direction="signal"):
    if direction == "signal":
        p6_worsen, p1_worsen = 0.2, 0.4
        lower, higher = 4, 1
        p6_agree, p1_agree = 7, 5
        signal, regression = True, False
    elif direction == "regression":
        p6_worsen, p1_worsen = 0.5, 0.3
        lower, higher = 1, 4
        p6_agree, p1_agree = 4, 6
        signal, regression = False, True
    elif direction == "flat":
        p6_worsen = p1_worsen = 0.3
        lower = higher = 2
        p6_agree = p1_agree = 5
        signal = regression = False
    else:
        return {
            "role": role.value,
            "source_artifact": p6_gate_a.ROLE_SOURCE[role],
            "split": p6_gate_a.ROLE_SPLIT[role].value,
            "is_primary_role": role in p6_gate_a.PRIMARY_ROLES,
            "is_generalization_evidence": False,
            "row_counts": {},
            "derived_coverage": {},
            "common_row_identity": True,
            "action_comparison": {
                "eligible_row_count": 0,
                "p6_vs_p1_disagreement_count": 0,
                "p6_vs_p1_disagreement_rate": p6_gate_a.rate(0, 0),
                "p6_vs_behavior_agreement_count": 0,
                "p1_vs_behavior_agreement_count": 0,
            },
            "hand_progression": {
                "status": MeasurementAvailability.UNAVAILABLE.value,
                "unavailable_reason": "fixture",
                "arms": None,
                "p6_vs_p1": None,
                "conditions": None,
            },
            "conservative_q_diagnostics": {"status": "UNAVAILABLE"},
        }
    return {
        "role": role.value,
        "source_artifact": p6_gate_a.ROLE_SOURCE[role],
        "split": p6_gate_a.ROLE_SPLIT[role].value,
        "is_primary_role": role in p6_gate_a.PRIMARY_ROLES,
        "is_generalization_evidence": False,
        "row_counts": {},
        "derived_coverage": {},
        "common_row_identity": True,
        "action_comparison": {
            "eligible_row_count": 10,
            "p6_vs_p1_disagreement_count": 3,
            "p6_vs_p1_disagreement_rate": 0.3,
            "p6_vs_behavior_agreement_count": p6_agree,
            "p1_vs_behavior_agreement_count": p1_agree,
        },
        "hand_progression": {
            "status": MeasurementAvailability.AVAILABLE.value,
            "unavailable_reason": None,
            "arms": {
                "p6": _arm(p6_worsen),
                "p1": _arm(p1_worsen),
                "bc": _arm(0.3),
                "behavior": _arm(0.3),
            },
            "p6_vs_p1": {
                "row_count": 10,
                "lower_post_discard_shanten_count": lower,
                "equal_post_discard_shanten_count": 10 - lower - higher,
                "higher_post_discard_shanten_count": higher,
                "higher_post_discard_shanten_rate": higher / 10,
                "worsen_shanten_rate_difference": p6_worsen - p1_worsen,
            },
            "conditions": {
                "p6_behavior_top1_agreement_count": p6_agree,
                "p6_behavior_top1_agreement_rate": p6_agree / 10,
                "p1_behavior_top1_agreement_count": p1_agree,
                "p1_behavior_top1_agreement_rate": p1_agree / 10,
                "signal_conditions": {},
                "regression_conditions": {},
                "signal": signal,
                "regression": regression,
            },
        },
        "conservative_q_diagnostics": {"status": "AVAILABLE"},
    }


def _result(primary_direction="signal", replacement_direction=None):
    replacement_direction = replacement_direction or primary_direction
    directions = {
        P1GateARole.DATASET_TRAIN: "flat",
        P1GateARole.DATASET_VALIDATION: "flat",
        P1GateARole.DATASET_TEST: primary_direction,
        P1GateARole.REPLACEMENT_TEST: replacement_direction,
    }
    document = {
        "result_schema_version": p6_gate_a.P6_GATE_A_RESULT_SCHEMA_VERSION,
        "experiment_id": p6_gate_a.P6_GATE_A_ID,
        "source_issue": p6_gate_a.SOURCE_ISSUE,
        "parent_issue": p6_gate_a.PARENT_ISSUE,
        "lock_identity": "b" * 64,
        "lock_comment_url": (
            "https://github.com/lisbun/lisjong-arena/issues/181#issuecomment-123"
        ),
        "input_binding": _lock()["inputs"],
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "base_training": p1_training_block(),
        "p6_training": p6_training_block(),
        "candidate": {
            "candidate_identity": "learned-p6-conservative-q:test",
            "canonical_model_weights_digest": "c" * 64,
            "source_dataset_identity": LOCKED_SOURCE_IDENTITIES.dataset_identity,
            "supported_indices_digest": LOCKED_SOURCE_IDENTITIES.supported_indices_digest,
            "model": p6_model_block(),
            "training": p6_training_block(),
            "selected_epoch": 20,
        },
        "generation_budget": dict(p6_gate_a.GENERATION_BUDGET),
        "primary_roles": [role.value for role in p6_gate_a.PRIMARY_ROLES],
        "roles": [
            _role(role, direction=directions[role]) for role in p6_gate_a.ALL_ROLES
        ],
        "limitations": list(p6_gate_a.LIMITATIONS),
        "classification": None,
    }
    document["result_identity"] = p6_gate_a._sha256_document(document)
    return document


class P6GateAContractTests(unittest.TestCase):
    def test_locked_change_is_single_fixed_formulation(self):
        self.assertEqual(CQL_ALPHA, 0.1)
        self.assertEqual(CQL_TEMPERATURE, 1.0)
        self.assertEqual(p6_training_block()["base_training"], p1_training_block())
        candidate_model = p6_model_block()
        control_model = p1_model_block()
        self.assertEqual(
            {key: value for key, value in candidate_model.items() if key != "model_id"},
            {key: value for key, value in control_model.items() if key != "model_id"},
        )
        self.assertEqual(sum(p6_gate_a.GENERATION_BUDGET.values()), 0)

    def test_lock_requires_issue_181_comment_url(self):
        valid = "https://github.com/lisbun/lisjong-arena/issues/181#issuecomment-123"
        self.assertEqual(p6_gate_a.require_lock_comment_url(valid), valid)
        with self.assertRaises(OfflineQProtocolError):
            p6_gate_a.require_lock_comment_url(
                "https://github.com/lisbun/lisjong-arena/issues/179#issuecomment-123"
            )

    def test_lock_identity_and_retained_support_are_fail_closed(self):
        document = _lock()
        p6_gate_a.validate_pre_result_lock(document)
        tampered = copy.deepcopy(document)
        tampered["cql_alpha"] = 1.0
        with self.assertRaises(OfflineQProtocolError):
            p6_gate_a.validate_pre_result_lock(tampered)
        tampered = _lock()
        tampered["inputs"]["p1_control"]["supported_indices_digest"] = "0" * 64
        logical = {k: v for k, v in tampered.items() if k != "lock_identity"}
        tampered["lock_identity"] = p6_gate_a._sha256_document(logical)
        with self.assertRaises(OfflineQProtocolError):
            p6_gate_a.validate_pre_result_lock(tampered)

    def test_exhaustive_primary_classification(self):
        self.assertEqual(
            p6_gate_a.derive_classification(_result("signal")),
            p6_gate_a.P6GateAOutcome.SIGNAL,
        )
        self.assertEqual(
            p6_gate_a.derive_classification(_result("regression")),
            p6_gate_a.P6GateAOutcome.REGRESSION,
        )
        self.assertEqual(
            p6_gate_a.derive_classification(_result("signal", "flat")),
            p6_gate_a.P6GateAOutcome.INCONCLUSIVE,
        )
        self.assertEqual(
            p6_gate_a.derive_classification(_result("unavailable")),
            p6_gate_a.P6GateAOutcome.EVIDENCE_INSUFFICIENT,
        )

    def test_recorded_conditions_cannot_override_metrics(self):
        document = _result("signal")
        role = next(
            item for item in document["roles"] if item["role"] == "dataset-test"
        )
        role["hand_progression"]["conditions"]["signal"] = False
        logical = {k: v for k, v in document.items() if k != "result_identity"}
        document["result_identity"] = p6_gate_a._sha256_document(logical)
        with self.assertRaises(OfflineQProtocolError):
            p6_gate_a.validate_gate_a_result(document)


if __name__ == "__main__":
    unittest.main()
