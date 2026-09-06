"""Arena #157 epoch-budget adequacy studyのtorch非依存test。

retained artifact identity、population / dataset / split mismatchの拒否、
epoch budget以外のconfig mismatchの拒否、execution lock identity / readback、
determinism gateのsuccessとmismatch、exhaustive outcomeの4分岐、tampered
artifactの拒否、bootstrap定数の固定、result-driven extensionが存在しない
ことを固定する。

E80のlarge trainingはunit testへ入れない。#150 historical protocol / seeds /
schema / validatorは変更しないので、regression boundaryとしてそちら側の
constantsも合わせて固定する。
"""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from _stage3_epoch_budget_fixtures import (
    BASELINE_SNAPSHOT_MAE,
    BUDGET_ARENA_REVISION,
    RETAINED_MAE,
    WORSENING_TAIL,
    budget_lock_value,
    budget_manifest_value,
    budget_result_value,
    evaluation_cell,
    locked_identities_match,
    population_manifest,
    retained_lock_value,
    retained_manifest_value,
)

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_epoch_budget.artifact import (
    load_result,
    save_result,
    validate_model_manifest,
)
from lisjong_arena.stage3_epoch_budget.comparison import (
    classify_interval,
    compare,
    determinism_gate,
)
from lisjong_arena.stage3_epoch_budget.lock import (
    comparison_semantics,
    validate_lock,
    validate_runtime,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    ARMS,
    BASELINE_ARM,
    BASELINE_MAX_EPOCHS,
    BOOTSTRAP,
    BUDGET_ARM,
    BUDGET_BOUND,
    BUDGET_MARGINAL,
    BUDGET_MAX_EPOCHS,
    BUDGET_SUFFICIENT,
    CLASSIFICATIONS,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    DETERMINISM_PREFIX_EPOCHS,
    INCONCLUSIVE,
    OUTCOMES,
    PATIENCE,
    PRIMARY_AXIS,
    RETAINED_DATASET_IDENTITY,
    RETAINED_EXECUTION_LOCK_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_SELECTED_EPOCH,
    RETAINED_TRAIN_ANCHORS,
    RETAINED_VALIDATION_ANCHORS,
    RETAINED_WEIGHTS_SHA256,
    SELECTION_EXPOSURE,
    STOP_INVALID,
    BudgetError,
    assert_bootstrap_constants_are_locked,
    assert_single_axis,
    baseline_training_lock,
    budget_training_config,
    budget_training_lock,
    identity,
)
from lisjong_arena.stage3_epoch_budget.result import (
    RESULT_FIELDS,
    classify,
    retained_artifact_gate,
    validate_result,
)
from lisjong_arena.stage3_epoch_budget.retained import (
    retained_value,
    validate_retained_manifest,
    validate_retained_population,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    BOOTSTRAP as SCALE_BOOTSTRAP,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    ScaleError,
)


def _clone(value):
    return deepcopy(value)


class RetainedIdentityTest(unittest.TestCase):
    """Issue #157がlockした#150 artifact identityをそのまま持つこと。"""

    def test_the_locked_identities_are_the_issue_values(self):
        self.assertEqual(
            RETAINED_POPULATION_IDENTITY,
            "e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7",
        )
        self.assertEqual(
            RETAINED_RAW_CORPUS_IDENTITY,
            "bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2",
        )
        self.assertEqual(
            RETAINED_DATASET_IDENTITY,
            "fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646",
        )
        self.assertEqual(
            RETAINED_WEIGHTS_SHA256,
            "71f5ff5bf39077d9be38a99de2a3ff692349e7b58e994df70ec238ca5c59a0c1",
        )
        self.assertEqual(
            RETAINED_EXECUTION_LOCK_IDENTITY,
            "a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3",
        )

    def test_the_locked_split_matches_the_issue_expectation(self):
        value = retained_value()
        self.assertEqual(value["train_seeds"], list(range(360, 424)))
        self.assertEqual(value["validation_seeds"], list(range(424, 440)))
        self.assertEqual(value["train_hanchan"], 64)
        self.assertEqual(value["validation_hanchan"], 16)
        self.assertEqual(value["train_anchors"], RETAINED_TRAIN_ANCHORS)
        self.assertEqual(value["validation_anchors"], RETAINED_VALIDATION_ANCHORS)
        self.assertEqual(RETAINED_TRAIN_ANCHORS, 32726)
        self.assertEqual(RETAINED_VALIDATION_ANCHORS, 7932)
        self.assertIs(value["test_partition_present"], False)
        self.assertIs(value["regenerated"], False)
        self.assertIs(value["baseline_retrained"], False)
        self.assertEqual(value["baseline_selected_epoch"], RETAINED_SELECTED_EPOCH)
        self.assertEqual(
            value["population_recipe"],
            "yakuhai-call primary + kan-coverage-yakuhai-call augmentation @ 12.5%",
        )

    def test_the_train_and_validation_seeds_come_from_the_phase10_plan(self):
        self.assertEqual(retained_value()["train_seeds"], list(TRAIN_SEEDS))
        self.assertEqual(retained_value()["validation_seeds"], list(VALIDATION_SEEDS))


class SingleAxisTest(unittest.TestCase):
    """変更してよいexperimental axisが`max_epochs`だけであること。"""

    def test_the_budget_lock_differs_from_the_baseline_only_in_max_epochs(self):
        baseline = baseline_training_lock()
        budget = budget_training_lock()
        differing = {
            name
            for name in set(baseline) | set(budget)
            if baseline.get(name) != budget.get(name)
        }
        self.assertEqual(differing, {"training_config"})
        baseline_config = baseline["training_config"]
        budget_config = budget["training_config"]
        self.assertEqual(
            {
                name
                for name in set(baseline_config) | set(budget_config)
                if baseline_config.get(name) != budget_config.get(name)
            },
            {PRIMARY_AXIS},
        )
        self.assertEqual(baseline_config[PRIMARY_AXIS], BASELINE_MAX_EPOCHS)
        self.assertEqual(budget_config[PRIMARY_AXIS], BUDGET_MAX_EPOCHS)

    def test_patience_stays_six_in_both_arms(self):
        self.assertEqual(PATIENCE, 6)
        self.assertEqual(baseline_training_lock()["training_config"]["patience"], 6)
        self.assertEqual(budget_training_lock()["training_config"]["patience"], 6)

    def test_the_locked_optimizer_and_seed_settings_are_shared(self):
        baseline = baseline_training_lock()["training_config"]
        budget = budget_training_lock()["training_config"]
        for name in (
            "seed",
            "dataloader_seed",
            "learning_rate",
            "weight_decay",
            "workers",
            "deterministic_algorithms",
            "torch_threads",
        ):
            self.assertEqual(baseline[name], budget[name])

    def test_a_second_changed_config_field_is_rejected(self):
        budget = budget_training_lock()
        budget["training_config"]["learning_rate"] = 0.0005
        with self.assertRaises(BudgetError):
            assert_single_axis(baseline_training_lock(), budget)

    def test_a_changed_patience_is_rejected(self):
        budget = budget_training_lock()
        budget["training_config"]["patience"] = 12
        with self.assertRaises(BudgetError):
            assert_single_axis(baseline_training_lock(), budget)

    def test_a_changed_model_family_is_rejected(self):
        budget = budget_training_lock()
        budget["parameter_count"] = 999_999
        with self.assertRaises(BudgetError):
            assert_single_axis(baseline_training_lock(), budget)

    def test_a_changed_epoch_budget_value_is_rejected(self):
        budget = budget_training_lock()
        budget["training_config"][PRIMARY_AXIS] = 160
        with self.assertRaises(BudgetError):
            assert_single_axis(baseline_training_lock(), budget)

    def test_the_budget_training_config_only_moves_max_epochs(self):
        from dataclasses import asdict

        from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

        actual = asdict(budget_training_config())
        expected = asdict(FORMAL_TRAINING_CONFIG)
        self.assertEqual(
            {name for name in expected if actual[name] != expected[name]},
            {PRIMARY_AXIS},
        )
        self.assertEqual(actual[PRIMARY_AXIS], BUDGET_MAX_EPOCHS)


class BootstrapConstantsTest(unittest.TestCase):
    """bootstrap定数を#150からそのまま引き継ぎ、本childで選び直さないこと。"""

    def test_the_bootstrap_constants_match_phase10(self):
        assert_bootstrap_constants_are_locked()
        for name in (
            "unit",
            "replicates",
            "seed",
            "lower_percentile",
            "upper_percentile",
        ):
            self.assertEqual(BOOTSTRAP[name], SCALE_BOOTSTRAP[name])
        self.assertEqual(
            BOOTSTRAP["order_statistic_indices"],
            list(SCALE_BOOTSTRAP["order_statistic_indices"]),
        )

    def test_the_issue_bootstrap_values_are_recorded(self):
        self.assertEqual(BOOTSTRAP["replicates"], 10000)
        self.assertEqual(BOOTSTRAP["seed"], 148)
        self.assertEqual(BOOTSTRAP["lower_percentile"], 2.5)
        self.assertEqual(BOOTSTRAP["upper_percentile"], 97.5)
        self.assertEqual(BOOTSTRAP["order_statistic_indices"], [249, 9750])
        self.assertEqual(
            BOOTSTRAP["statistic"],
            "anchor-weighted pooled MAE(E40) - pooled MAE(E80)",
        )


class DeterminismGateTest(unittest.TestCase):
    """`E80.loss_history[0:40] == #150 S64.loss_history`をexactに扱うこと。"""

    def setUp(self):
        self.baseline = [
            {
                "epoch": epoch,
                "train_mse": 0.5 - epoch * 1e-3,
                "validation_mae": 0.5 - epoch * 1e-4,
            }
            for epoch in range(1, BASELINE_MAX_EPOCHS + 1)
        ]

    def _budget(self, prefix, tail_length=10):
        return list(prefix) + [
            {
                "epoch": BASELINE_MAX_EPOCHS + offset,
                "train_mse": 0.3,
                "validation_mae": 0.4 - offset * 1e-4,
            }
            for offset in range(1, tail_length + 1)
        ]

    def test_an_exact_prefix_passes(self):
        gate = determinism_gate(self.baseline, self._budget(self.baseline))
        self.assertIs(gate["matched"], True)
        self.assertEqual(gate["prefix_epochs"], DETERMINISM_PREFIX_EPOCHS)
        self.assertEqual(gate["baseline_epochs"], BASELINE_MAX_EPOCHS)
        self.assertEqual(gate["budget_epochs"], BASELINE_MAX_EPOCHS + 10)
        self.assertEqual(gate["baseline_prefix_digest"], gate["budget_prefix_digest"])

    def test_a_last_bit_difference_fails_the_gate(self):
        drifted = _clone(self.baseline)
        drifted[-1]["validation_mae"] += 1e-15
        gate = determinism_gate(self.baseline, self._budget(drifted))
        self.assertIs(gate["matched"], False)
        self.assertNotEqual(
            gate["baseline_prefix_digest"], gate["budget_prefix_digest"]
        )

    def test_the_gate_is_not_a_tolerance_comparison(self):
        """1 ULPのdriftでもmatchしない。toleranceで吸収しない。"""
        import math

        drifted = _clone(self.baseline)
        drifted[0]["train_mse"] = math.nextafter(drifted[0]["train_mse"], math.inf)
        self.assertNotEqual(drifted[0]["train_mse"], self.baseline[0]["train_mse"])
        self.assertIs(
            determinism_gate(self.baseline, self._budget(drifted))["matched"], False
        )

    def test_a_short_budget_history_is_rejected(self):
        with self.assertRaises(BudgetError):
            determinism_gate(self.baseline, self.baseline[:-1])

    def test_a_baseline_that_is_not_forty_epochs_is_rejected(self):
        with self.assertRaises(BudgetError):
            determinism_gate(self.baseline[:-1], self._budget(self.baseline))


class ClassificationTest(unittest.TestCase):
    """paired intervalのexhaustive classification。"""

    def test_a_positive_lower_bound_is_a_clear_budget_improvement(self):
        self.assertEqual(classify_interval(0.001, 0.01), CLEAR_IMPROVEMENT)

    def test_a_negative_upper_bound_is_a_clear_budget_regression(self):
        self.assertEqual(classify_interval(-0.01, -0.001), CLEAR_REGRESSION)

    def test_an_interval_containing_zero_is_inconclusive(self):
        self.assertEqual(classify_interval(-0.001, 0.01), INCONCLUSIVE)
        self.assertEqual(classify_interval(0.0, 0.01), INCONCLUSIVE)
        self.assertEqual(classify_interval(-0.01, 0.0), INCONCLUSIVE)

    def test_the_classification_set_is_exhaustive(self):
        self.assertEqual(
            set(CLASSIFICATIONS),
            {CLEAR_IMPROVEMENT, CLEAR_REGRESSION, INCONCLUSIVE},
        )

    def test_an_inverted_interval_is_rejected(self):
        with self.assertRaises(BudgetError):
            classify_interval(0.01, -0.01)


class OutcomeRuleTest(unittest.TestCase):
    """locked decision ruleのexhaustive 4分岐。"""

    PASSING = {
        "retained_artifact_identity": True,
        "determinism": True,
        BASELINE_ARM + "_physical_validity": True,
        BUDGET_ARM + "_physical_validity": True,
        BASELINE_ARM + "_self_rollout_complete": True,
        BUDGET_ARM + "_self_rollout_complete": True,
    }

    def test_the_outcome_set_is_exhaustive(self):
        self.assertEqual(
            set(OUTCOMES),
            {STOP_INVALID, BUDGET_SUFFICIENT, BUDGET_BOUND, BUDGET_MARGINAL},
        )

    def test_a_failed_determinism_gate_is_stop_invalid(self):
        gates = dict(self.PASSING, determinism=False)
        outcome, reasons = classify(gates, None, 61)
        self.assertEqual(outcome, STOP_INVALID)
        self.assertIn("determinism", reasons[0])

    def test_a_failed_retained_identity_gate_is_stop_invalid(self):
        gates = dict(self.PASSING, retained_artifact_identity=False)
        self.assertEqual(classify(gates, None, 61)[0], STOP_INVALID)

    def test_a_failed_physical_gate_is_stop_invalid(self):
        gates = dict(self.PASSING)
        gates[BUDGET_ARM + "_physical_validity"] = False
        self.assertEqual(classify(gates, None, 61)[0], STOP_INVALID)

    def test_a_selected_epoch_within_forty_is_budget_sufficient(self):
        comparison = {"classification": CLEAR_IMPROVEMENT}
        self.assertEqual(
            classify(self.PASSING, comparison, BASELINE_MAX_EPOCHS)[0],
            BUDGET_SUFFICIENT,
        )
        self.assertEqual(classify(self.PASSING, comparison, 36)[0], BUDGET_SUFFICIENT)

    def test_a_later_epoch_with_a_clear_improvement_is_budget_bound(self):
        outcome, reasons = classify(
            self.PASSING, {"classification": CLEAR_IMPROVEMENT}, 61
        )
        self.assertEqual(outcome, BUDGET_BOUND)
        self.assertIn("development checkpoint-selection surface", reasons[0])
        self.assertIn("no ", reasons[0])

    def test_a_later_epoch_without_a_clear_improvement_is_budget_marginal(self):
        for classification in (INCONCLUSIVE, CLEAR_REGRESSION):
            outcome, reasons = classify(
                self.PASSING, {"classification": classification}, 61
            )
            self.assertEqual(outcome, BUDGET_MARGINAL)
            self.assertIn("not clear", reasons[0])

    def test_passing_gates_require_a_comparison(self):
        with self.assertRaises(BudgetError):
            classify(self.PASSING, None, 61)


class ExecutionLockTest(unittest.TestCase):
    """execution lock identity / readbackとpre-exposure contract。"""

    def test_a_well_formed_lock_is_accepted(self):
        self.assertEqual(validate_lock(budget_lock_value()), budget_lock_value())

    def test_the_lock_identity_is_stable_and_readback_safe(self):
        lock = budget_lock_value()
        encoded = canonical_json_bytes(lock)
        self.assertEqual(identity(json.loads(encoded)), identity(lock))
        self.assertEqual(identity(validate_lock(json.loads(encoded))), identity(lock))

    def test_the_lock_carries_the_retained_identities_and_budget_semantics(self):
        lock = budget_lock_value()
        self.assertEqual(lock["retained"], retained_value())
        self.assertEqual(lock["budget_training_lock"], budget_training_lock())
        self.assertEqual(lock["comparison"], comparison_semantics())
        self.assertEqual(lock["comparison"]["budget_max_epochs"], BUDGET_MAX_EPOCHS)
        self.assertEqual(lock["comparison"]["patience"], PATIENCE)
        self.assertEqual(lock["comparison"]["bootstrap"], dict(BOOTSTRAP))
        self.assertEqual(lock["comparison"]["budget"]["new_hanchan"], 0)
        self.assertEqual(lock["comparison"]["budget"]["new_seed"], 0)
        self.assertEqual(lock["comparison"]["budget"]["new_corpus_generation"], 0)
        self.assertEqual(lock["comparison"]["budget"]["formal_test_exposure"], 0)
        self.assertEqual(lock["comparison"]["budget"]["budget_arm_trainings"], 1)
        self.assertEqual(lock["comparison"]["budget"]["baseline_arm_trainings"], 0)

    def test_a_result_exposed_lock_is_rejected(self):
        with self.assertRaises(BudgetError):
            validate_lock(budget_lock_value(result_exposed=True))

    def test_a_missing_artifact_audit_is_rejected(self):
        with self.assertRaises(BudgetError):
            validate_lock(budget_lock_value(artifact_audit="   "))

    def test_extra_or_missing_lock_fields_are_rejected(self):
        extra = budget_lock_value()
        extra["note"] = "extra"
        with self.assertRaises(BudgetError):
            validate_lock(extra)
        missing = budget_lock_value()
        del missing["comparison"]
        with self.assertRaises(BudgetError):
            validate_lock(missing)

    def test_a_lock_that_widens_the_axis_is_rejected(self):
        lock = budget_lock_value()
        lock["budget_training_lock"]["training_config"]["patience"] = 12
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_a_lock_that_moves_the_epoch_budget_is_rejected(self):
        lock = budget_lock_value()
        lock["budget_training_lock"]["training_config"][PRIMARY_AXIS] = 160
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_a_lock_that_edits_the_bootstrap_constants_is_rejected(self):
        lock = budget_lock_value()
        lock["comparison"]["bootstrap"]["seed"] = 149
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_an_unpinned_source_revision_is_rejected(self):
        lock = budget_lock_value()
        lock["provenance"]["source_revisions"]["lisjong"] = "0" * 40
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_an_unresolved_provenance_is_rejected(self):
        lock = budget_lock_value()
        lock["provenance"]["fully_resolved"] = False
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_a_gpu_or_threaded_runtime_is_rejected(self):
        for override in (
            {"device": "cuda"},
            {"torch_threads": 4},
            {"deterministic_algorithms": False},
            {"free_threaded": True},
        ):
            lock = budget_lock_value()
            lock["runtime"].update(override)
            lock["retained_runtime"].update(override)
            with self.assertRaises(BudgetError):
                validate_lock(lock)

    def test_a_runtime_that_differs_from_the_retained_numeric_runtime_is_rejected(self):
        lock = budget_lock_value()
        lock["runtime"]["torch"] = "2.13.0"
        lock["retained_runtime"]["torch"] = "2.13.0+cpu"
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_a_platform_difference_is_recorded_rather_than_fatal(self):
        lock = budget_lock_value()
        lock["runtime"]["platform"] = "Windows-11-other"
        lock["platform_matches_retained"] = False
        validate_lock(lock)
        validate_runtime(lock["runtime"], lock["retained_runtime"])

    def test_a_misreported_platform_agreement_is_rejected(self):
        lock = budget_lock_value()
        lock["runtime"]["platform"] = "Windows-11-other"
        with self.assertRaises(BudgetError):
            validate_lock(lock)

    def test_the_arena_revision_differs_from_the_phase10_execution_revision(self):
        lock = budget_lock_value()
        self.assertEqual(
            lock["provenance"]["source_revisions"]["lisjong_arena"],
            BUDGET_ARENA_REVISION,
        )
        self.assertNotEqual(
            lock["provenance"]["source_revisions"]["lisjong_arena"],
            lock["retained"]["execution_lock_identity"],
        )


class RetainedReadbackTest(unittest.TestCase):
    """#150 population / model manifestのexact identity validation。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.retained_lock = retained_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.retained_lock
        )
        cls.retained = retained_manifest_value(cls.population, cls.retained_lock)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_a_population_whose_corpus_or_dataset_identity_differs_is_rejected(self):
        population = _clone(self.population)
        self.assertNotEqual(
            population["raw_corpus_identity"], RETAINED_RAW_CORPUS_IDENTITY
        )
        with self.assertRaises(BudgetError):
            validate_retained_population(population)

    def test_a_population_with_a_substituted_population_identity_is_rejected(self):
        population = _clone(self.population)
        population["raw_corpus_identity"] = RETAINED_RAW_CORPUS_IDENTITY
        population["dataset_identity"] = RETAINED_DATASET_IDENTITY
        population["population_identity"] = "e" * 64
        with self.assertRaises(BudgetError):
            validate_retained_population(population)

    def test_a_population_whose_split_membership_moved_is_rejected(self):
        population = _clone(self.population)
        population["population_identity"] = RETAINED_POPULATION_IDENTITY
        population["raw_corpus_identity"] = RETAINED_RAW_CORPUS_IDENTITY
        population["dataset_identity"] = RETAINED_DATASET_IDENTITY
        population["population_plan"]["train_seeds"] = list(range(360, 392))
        with self.assertRaises(BudgetError):
            validate_retained_population(population)

    def test_a_population_that_declares_a_formal_test_partition_is_rejected(self):
        population = _clone(self.population)
        population["population_identity"] = RETAINED_POPULATION_IDENTITY
        population["raw_corpus_identity"] = RETAINED_RAW_CORPUS_IDENTITY
        population["dataset_identity"] = RETAINED_DATASET_IDENTITY
        population["population_plan"]["test_partition_present"] = True
        with self.assertRaises(BudgetError):
            validate_retained_population(population)

    def test_a_substituted_scale_is_rejected(self):
        manifest = _clone(self.retained)
        manifest["scale"] = "S32"
        with self.assertRaises(BudgetError):
            validate_retained_manifest(manifest)

    def test_a_substituted_weights_digest_is_rejected(self):
        manifest = _clone(self.retained)
        manifest["weights_sha256"] = "c" * 64
        with self.assertRaises(BudgetError):
            validate_retained_manifest(manifest)

    def test_a_baseline_arm_trained_under_a_different_budget_is_rejected(self):
        manifest = _clone(self.retained)
        manifest["training_lock"] = budget_training_lock()
        with self.assertRaises(BudgetError):
            validate_retained_manifest(manifest)

    def test_a_baseline_arm_with_a_longer_history_is_rejected(self):
        manifest = _clone(self.retained)
        manifest["loss_history"] = manifest["loss_history"] + [
            {"epoch": 41, "train_mse": 0.3, "validation_mae": 0.39}
        ]
        with self.assertRaises(BudgetError):
            validate_retained_manifest(manifest)


class ModelArtifactTest(unittest.TestCase):
    """E80 model manifestのbindingとtampered artifactの拒否。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.retained_lock = retained_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.retained_lock
        )
        cls.retained = retained_manifest_value(cls.population, cls.retained_lock)
        cls.lock = budget_lock_value()
        cls.budget = budget_manifest_value(cls.population, cls.lock, cls.retained)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _validate(self, manifest):
        return validate_model_manifest(
            manifest, self.population, self.lock, self.retained
        )

    def test_a_well_formed_budget_manifest_is_accepted(self):
        self._validate(_clone(self.budget))

    def test_the_budget_arm_shares_the_retained_train_subset(self):
        self.assertEqual(self.budget["subset"], self.retained["subset"])
        self.assertEqual(
            self.budget["train_anchor_identities"],
            self.retained["train_anchor_identities"],
        )

    def test_a_relabelled_train_subset_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["subset"] = dict(manifest["subset"], scale="S32")
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_different_train_anchor_membership_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["train_anchor_identities"] = manifest["train_anchor_identities"][:-1]
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_manifest_under_the_baseline_budget_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["training_lock"] = baseline_training_lock()
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_history_longer_than_the_locked_budget_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["loss_history"] = manifest["loss_history"] + [
            {"epoch": epoch, "train_mse": 0.3, "validation_mae": 0.5}
            for epoch in range(len(manifest["loss_history"]) + 1, BUDGET_MAX_EPOCHS + 2)
        ]
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_history_shorter_than_the_determinism_prefix_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["loss_history"] = manifest["loss_history"][
            : DETERMINISM_PREFIX_EPOCHS - 1
        ]
        manifest["selected_epoch"] = DETERMINISM_PREFIX_EPOCHS - 1
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_selected_epoch_that_contradicts_the_history_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["selected_epoch"] = 41
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_an_evaluation_that_contradicts_the_selected_epoch_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["evaluation"]["canonical_pooled_mae"] = 0.123
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_manifest_bound_to_another_execution_lock_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["execution_lock_identity"] = "d" * 64
        with self.assertRaises(BudgetError):
            self._validate(manifest)

    def test_a_manifest_naming_the_baseline_arm_is_rejected(self):
        manifest = _clone(self.budget)
        manifest["arm"] = BASELINE_ARM
        with self.assertRaises(BudgetError):
            self._validate(manifest)


class ResultTest(unittest.TestCase):
    """gates / determinism / comparison / outcomeのend-to-end re-derivation。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.retained_lock = retained_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.retained_lock
        )
        cls.retained = retained_manifest_value(cls.population, cls.retained_lock)
        cls.lock = budget_lock_value()

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _result(self, budget):
        with locked_identities_match(
            self.retained_lock, self.population, self.retained
        ):
            return budget_result_value(
                self.retained_lock, self.population, self.retained, budget, self.lock
            )

    def test_a_binding_cap_with_a_clear_improvement_is_budget_bound(self):
        budget = budget_manifest_value(self.population, self.lock, self.retained)
        value = self._result(budget)
        self.assertEqual(value["outcome"], BUDGET_BOUND)
        self.assertGreater(value["metrics"][BUDGET_ARM]["selected_epoch"], 40)
        self.assertEqual(value["comparison"]["classification"], CLEAR_IMPROVEMENT)
        self.assertTrue(all(value["gates"].values()))
        self.assertIs(value["determinism_gate"]["matched"], True)
        self.assertIs(value["structural_monotonicity"]["holds"], True)
        self.assertIs(value["formal_test"], False)

    def test_a_non_binding_cap_is_budget_sufficient(self):
        budget = budget_manifest_value(
            self.population,
            self.lock,
            self.retained,
            tail_maes=WORSENING_TAIL,
            per_game_mae=RETAINED_MAE,
        )
        value = self._result(budget)
        self.assertEqual(value["metrics"][BUDGET_ARM]["selected_epoch"], 40)
        self.assertEqual(value["outcome"], BUDGET_SUFFICIENT)

    def test_a_binding_cap_without_a_clear_improvement_is_budget_marginal(self):
        straddling = [
            RETAINED_MAE - 0.01 if index % 2 else RETAINED_MAE + 0.01
            for index in range(len(VALIDATION_SEEDS))
        ]
        budget = budget_manifest_value(
            self.population, self.lock, self.retained, per_game_mae=straddling
        )
        value = self._result(budget)
        self.assertGreater(value["metrics"][BUDGET_ARM]["selected_epoch"], 40)
        self.assertEqual(value["comparison"]["classification"], INCONCLUSIVE)
        self.assertEqual(value["outcome"], BUDGET_MARGINAL)

    def test_a_determinism_mismatch_is_stop_invalid_without_a_comparison(self):
        drifted = _clone(self.retained)
        drifted["loss_history"][0]["validation_mae"] += 1e-12
        budget = budget_manifest_value(self.population, self.lock, drifted)
        value = self._result(budget)
        self.assertIs(value["determinism_gate"]["matched"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertIsNone(value["comparison"])
        self.assertIsNone(value["structural_monotonicity"])

    def test_a_substituted_retained_artifact_is_stop_invalid(self):
        budget = budget_manifest_value(self.population, self.lock, self.retained)
        value = budget_result_value(
            self.retained_lock, self.population, self.retained, budget, self.lock
        )
        self.assertIs(value["gates"]["retained_artifact_identity"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertIsNone(value["comparison"])

    def test_a_self_rollout_failure_is_stop_invalid(self):
        budget = budget_manifest_value(
            self.population, self.lock, self.retained, self_rollout_failures=2
        )
        value = self._result(budget)
        self.assertIs(value["gates"][BUDGET_ARM + "_self_rollout_complete"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)

    def test_a_failed_physical_gate_is_stop_invalid(self):
        budget = budget_manifest_value(
            self.population, self.lock, self.retained, physical_passed=False
        )
        value = self._result(budget)
        self.assertIs(value["gates"][BUDGET_ARM + "_physical_validity"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)

    def test_a_changed_conditional_uniform_baseline_is_rejected(self):
        budget = budget_manifest_value(self.population, self.lock, self.retained)
        budget["evaluation"] = evaluation_cell(
            self.population,
            per_game_mae=0.38,
            canonical_mae=budget["evaluation"]["canonical_pooled_mae"],
            baseline=BASELINE_SNAPSHOT_MAE + 0.01,
        )
        with self.assertRaises(BudgetError):
            self._result(budget)

    def test_the_result_records_the_required_metrics_for_both_arms(self):
        value = self._result(
            budget_manifest_value(self.population, self.lock, self.retained)
        )
        self.assertEqual(set(value["metrics"]), set(ARMS))
        for arm in ARMS:
            metrics = value["metrics"][arm]
            for name in (
                "selected_epoch",
                "loss_history",
                "pooled_expected_count_mae",
                "conditional_uniform_baseline_mae",
                "per_hanchan_mae",
                "depth_1_mae",
                "depth_2_4_mae",
                "depth_5_8_mae",
                "depth_9_plus_mae",
                "physical_validity_passed",
                "finite_output",
                "self_rollout_failures",
                "training_cpu_seconds",
                "training_wall_seconds",
                "peak_process_ram_bytes",
            ):
                self.assertIn(name, metrics)
            self.assertEqual(len(metrics["per_hanchan_mae"]), len(VALIDATION_SEEDS))
        self.assertEqual(value["metrics"][BASELINE_ARM]["max_epochs"], 40)
        self.assertEqual(value["metrics"][BUDGET_ARM]["max_epochs"], 80)

    def test_the_result_records_the_cumulative_selection_exposure(self):
        value = self._result(
            budget_manifest_value(self.population, self.lock, self.retained)
        )
        self.assertEqual(value["selection_exposure"], dict(SELECTION_EXPOSURE))
        self.assertEqual(value["selection_exposure"]["cumulative_uses"], 2)
        self.assertIs(value["selection_exposure"]["formal_test"], False)
        self.assertIn("selection exposure", value["interpretation_boundary"])
        self.assertIn("formal superiority", value["interpretation_boundary"])

    def test_the_result_records_zero_new_generation(self):
        value = self._result(
            budget_manifest_value(self.population, self.lock, self.retained)
        )
        generation = value["cost_accounting"]["generation"]
        self.assertEqual(generation["new_hanchan"], 0)
        self.assertEqual(generation["new_seed"], 0)
        self.assertEqual(generation["new_corpus_generation"], 0)
        self.assertIs(
            value["cost_accounting"]["training"][BASELINE_ARM]["retrained"], False
        )

    def test_the_result_fields_are_exact(self):
        value = self._result(
            budget_manifest_value(self.population, self.lock, self.retained)
        )
        self.assertEqual(set(value), set(RESULT_FIELDS))

    def test_the_retained_artifact_gate_reads_the_locked_identities(self):
        self.assertIs(
            retained_artifact_gate(self.retained_lock, self.retained, self.population),
            False,
        )
        with locked_identities_match(
            self.retained_lock, self.population, self.retained
        ):
            self.assertIs(
                retained_artifact_gate(
                    self.retained_lock, self.retained, self.population
                ),
                True,
            )


class ResultArtifactTest(unittest.TestCase):
    """result artifactのcanonical readbackとtampered-but-self-consistent拒否。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.retained_lock = retained_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.retained_lock
        )
        cls.retained = retained_manifest_value(cls.population, cls.retained_lock)
        cls.lock = budget_lock_value()
        cls.budget = budget_manifest_value(cls.population, cls.lock, cls.retained)
        with locked_identities_match(cls.retained_lock, cls.population, cls.retained):
            cls.value = budget_result_value(
                cls.retained_lock,
                cls.population,
                cls.retained,
                cls.budget,
                cls.lock,
            )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _round_trip(self, value):
        with locked_identities_match(
            self.retained_lock, self.population, self.retained
        ):
            destination = self.root / f"result-{id(value)}.json"
            save_result(destination, value, self.lock)
            return load_result(destination, self.lock)

    def test_a_result_round_trips_with_its_logical_identity(self):
        loaded = self._round_trip(_clone(self.value))
        self.assertEqual(loaded["outcome"], self.value["outcome"])
        self.assertIn("result_identity", loaded)

    def test_an_existing_destination_is_never_overwritten(self):
        destination = self.root / "once.json"
        with locked_identities_match(
            self.retained_lock, self.population, self.retained
        ):
            save_result(destination, _clone(self.value), self.lock)
            with self.assertRaises(FileExistsError):
                save_result(destination, _clone(self.value), self.lock)

    def test_a_rewritten_outcome_is_rejected(self):
        tampered = _clone(self.value)
        tampered["outcome"] = BUDGET_BOUND
        tampered["reasons"] = ["hand-written"]
        with self.assertRaises(BudgetError):
            self._round_trip(tampered)

    def test_a_consistently_rewritten_comparison_is_rejected(self):
        tampered = _clone(self.value)
        tampered["comparison"]["interval_lower"] = 0.5
        tampered["comparison"]["interval_upper"] = 0.6
        tampered["comparison"]["classification"] = CLEAR_IMPROVEMENT
        with self.assertRaises(BudgetError):
            self._round_trip(tampered)

    def test_a_rewritten_determinism_gate_is_rejected(self):
        tampered = _clone(self.value)
        tampered["determinism_gate"]["matched"] = False
        with self.assertRaises(BudgetError):
            self._round_trip(tampered)

    def test_a_rewritten_metrics_block_is_rejected(self):
        tampered = _clone(self.value)
        tampered["metrics"][BUDGET_ARM]["selected_epoch"] = 12
        with self.assertRaises(BudgetError):
            self._round_trip(tampered)

    def test_a_rewritten_selection_exposure_is_rejected(self):
        tampered = _clone(self.value)
        tampered["selection_exposure"]["cumulative_uses"] = 1
        with self.assertRaises(BudgetError):
            self._round_trip(tampered)

    def test_a_result_with_extra_fields_is_rejected(self):
        tampered = _clone(self.value)
        tampered["note"] = "extra"
        with self.assertRaises(BudgetError):
            validate_result(tampered, self.lock)

    def test_a_result_missing_an_arm_is_rejected(self):
        tampered = _clone(self.value)
        del tampered["arms"][BUDGET_ARM]
        with self.assertRaises(BudgetError):
            validate_result(tampered, self.lock)

    def test_a_non_canonical_result_file_is_rejected(self):
        destination = self.root / "non-canonical.json"
        destination.write_bytes(b'{"b": 1, "a": 2}')
        with self.assertRaises(BudgetError):
            load_result(destination, self.lock)


class ComparisonPairingTest(unittest.TestCase):
    """paired comparisonのcluster contractとbootstrapのdeterminism。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.retained_lock = retained_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.retained_lock
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _cells(self, baseline_mae, budget_mae):
        return (
            evaluation_cell(
                self.population, per_game_mae=baseline_mae, canonical_mae=0.40
            ),
            evaluation_cell(
                self.population, per_game_mae=budget_mae, canonical_mae=0.38
            ),
        )

    def test_the_comparison_pairs_exactly_the_sixteen_validation_hanchan(self):
        baseline, budget = self._cells(0.40, 0.38)
        value = compare(baseline, budget)
        self.assertEqual(value["hanchan"], 16)
        self.assertEqual(len(value["per_hanchan_delta_mae"]), 16)
        self.assertEqual(
            [row["game_seed"] for row in value["per_hanchan_delta_mae"]],
            list(VALIDATION_SEEDS),
        )

    def test_the_statistic_is_positive_when_the_larger_budget_is_better(self):
        baseline, budget = self._cells(0.40, 0.38)
        value = compare(baseline, budget)
        self.assertGreater(value["pooled_delta_mae"], 0)
        self.assertEqual(value["classification"], CLEAR_IMPROVEMENT)
        self.assertEqual(value["baseline"], BASELINE_ARM)
        self.assertEqual(value["budget"], BUDGET_ARM)

    def test_the_bootstrap_is_deterministic_under_the_locked_seed(self):
        baseline, budget = self._cells(0.40, 0.39)
        first = compare(baseline, budget)
        second = compare(baseline, budget)
        self.assertEqual(first["interval_lower"], second["interval_lower"])
        self.assertEqual(first["interval_upper"], second["interval_upper"])
        self.assertEqual(first["bootstrap"], dict(BOOTSTRAP))

    def test_unpaired_anchor_identities_are_rejected(self):
        baseline, budget = self._cells(0.40, 0.38)
        budget["validation_anchor_identities"] = budget["validation_anchor_identities"][
            :-1
        ]
        with self.assertRaises(ScaleError):
            compare(baseline, budget)


class CliContractTest(unittest.TestCase):
    """CLIがresult-driven extension optionを持たないこと。"""

    def test_the_normal_import_and_cli_are_torch_free(self):
        """同一processのimport順に依存しないよう、独立したinterpreterで確認する。"""
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from lisjong_arena.stage3_epoch_budget.__main__ import _parser\n"
            "from lisjong_arena.stage3_epoch_budget import budget_training_lock\n"
            "_parser()\n"
            "budget_training_lock()\n"
            "raise SystemExit(1 if 'torch' in sys.modules else 0)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(completed.returncode, 0)

    def test_the_cli_offers_no_budget_or_rescue_option(self):
        from lisjong_arena.stage3_epoch_budget.__main__ import _parser

        parser = _parser()
        command = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command.choices), {"plan", "verify", "lock", "train", "compare"}
        )
        help_text = parser.format_help().lower()
        for forbidden in (
            "--max-epochs",
            "--epochs",
            "--patience",
            "--seed",
            "--learning-rate",
            "--extend",
            "160",
            "test partition",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_no_subcommand_accepts_an_epoch_or_seed_option(self):
        from lisjong_arena.stage3_epoch_budget.__main__ import _parser

        parser = _parser()
        command = next(action for action in parser._actions if action.dest == "command")
        for subparser in command.choices.values():
            options = {
                option
                for action in subparser._actions
                for option in action.option_strings
            }
            for forbidden in (
                "--max-epochs",
                "--epochs",
                "--patience",
                "--seed",
                "--seeds",
                "--learning-rate",
                "--scale",
                "--retrain-baseline",
            ):
                self.assertNotIn(forbidden, options)


class HistoricalBoundaryTest(unittest.TestCase):
    """#150 historical protocolを本childのために書き換えていないこと。"""

    def test_the_phase10_training_lock_still_caps_at_forty_epochs(self):
        config = baseline_training_lock()["training_config"]
        self.assertEqual(config["max_epochs"], 40)
        self.assertEqual(config["patience"], 6)

    def test_the_phase10_bootstrap_constants_are_unchanged(self):
        self.assertEqual(SCALE_BOOTSTRAP["replicates"], 10000)
        self.assertEqual(SCALE_BOOTSTRAP["seed"], 148)
        self.assertEqual(SCALE_BOOTSTRAP["order_statistic_indices"], [249, 9750])

    def test_the_phase10_split_is_unchanged(self):
        self.assertEqual(list(TRAIN_SEEDS), list(range(360, 424)))
        self.assertEqual(list(VALIDATION_SEEDS), list(range(424, 440)))


if __name__ == "__main__":
    unittest.main()
