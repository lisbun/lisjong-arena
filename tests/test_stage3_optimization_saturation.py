"""Arena #167 optimization-budget saturation studyのtorch非依存test。

retained #150 / #157 artifact identity、population / dataset / split mismatchの
拒否、epoch budget以外のconfig mismatchの拒否、execution lock identity /
readback、prefix-80 determinism gateのsuccessとmismatch、exhaustive outcomeの
5分岐、saturation record、tampered artifactの拒否、bootstrap定数の固定、
result-driven extension / rescue pathが存在しないことを固定する。

E160のlarge trainingはunit testへ入れない。#150 / #157 historical protocol /
seeds / schema / validatorは変更しないので、regression boundaryとしてそちら側の
constantsも合わせて固定する。
"""

import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from _stage3_optimization_saturation_fixtures import (
    BASELINE_MAE,
    BASELINE_SNAPSHOT_MAE,
    MID_SATURATION_TAIL,
    NON_IMPROVING_TAIL,
    SATURATION_ARENA_REVISION,
    baseline_manifest_value,
    evaluation_cell,
    locked_identities_match,
    phase10_lock_value,
    population_manifest,
    predecessor_lock_value,
    saturation_lock_value,
    saturation_manifest_value,
    saturation_result_value,
    scale_manifest_value,
)

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_epoch_budget.protocol import (
    BOOTSTRAP as BUDGET_BOOTSTRAP,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    BUDGET_MAX_EPOCHS,
    BudgetError,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    PATIENCE as BUDGET_PATIENCE,
)
from lisjong_arena.stage3_optimization_saturation.artifact import (
    load_result,
    save_result,
    validate_model_manifest,
)
from lisjong_arena.stage3_optimization_saturation.comparison import (
    classify_interval,
    compare,
    determinism_gate,
)
from lisjong_arena.stage3_optimization_saturation.lock import (
    comparison_semantics,
    validate_lock,
    validate_runtime,
)
from lisjong_arena.stage3_optimization_saturation.protocol import (
    ARMS,
    BASELINE_ARM,
    BASELINE_LOSS_HISTORY_DIGEST,
    BASELINE_MAX_EPOCHS,
    BASELINE_POOLED_MAE,
    BASELINE_SELECTED_EPOCH,
    BASELINE_SUFFICIENT,
    BASELINE_WEIGHTS_SHA256,
    BOOTSTRAP,
    BOUND_MARGINAL,
    CLASSIFICATIONS,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    DETERMINISM_PREFIX_EPOCHS,
    HARD_CAP_EPOCHS,
    INCONCLUSIVE,
    OUTCOMES,
    PATIENCE,
    PHASE10_EXECUTION_LOCK_IDENTITY,
    PREDECESSOR_EXECUTION_LOCK_IDENTITY,
    PREDECESSOR_RESULT_IDENTITY,
    PRIMARY_AXIS,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_TRAIN_ANCHORS,
    RETAINED_VALIDATION_ANCHORS,
    SATURATION_ARM,
    SATURATION_MAX_EPOCHS,
    SATURATION_OBSERVED,
    SELECTION_EXPOSURE,
    STILL_BOUND,
    STOP_INVALID,
    SaturationError,
    assert_bootstrap_constants_are_locked,
    assert_single_axis,
    baseline_training_lock,
    identity,
    saturation_training_config,
    saturation_training_lock,
)
from lisjong_arena.stage3_optimization_saturation.result import (
    RESULT_FIELDS,
    SATURATION_FIELDS,
    classify,
    retained_artifact_gate,
    validate_result,
)
from lisjong_arena.stage3_optimization_saturation.retained import (
    loss_history_digest,
    retained_value,
    validate_baseline_manifest,
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
    """Issue #167がlockした#150 / #157 artifact identityをそのまま持つこと。"""

    def test_the_locked_phase10_identities_are_the_issue_values(self):
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
            PHASE10_EXECUTION_LOCK_IDENTITY,
            "a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3",
        )

    def test_the_locked_predecessor_identities_are_the_issue_values(self):
        self.assertEqual(
            PREDECESSOR_EXECUTION_LOCK_IDENTITY,
            "5331af88bb2531d2fe3d34ca41cbc0ffffd0c6aaab63b0032265c15b7cccf55c",
        )
        self.assertEqual(
            PREDECESSOR_RESULT_IDENTITY,
            "700d8efb087e161d09581a10fe0b87c43c51a0f4cdb1e786f6ecc5c2852600ea",
        )
        self.assertEqual(
            BASELINE_WEIGHTS_SHA256,
            "2773fd8d61a5f8945c7663b959a1e1f0d9689e702bdb42ecd238c25ea9b30ea0",
        )
        self.assertEqual(BASELINE_SELECTED_EPOCH, 80)
        self.assertEqual(BASELINE_POOLED_MAE, 0.46259369600375433)
        self.assertEqual(len(BASELINE_LOSS_HISTORY_DIGEST), 64)

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
        self.assertIs(value["resumed_from_baseline_checkpoint"], False)
        self.assertEqual(value["baseline_selected_epoch"], BASELINE_SELECTED_EPOCH)
        self.assertEqual(value["baseline_max_epochs"], 80)
        self.assertEqual(value["saturation_max_epochs"], 160)
        self.assertEqual(value["predecessor_outcome"], "BUDGET BOUND")
        self.assertEqual(
            value["population_recipe"],
            "yakuhai-call primary + kan-coverage-yakuhai-call augmentation @ 12.5%",
        )

    def test_the_train_and_validation_seeds_come_from_the_phase10_plan(self):
        self.assertEqual(retained_value()["train_seeds"], list(TRAIN_SEEDS))
        self.assertEqual(retained_value()["validation_seeds"], list(VALIDATION_SEEDS))


class SingleAxisTest(unittest.TestCase):
    """変更してよいexperimental axisが`max_epochs`だけであること。"""

    def test_the_saturation_lock_differs_from_the_baseline_only_in_max_epochs(self):
        baseline = baseline_training_lock()
        saturation = saturation_training_lock()
        differing = {
            name
            for name in set(baseline) | set(saturation)
            if baseline.get(name) != saturation.get(name)
        }
        self.assertEqual(differing, {"training_config"})
        baseline_config = baseline["training_config"]
        saturation_config = saturation["training_config"]
        self.assertEqual(
            {
                name
                for name in set(baseline_config) | set(saturation_config)
                if baseline_config.get(name) != saturation_config.get(name)
            },
            {PRIMARY_AXIS},
        )
        self.assertEqual(baseline_config[PRIMARY_AXIS], BASELINE_MAX_EPOCHS)
        self.assertEqual(saturation_config[PRIMARY_AXIS], SATURATION_MAX_EPOCHS)
        self.assertEqual(BASELINE_MAX_EPOCHS, 80)
        self.assertEqual(SATURATION_MAX_EPOCHS, 160)

    def test_patience_stays_six_in_both_arms(self):
        self.assertEqual(PATIENCE, 6)
        self.assertEqual(baseline_training_lock()["training_config"]["patience"], 6)
        self.assertEqual(saturation_training_lock()["training_config"]["patience"], 6)

    def test_the_locked_optimizer_and_seed_settings_are_shared(self):
        baseline = baseline_training_lock()["training_config"]
        saturation = saturation_training_lock()["training_config"]
        for name in (
            "seed",
            "dataloader_seed",
            "learning_rate",
            "weight_decay",
            "workers",
            "deterministic_algorithms",
            "torch_threads",
        ):
            self.assertEqual(baseline[name], saturation[name])

    def test_the_locked_model_family_is_shared(self):
        baseline = baseline_training_lock()
        saturation = saturation_training_lock()
        for name in set(baseline) - {"training_config"}:
            self.assertEqual(baseline[name], saturation[name])

    def test_a_second_changed_config_field_is_rejected(self):
        saturation = saturation_training_lock()
        saturation["training_config"]["learning_rate"] = 0.0005
        with self.assertRaises(SaturationError):
            assert_single_axis(baseline_training_lock(), saturation)

    def test_a_changed_patience_is_rejected(self):
        saturation = saturation_training_lock()
        saturation["training_config"]["patience"] = 12
        with self.assertRaises(SaturationError):
            assert_single_axis(baseline_training_lock(), saturation)

    def test_a_changed_model_family_is_rejected(self):
        saturation = saturation_training_lock()
        saturation["parameter_count"] = 999_999
        with self.assertRaises(SaturationError):
            assert_single_axis(baseline_training_lock(), saturation)

    def test_a_changed_epoch_budget_value_is_rejected(self):
        for value in (80, 240, 320):
            saturation = saturation_training_lock()
            saturation["training_config"][PRIMARY_AXIS] = value
            with self.assertRaises(SaturationError):
                assert_single_axis(baseline_training_lock(), saturation)

    def test_the_saturation_training_config_only_moves_max_epochs(self):
        from dataclasses import asdict

        from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

        actual = asdict(saturation_training_config())
        expected = asdict(FORMAL_TRAINING_CONFIG)
        self.assertEqual(
            {name for name in expected if actual[name] != expected[name]},
            {PRIMARY_AXIS},
        )
        self.assertEqual(actual[PRIMARY_AXIS], SATURATION_MAX_EPOCHS)
        self.assertEqual(actual["patience"], PATIENCE)


class BootstrapConstantsTest(unittest.TestCase):
    """bootstrap定数を#150 / #157からそのまま引き継ぎ、本childで選び直さないこと。"""

    def test_the_bootstrap_constants_match_the_predecessor(self):
        assert_bootstrap_constants_are_locked()
        for name in (
            "unit",
            "replicates",
            "seed",
            "lower_percentile",
            "upper_percentile",
        ):
            self.assertEqual(BOOTSTRAP[name], BUDGET_BOOTSTRAP[name])
            self.assertEqual(BOOTSTRAP[name], SCALE_BOOTSTRAP[name])
        self.assertEqual(
            BOOTSTRAP["order_statistic_indices"],
            list(SCALE_BOOTSTRAP["order_statistic_indices"]),
        )

    def test_the_issue_bootstrap_values_are_recorded(self):
        self.assertEqual(BOOTSTRAP["unit"], "whole VALIDATION hanchan")
        self.assertEqual(BOOTSTRAP["replicates"], 10000)
        self.assertEqual(BOOTSTRAP["seed"], 148)
        self.assertEqual(BOOTSTRAP["lower_percentile"], 2.5)
        self.assertEqual(BOOTSTRAP["upper_percentile"], 97.5)
        self.assertEqual(BOOTSTRAP["order_statistic_indices"], [249, 9750])
        self.assertEqual(
            BOOTSTRAP["statistic"],
            "anchor-weighted pooled MAE(E80) - pooled MAE(E160)",
        )


class DeterminismGateTest(unittest.TestCase):
    """`E160.loss_history[0:80] == #157 E80.loss_history`をexactに扱うこと。"""

    def setUp(self):
        self.baseline = [
            {
                "epoch": epoch,
                "train_mse": 0.5 - epoch * 1e-3,
                "validation_mae": 0.5 - epoch * 1e-4,
            }
            for epoch in range(1, BASELINE_MAX_EPOCHS + 1)
        ]

    def _saturation(self, prefix, tail_length=10):
        return list(prefix) + [
            {
                "epoch": BASELINE_MAX_EPOCHS + offset,
                "train_mse": 0.3,
                "validation_mae": 0.4 - offset * 1e-4,
            }
            for offset in range(1, tail_length + 1)
        ]

    def test_the_prefix_is_eighty_epochs(self):
        self.assertEqual(DETERMINISM_PREFIX_EPOCHS, 80)

    def test_an_exact_prefix_passes(self):
        gate = determinism_gate(self.baseline, self._saturation(self.baseline))
        self.assertIs(gate["matched"], True)
        self.assertEqual(gate["prefix_epochs"], DETERMINISM_PREFIX_EPOCHS)
        self.assertEqual(gate["baseline_epochs"], BASELINE_MAX_EPOCHS)
        self.assertEqual(gate["saturation_epochs"], BASELINE_MAX_EPOCHS + 10)
        self.assertEqual(
            gate["baseline_prefix_digest"], gate["saturation_prefix_digest"]
        )
        self.assertEqual(
            gate["baseline_prefix_digest"], loss_history_digest(self.baseline)
        )

    def test_a_last_bit_difference_fails_the_gate(self):
        drifted = _clone(self.baseline)
        drifted[-1]["validation_mae"] += 1e-15
        gate = determinism_gate(self.baseline, self._saturation(drifted))
        self.assertIs(gate["matched"], False)
        self.assertNotEqual(
            gate["baseline_prefix_digest"], gate["saturation_prefix_digest"]
        )

    def test_the_gate_is_not_a_tolerance_comparison(self):
        """1 ULPのdriftでもmatchしない。toleranceで吸収しない。"""
        drifted = _clone(self.baseline)
        drifted[0]["train_mse"] = math.nextafter(drifted[0]["train_mse"], math.inf)
        self.assertNotEqual(drifted[0]["train_mse"], self.baseline[0]["train_mse"])
        self.assertIs(
            determinism_gate(self.baseline, self._saturation(drifted))["matched"],
            False,
        )

    def test_a_drift_inside_the_prefix_beyond_forty_epochs_is_caught(self):
        """#157 prefix (40) の外側で動いたhistoryも落ちること。"""
        drifted = _clone(self.baseline)
        drifted[59]["validation_mae"] = math.nextafter(
            drifted[59]["validation_mae"], math.inf
        )
        self.assertIs(
            determinism_gate(self.baseline, self._saturation(drifted))["matched"],
            False,
        )

    def test_a_short_saturation_history_is_rejected(self):
        with self.assertRaises(SaturationError):
            determinism_gate(self.baseline, self.baseline[:-1])

    def test_a_baseline_that_is_not_eighty_epochs_is_rejected(self):
        with self.assertRaises(SaturationError):
            determinism_gate(self.baseline[:-1], self._saturation(self.baseline))
        with self.assertRaises(SaturationError):
            determinism_gate(self.baseline[:40], self._saturation(self.baseline))


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
        with self.assertRaises(SaturationError):
            classify_interval(0.01, -0.01)


class OutcomeRuleTest(unittest.TestCase):
    """locked decision ruleのexhaustive 5分岐。"""

    PASSING = {
        "retained_artifact_identity": True,
        "determinism": True,
        BASELINE_ARM + "_physical_validity": True,
        SATURATION_ARM + "_physical_validity": True,
        BASELINE_ARM + "_self_rollout_complete": True,
        SATURATION_ARM + "_self_rollout_complete": True,
    }

    def test_the_outcome_set_is_exhaustive(self):
        self.assertEqual(
            set(OUTCOMES),
            {
                STOP_INVALID,
                BASELINE_SUFFICIENT,
                SATURATION_OBSERVED,
                STILL_BOUND,
                BOUND_MARGINAL,
            },
        )
        self.assertEqual(len(OUTCOMES), 5)

    def test_a_failed_determinism_gate_is_stop_invalid(self):
        gates = dict(self.PASSING, determinism=False)
        outcome, reasons = classify(gates, None, 120)
        self.assertEqual(outcome, STOP_INVALID)
        self.assertIn("determinism", reasons[0])

    def test_a_failed_retained_identity_gate_is_stop_invalid(self):
        gates = dict(self.PASSING, retained_artifact_identity=False)
        self.assertEqual(classify(gates, None, 120)[0], STOP_INVALID)

    def test_a_failed_physical_gate_is_stop_invalid(self):
        gates = dict(self.PASSING)
        gates[SATURATION_ARM + "_physical_validity"] = False
        self.assertEqual(classify(gates, None, 120)[0], STOP_INVALID)

    def test_a_failed_self_rollout_gate_is_stop_invalid(self):
        gates = dict(self.PASSING)
        gates[SATURATION_ARM + "_self_rollout_complete"] = False
        self.assertEqual(classify(gates, None, 120)[0], STOP_INVALID)

    def test_a_selected_epoch_within_eighty_is_baseline_sufficient(self):
        comparison = {"classification": CLEAR_IMPROVEMENT}
        for selected in (1, 40, 79, BASELINE_MAX_EPOCHS):
            self.assertEqual(
                classify(self.PASSING, comparison, selected)[0], BASELINE_SUFFICIENT
            )

    def test_a_selected_epoch_between_eighty_one_and_one_fifty_nine_is_saturation(self):
        for classification in (CLEAR_IMPROVEMENT, INCONCLUSIVE, CLEAR_REGRESSION):
            comparison = {"classification": classification}
            for selected in (81, 100, 159):
                outcome, reasons = classify(self.PASSING, comparison, selected)
                self.assertEqual(outcome, SATURATION_OBSERVED)
                self.assertIn("hard cap", reasons[0])

    def test_the_hard_cap_with_a_clear_improvement_is_still_bound(self):
        outcome, reasons = classify(
            self.PASSING, {"classification": CLEAR_IMPROVEMENT}, HARD_CAP_EPOCHS
        )
        self.assertEqual(outcome, STILL_BOUND)
        self.assertIn("does not extend to 320 epochs", reasons[0])

    def test_the_hard_cap_without_a_clear_improvement_is_bound_marginal(self):
        for classification in (INCONCLUSIVE, CLEAR_REGRESSION):
            outcome, reasons = classify(
                self.PASSING, {"classification": classification}, HARD_CAP_EPOCHS
            )
            self.assertEqual(outcome, BOUND_MARGINAL)
            self.assertIn("not clear", reasons[0])
            self.assertIn("does not extend to 320 epochs", reasons[0])

    def test_passing_gates_require_a_comparison(self):
        with self.assertRaises(SaturationError):
            classify(self.PASSING, None, 120)


class ExecutionLockTest(unittest.TestCase):
    """execution lock identity / readbackとpre-exposure contract。"""

    def test_a_well_formed_lock_is_accepted(self):
        self.assertEqual(
            validate_lock(saturation_lock_value()), saturation_lock_value()
        )

    def test_the_lock_identity_is_stable_and_readback_safe(self):
        lock = saturation_lock_value()
        encoded = canonical_json_bytes(lock)
        self.assertEqual(identity(json.loads(encoded)), identity(lock))
        self.assertEqual(identity(validate_lock(json.loads(encoded))), identity(lock))

    def test_the_lock_carries_the_retained_identities_and_saturation_semantics(self):
        lock = saturation_lock_value()
        self.assertEqual(lock["retained"], retained_value())
        self.assertEqual(lock["baseline_training_lock"], baseline_training_lock())
        self.assertEqual(lock["saturation_training_lock"], saturation_training_lock())
        self.assertEqual(lock["comparison"], comparison_semantics())
        self.assertEqual(lock["comparison"]["baseline_max_epochs"], 80)
        self.assertEqual(lock["comparison"]["saturation_max_epochs"], 160)
        self.assertEqual(lock["comparison"]["hard_cap_epochs"], 160)
        self.assertEqual(lock["comparison"]["patience"], PATIENCE)
        self.assertEqual(lock["comparison"]["bootstrap"], dict(BOOTSTRAP))
        self.assertEqual(lock["comparison"]["determinism_prefix_epochs"], 80)
        self.assertEqual(lock["comparison"]["budget"]["new_hanchan"], 0)
        self.assertEqual(lock["comparison"]["budget"]["baseline_arm_trainings"], 0)
        self.assertEqual(lock["comparison"]["budget"]["saturation_arm_trainings"], 1)
        self.assertEqual(lock["comparison"]["budget"]["formal_test_exposure"], 0)
        self.assertEqual(
            lock["comparison"]["selection_exposure"], dict(SELECTION_EXPOSURE)
        )
        self.assertIn("320 epochs", lock["comparison"]["no_extension_rule"])
        self.assertIn("#166", lock["comparison"]["no_extension_rule"])
        self.assertIn("does not resume", lock["comparison"]["resume_rule"])

    def test_a_result_exposed_lock_is_rejected(self):
        with self.assertRaises(SaturationError):
            validate_lock(saturation_lock_value(result_exposed=True))

    def test_a_missing_artifact_audit_is_rejected(self):
        with self.assertRaises(SaturationError):
            validate_lock(saturation_lock_value(artifact_audit="   "))

    def test_extra_or_missing_lock_fields_are_rejected(self):
        extra = saturation_lock_value()
        extra["note"] = "extra"
        with self.assertRaises(SaturationError):
            validate_lock(extra)
        missing = saturation_lock_value()
        del missing["comparison"]
        with self.assertRaises(SaturationError):
            validate_lock(missing)

    def test_a_lock_that_widens_the_axis_is_rejected(self):
        lock = saturation_lock_value()
        lock["saturation_training_lock"]["training_config"]["patience"] = 12
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_lock_that_moves_the_epoch_budget_is_rejected(self):
        for value in (80, 320):
            lock = saturation_lock_value()
            lock["saturation_training_lock"]["training_config"][PRIMARY_AXIS] = value
            with self.assertRaises(SaturationError):
                validate_lock(lock)

    def test_a_lock_that_moves_the_baseline_budget_is_rejected(self):
        lock = saturation_lock_value()
        lock["baseline_training_lock"]["training_config"][PRIMARY_AXIS] = 40
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_lock_that_edits_the_bootstrap_constants_is_rejected(self):
        lock = saturation_lock_value()
        lock["comparison"]["bootstrap"]["seed"] = 149
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_lock_that_edits_the_selection_exposure_is_rejected(self):
        lock = saturation_lock_value()
        lock["comparison"]["selection_exposure"]["cumulative_uses"] = 1
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_lock_that_edits_a_retained_identity_is_rejected(self):
        lock = saturation_lock_value()
        lock["retained"]["baseline_weights_sha256"] = "0" * 64
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_an_unpinned_source_revision_is_rejected(self):
        lock = saturation_lock_value()
        lock["provenance"]["source_revisions"]["lisjong"] = "0" * 40
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_an_unresolved_provenance_is_rejected(self):
        lock = saturation_lock_value()
        lock["provenance"]["fully_resolved"] = False
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_gpu_or_threaded_runtime_is_rejected(self):
        for override in (
            {"device": "cuda"},
            {"torch_threads": 4},
            {"deterministic_algorithms": False},
            {"free_threaded": True},
        ):
            lock = saturation_lock_value()
            lock["runtime"].update(override)
            lock["predecessor_runtime"].update(override)
            with self.assertRaises(SaturationError):
                validate_lock(lock)

    def test_a_runtime_that_differs_from_the_predecessor_numeric_runtime_is_rejected(
        self,
    ):
        lock = saturation_lock_value()
        lock["runtime"]["torch"] = "2.13.0"
        lock["predecessor_runtime"]["torch"] = "2.13.0+cpu"
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_a_platform_difference_is_recorded_rather_than_fatal(self):
        lock = saturation_lock_value()
        lock["runtime"]["platform"] = "Windows-11-other"
        lock["platform_matches_predecessor"] = False
        validate_lock(lock)
        validate_runtime(lock["runtime"], lock["predecessor_runtime"])

    def test_a_misreported_platform_agreement_is_rejected(self):
        lock = saturation_lock_value()
        lock["runtime"]["platform"] = "Windows-11-other"
        with self.assertRaises(SaturationError):
            validate_lock(lock)

    def test_the_arena_revision_differs_from_the_predecessor_execution_revision(self):
        lock = saturation_lock_value()
        self.assertEqual(
            lock["provenance"]["source_revisions"]["lisjong_arena"],
            SATURATION_ARENA_REVISION,
        )
        self.assertNotEqual(
            lock["provenance"]["source_revisions"]["lisjong_arena"],
            lock["retained"]["predecessor_execution_lock_identity"],
        )


class RetainedReadbackTest(unittest.TestCase):
    """#157 E80 manifestのexact identity validation。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.phase10_lock
        )
        cls.scale = scale_manifest_value(cls.population, cls.phase10_lock)
        cls.predecessor_lock = predecessor_lock_value()
        cls.baseline = baseline_manifest_value(
            cls.population, cls.predecessor_lock, cls.scale
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_a_substituted_arm_label_is_rejected(self):
        manifest = _clone(self.baseline)
        manifest["arm"] = SATURATION_ARM
        with self.assertRaises(SaturationError):
            validate_baseline_manifest(manifest)

    def test_a_substituted_weights_digest_is_rejected(self):
        manifest = _clone(self.baseline)
        manifest["weights_sha256"] = "f" * 64
        with self.assertRaises(SaturationError):
            validate_baseline_manifest(manifest)

    def test_a_baseline_arm_trained_under_a_different_budget_is_rejected(self):
        manifest = _clone(self.baseline)
        manifest["training_lock"] = saturation_training_lock()
        with self.assertRaises(SaturationError):
            validate_baseline_manifest(manifest)

    def test_a_baseline_arm_with_a_longer_history_is_rejected(self):
        manifest = _clone(self.baseline)
        manifest["loss_history"] = manifest["loss_history"] + [
            {"epoch": 81, "train_mse": 0.3, "validation_mae": 0.37}
        ]
        with self.assertRaises(SaturationError):
            validate_baseline_manifest(manifest)

    def test_a_baseline_arm_whose_history_digest_moved_is_rejected(self):
        """1 epoch分でも書き換えられたE80 historyは、他が整合していても落ちる。"""
        manifest = _clone(self.baseline)
        manifest["loss_history"][10]["train_mse"] += 1e-9
        with self.assertRaises(SaturationError):
            validate_baseline_manifest(manifest)


class ModelArtifactTest(unittest.TestCase):
    """E160 model manifestのbindingとtampered artifactの拒否。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.phase10_lock
        )
        cls.scale = scale_manifest_value(cls.population, cls.phase10_lock)
        cls.predecessor_lock = predecessor_lock_value()
        cls.baseline = baseline_manifest_value(
            cls.population, cls.predecessor_lock, cls.scale
        )
        cls.lock = saturation_lock_value()
        cls.saturation = saturation_manifest_value(
            cls.population, cls.lock, cls.baseline
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _validate(self, manifest):
        return validate_model_manifest(
            manifest, self.population, self.lock, self.baseline
        )

    def test_a_well_formed_saturation_manifest_is_accepted(self):
        self._validate(_clone(self.saturation))

    def test_the_saturation_arm_shares_the_retained_train_subset(self):
        self.assertEqual(self.saturation["subset"], self.baseline["subset"])
        self.assertEqual(
            self.saturation["train_anchor_identities"],
            self.baseline["train_anchor_identities"],
        )

    def test_a_relabelled_train_subset_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["subset"] = dict(manifest["subset"], scale="S32")
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_different_train_anchor_membership_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["train_anchor_identities"] = manifest["train_anchor_identities"][:-1]
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_manifest_under_the_baseline_budget_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["training_lock"] = baseline_training_lock()
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_history_longer_than_the_locked_budget_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["loss_history"] = manifest["loss_history"] + [
            {
                "epoch": SATURATION_MAX_EPOCHS + 1,
                "train_mse": 0.3,
                "validation_mae": 0.5,
            }
        ]
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_history_shorter_than_the_determinism_prefix_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["loss_history"] = manifest["loss_history"][
            : DETERMINISM_PREFIX_EPOCHS - 1
        ]
        manifest["selected_epoch"] = DETERMINISM_PREFIX_EPOCHS - 1
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_selected_epoch_that_contradicts_the_history_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["selected_epoch"] = 81
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_an_evaluation_that_contradicts_the_selected_epoch_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["evaluation"]["canonical_pooled_mae"] = 0.123
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_manifest_bound_to_another_execution_lock_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["execution_lock_identity"] = "d" * 64
        with self.assertRaises(SaturationError):
            self._validate(manifest)

    def test_a_manifest_naming_the_baseline_arm_is_rejected(self):
        manifest = _clone(self.saturation)
        manifest["arm"] = BASELINE_ARM
        with self.assertRaises(SaturationError):
            self._validate(manifest)


class ResultTest(unittest.TestCase):
    """gates / determinism / comparison / saturation / outcomeのre-derivation。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.phase10_lock
        )
        cls.scale = scale_manifest_value(cls.population, cls.phase10_lock)
        cls.predecessor_lock = predecessor_lock_value()
        cls.baseline = baseline_manifest_value(
            cls.population, cls.predecessor_lock, cls.scale
        )
        cls.lock = saturation_lock_value()

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _result(self, saturation):
        with locked_identities_match(
            self.phase10_lock,
            self.population,
            self.scale,
            self.predecessor_lock,
            self.baseline,
        ):
            return saturation_result_value(
                self.phase10_lock,
                self.population,
                self.scale,
                self.predecessor_lock,
                self.baseline,
                saturation,
                self.lock,
            )

    def test_a_hard_cap_selection_with_a_clear_improvement_is_still_bound(self):
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline
        )
        value = self._result(saturation)
        self.assertEqual(value["outcome"], STILL_BOUND)
        self.assertEqual(
            value["metrics"][SATURATION_ARM]["selected_epoch"], HARD_CAP_EPOCHS
        )
        self.assertEqual(value["comparison"]["classification"], CLEAR_IMPROVEMENT)
        self.assertTrue(all(value["gates"].values()))
        self.assertIs(value["determinism_gate"]["matched"], True)
        self.assertIs(value["structural_monotonicity"]["holds"], True)
        self.assertIs(value["formal_test"], False)
        self.assertIs(value["saturation"]["selected_epoch_at_hard_cap"], True)
        self.assertEqual(value["saturation"]["margin_to_hard_cap"], 0)
        self.assertIs(value["saturation"]["early_stopped"], False)

    def test_a_selection_within_eighty_is_baseline_sufficient(self):
        saturation = saturation_manifest_value(
            self.population,
            self.lock,
            self.baseline,
            tail_maes=NON_IMPROVING_TAIL,
            per_game_mae=BASELINE_MAE,
        )
        value = self._result(saturation)
        self.assertEqual(value["metrics"][SATURATION_ARM]["selected_epoch"], 80)
        self.assertEqual(value["metrics"][SATURATION_ARM]["epochs_run"], 86)
        self.assertEqual(value["outcome"], BASELINE_SUFFICIENT)
        self.assertIs(value["saturation"]["early_stopped"], True)
        self.assertEqual(value["saturation"]["margin_to_hard_cap"], 80)

    def test_a_selection_between_the_budgets_is_saturation_observed(self):
        saturation = saturation_manifest_value(
            self.population,
            self.lock,
            self.baseline,
            tail_maes=MID_SATURATION_TAIL,
        )
        value = self._result(saturation)
        self.assertEqual(value["metrics"][SATURATION_ARM]["selected_epoch"], 100)
        self.assertEqual(value["metrics"][SATURATION_ARM]["epochs_run"], 106)
        self.assertEqual(value["outcome"], SATURATION_OBSERVED)
        self.assertIs(value["saturation"]["early_stopped"], True)
        self.assertIs(value["saturation"]["selected_epoch_at_hard_cap"], False)
        self.assertEqual(value["saturation"]["margin_to_hard_cap"], 60)

    def test_a_hard_cap_selection_without_a_clear_improvement_is_bound_marginal(self):
        straddling = [
            BASELINE_MAE - 0.01 if index % 2 else BASELINE_MAE + 0.01
            for index in range(len(VALIDATION_SEEDS))
        ]
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline, per_game_mae=straddling
        )
        value = self._result(saturation)
        self.assertEqual(
            value["metrics"][SATURATION_ARM]["selected_epoch"], HARD_CAP_EPOCHS
        )
        self.assertEqual(value["comparison"]["classification"], INCONCLUSIVE)
        self.assertEqual(value["outcome"], BOUND_MARGINAL)

    def test_a_determinism_mismatch_is_stop_invalid_without_a_comparison(self):
        drifted = _clone(self.baseline)
        drifted["loss_history"][0]["validation_mae"] += 1e-12
        saturation = saturation_manifest_value(self.population, self.lock, drifted)
        value = self._result(saturation)
        self.assertIs(value["determinism_gate"]["matched"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertIsNone(value["comparison"])
        self.assertIsNone(value["structural_monotonicity"])

    def test_a_substituted_retained_artifact_is_stop_invalid(self):
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline
        )
        value = saturation_result_value(
            self.phase10_lock,
            self.population,
            self.scale,
            self.predecessor_lock,
            self.baseline,
            saturation,
            self.lock,
        )
        self.assertIs(value["gates"]["retained_artifact_identity"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertIsNone(value["comparison"])

    def test_a_self_rollout_failure_is_stop_invalid(self):
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline, self_rollout_failures=2
        )
        value = self._result(saturation)
        self.assertIs(value["gates"][SATURATION_ARM + "_self_rollout_complete"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)

    def test_a_failed_physical_gate_is_stop_invalid(self):
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline, physical_passed=False
        )
        value = self._result(saturation)
        self.assertIs(value["gates"][SATURATION_ARM + "_physical_validity"], False)
        self.assertEqual(value["outcome"], STOP_INVALID)

    def test_a_changed_conditional_uniform_baseline_is_rejected(self):
        saturation = saturation_manifest_value(
            self.population, self.lock, self.baseline
        )
        saturation["evaluation"] = evaluation_cell(
            self.population,
            per_game_mae=0.36,
            canonical_mae=saturation["evaluation"]["canonical_pooled_mae"],
            baseline=BASELINE_SNAPSHOT_MAE + 0.01,
        )
        with self.assertRaises(SaturationError):
            self._result(saturation)

    def test_the_result_records_the_required_metrics_for_both_arms(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        self.assertEqual(set(value["metrics"]), set(ARMS))
        for arm in ARMS:
            metrics = value["metrics"][arm]
            for name in (
                "max_epochs",
                "patience",
                "epochs_run",
                "selected_epoch",
                "loss_history",
                "loss_history_digest",
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
            self.assertEqual(metrics["patience"], PATIENCE)
        self.assertEqual(value["metrics"][BASELINE_ARM]["max_epochs"], 80)
        self.assertEqual(value["metrics"][SATURATION_ARM]["max_epochs"], 160)

    def test_the_result_records_the_required_paired_and_determinism_evidence(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        for name in (
            "pooled_delta_mae",
            "interval_lower",
            "interval_upper",
            "classification",
            "positive_hanchan",
            "negative_hanchan",
        ):
            self.assertIn(name, value["comparison"])
        self.assertEqual(value["comparison"]["positive_hanchan"], len(VALIDATION_SEEDS))
        self.assertEqual(value["comparison"]["negative_hanchan"], 0)
        for name in (
            "baseline_prefix_digest",
            "saturation_prefix_digest",
            "matched",
            "prefix_epochs",
        ):
            self.assertIn(name, value["determinism_gate"])

    def test_the_saturation_record_fields_are_exact(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        self.assertEqual(set(value["saturation"]), set(SATURATION_FIELDS))
        self.assertEqual(value["saturation"]["hard_cap_epochs"], 160)
        self.assertEqual(value["saturation"]["patience"], 6)
        self.assertEqual(value["saturation"]["baseline_selected_epoch"], 80)
        self.assertEqual(value["saturation"]["outcome"], value["outcome"])

    def test_the_result_records_the_cumulative_selection_exposure(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        self.assertEqual(value["selection_exposure"], dict(SELECTION_EXPOSURE))
        self.assertEqual(value["selection_exposure"]["cumulative_uses"], 3)
        self.assertIs(value["selection_exposure"]["formal_test"], False)
        self.assertEqual(len(value["selection_exposure"]["prior_studies"]), 2)
        self.assertIn("selection exposure", value["interpretation_boundary"])
        self.assertIn("formal superiority", value["interpretation_boundary"])
        self.assertIn("production-optimal", value["interpretation_boundary"])

    def test_the_result_records_zero_new_generation_and_no_baseline_retraining(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        generation = value["cost_accounting"]["generation"]
        self.assertEqual(generation["new_hanchan"], 0)
        self.assertEqual(generation["new_seed"], 0)
        self.assertEqual(generation["new_corpus_generation"], 0)
        self.assertIs(
            value["cost_accounting"]["training"][BASELINE_ARM]["retrained"], False
        )
        self.assertEqual(
            value["cost_accounting"]["training"][BASELINE_ARM]["reused_cost"],
            dict(self.baseline["cost"]),
        )

    def test_the_result_records_the_no_extension_rule(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        self.assertIn("320 epochs", value["no_extension_rule"])
        self.assertIn("#166", value["no_extension_rule"])
        self.assertIn("no retry", value["retry_rule"])
        self.assertIn("resuming from the E80 checkpoint", value["retry_rule"])

    def test_the_result_fields_are_exact(self):
        value = self._result(
            saturation_manifest_value(self.population, self.lock, self.baseline)
        )
        self.assertEqual(set(value), set(RESULT_FIELDS))

    def test_the_retained_artifact_gate_reads_the_locked_identities(self):
        self.assertIs(
            retained_artifact_gate(
                self.phase10_lock,
                self.scale,
                self.predecessor_lock,
                self.baseline,
                self.population,
            ),
            False,
        )
        with locked_identities_match(
            self.phase10_lock,
            self.population,
            self.scale,
            self.predecessor_lock,
            self.baseline,
        ):
            self.assertIs(
                retained_artifact_gate(
                    self.phase10_lock,
                    self.scale,
                    self.predecessor_lock,
                    self.baseline,
                    self.population,
                ),
                True,
            )


class ResultArtifactTest(unittest.TestCase):
    """result artifactのcanonical readbackとtampered-but-self-consistent拒否。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.phase10_lock
        )
        cls.scale = scale_manifest_value(cls.population, cls.phase10_lock)
        cls.predecessor_lock = predecessor_lock_value()
        cls.baseline = baseline_manifest_value(
            cls.population, cls.predecessor_lock, cls.scale
        )
        cls.lock = saturation_lock_value()
        cls.saturation = saturation_manifest_value(
            cls.population, cls.lock, cls.baseline
        )
        with locked_identities_match(
            cls.phase10_lock,
            cls.population,
            cls.scale,
            cls.predecessor_lock,
            cls.baseline,
        ):
            cls.value = saturation_result_value(
                cls.phase10_lock,
                cls.population,
                cls.scale,
                cls.predecessor_lock,
                cls.baseline,
                cls.saturation,
                cls.lock,
            )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _round_trip(self, value):
        with locked_identities_match(
            self.phase10_lock,
            self.population,
            self.scale,
            self.predecessor_lock,
            self.baseline,
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
            self.phase10_lock,
            self.population,
            self.scale,
            self.predecessor_lock,
            self.baseline,
        ):
            save_result(destination, _clone(self.value), self.lock)
            with self.assertRaises(FileExistsError):
                save_result(destination, _clone(self.value), self.lock)

    def test_a_rewritten_outcome_is_rejected(self):
        tampered = _clone(self.value)
        tampered["outcome"] = SATURATION_OBSERVED
        tampered["reasons"] = ["hand-written"]
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_consistently_rewritten_comparison_is_rejected(self):
        tampered = _clone(self.value)
        tampered["comparison"]["interval_lower"] = 0.5
        tampered["comparison"]["interval_upper"] = 0.6
        tampered["comparison"]["classification"] = CLEAR_IMPROVEMENT
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_rewritten_determinism_gate_is_rejected(self):
        tampered = _clone(self.value)
        tampered["determinism_gate"]["matched"] = False
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_rewritten_saturation_record_is_rejected(self):
        tampered = _clone(self.value)
        tampered["saturation"]["selected_epoch"] = 100
        tampered["saturation"]["margin_to_hard_cap"] = 60
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_tampered_baseline_arm_is_rejected(self):
        """E80 armのhistoryを書き換えたresultは再導出で落ちる。"""
        tampered = _clone(self.value)
        tampered["arms"][BASELINE_ARM]["loss_history"][0]["train_mse"] += 1e-9
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_tampered_saturation_arm_is_rejected(self):
        """E160 armのtail historyを書き換えたresultは再導出で落ちる。

        weights digestそのものはresult内部に照合先を持たない。artifact fileと
        manifestの対応は`load_model_artifact()`がSHA-256とstrict S2 loadで
        別に確認する。
        """
        for tamper in (
            lambda arm: arm["loss_history"][-1].__setitem__("validation_mae", 0.30),
            lambda arm: arm.__setitem__("selected_epoch", 100),
            lambda arm: arm["cost"].__setitem__("training_cpu_seconds", 1.0),
            lambda arm: arm["training_lock"]["training_config"].__setitem__(
                "max_epochs", 320
            ),
        ):
            tampered = _clone(self.value)
            tamper(tampered["arms"][SATURATION_ARM])
            with self.assertRaises(SaturationError):
                self._round_trip(tampered)

    def test_a_rewritten_metrics_block_is_rejected(self):
        tampered = _clone(self.value)
        tampered["metrics"][SATURATION_ARM]["selected_epoch"] = 12
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_rewritten_selection_exposure_is_rejected(self):
        tampered = _clone(self.value)
        tampered["selection_exposure"]["cumulative_uses"] = 2
        with self.assertRaises(SaturationError):
            self._round_trip(tampered)

    def test_a_result_with_extra_fields_is_rejected(self):
        tampered = _clone(self.value)
        tampered["note"] = "extra"
        with self.assertRaises(SaturationError):
            validate_result(tampered, self.lock)

    def test_a_result_missing_an_arm_is_rejected(self):
        tampered = _clone(self.value)
        del tampered["arms"][SATURATION_ARM]
        with self.assertRaises(SaturationError):
            validate_result(tampered, self.lock)

    def test_a_non_canonical_result_file_is_rejected(self):
        destination = self.root / "non-canonical.json"
        destination.write_bytes(b'{"b": 1, "a": 2}')
        with self.assertRaises(SaturationError):
            load_result(destination, self.lock)


class ComparisonPairingTest(unittest.TestCase):
    """paired comparisonのcluster contractとbootstrapのdeterminism。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.phase10_lock = phase10_lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(
            cls.root, cls.phase10_lock
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _cells(self, baseline_mae, saturation_mae):
        return (
            evaluation_cell(
                self.population, per_game_mae=baseline_mae, canonical_mae=0.38
            ),
            evaluation_cell(
                self.population, per_game_mae=saturation_mae, canonical_mae=0.36
            ),
        )

    def test_the_comparison_pairs_exactly_the_sixteen_validation_hanchan(self):
        baseline, saturation = self._cells(0.38, 0.36)
        value = compare(baseline, saturation)
        self.assertEqual(value["hanchan"], 16)
        self.assertEqual(len(value["per_hanchan_delta_mae"]), 16)
        self.assertEqual(
            [row["game_seed"] for row in value["per_hanchan_delta_mae"]],
            list(VALIDATION_SEEDS),
        )

    def test_the_statistic_is_positive_when_the_larger_budget_is_better(self):
        baseline, saturation = self._cells(0.38, 0.36)
        value = compare(baseline, saturation)
        self.assertGreater(value["pooled_delta_mae"], 0)
        self.assertEqual(value["classification"], CLEAR_IMPROVEMENT)
        self.assertEqual(value["baseline"], BASELINE_ARM)
        self.assertEqual(value["saturation"], SATURATION_ARM)
        self.assertEqual(value["positive_hanchan"], 16)
        self.assertEqual(value["negative_hanchan"], 0)
        self.assertEqual(value["tied_hanchan"], 0)

    def test_the_bootstrap_is_deterministic_under_the_locked_seed(self):
        baseline, saturation = self._cells(0.38, 0.379)
        first = compare(baseline, saturation)
        second = compare(baseline, saturation)
        self.assertEqual(first["interval_lower"], second["interval_lower"])
        self.assertEqual(first["interval_upper"], second["interval_upper"])
        self.assertEqual(first["bootstrap"], dict(BOOTSTRAP))

    def test_unpaired_anchor_identities_are_rejected(self):
        baseline, saturation = self._cells(0.38, 0.36)
        saturation["validation_anchor_identities"] = saturation[
            "validation_anchor_identities"
        ][:-1]
        with self.assertRaises(ScaleError):
            compare(baseline, saturation)


class CliContractTest(unittest.TestCase):
    """CLIがresult-driven extension / rescue optionを持たないこと。"""

    def test_the_normal_import_and_cli_are_torch_free(self):
        """同一processのimport順に依存しないよう、独立したinterpreterで確認する。"""
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from lisjong_arena.stage3_optimization_saturation.__main__ import _parser\n"
            "from lisjong_arena.stage3_optimization_saturation import (\n"
            "    saturation_training_lock,\n"
            ")\n"
            "_parser()\n"
            "saturation_training_lock()\n"
            "raise SystemExit(1 if 'torch' in sys.modules else 0)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(completed.returncode, 0)

    def test_the_cli_offers_no_budget_extension_or_rescue_option(self):
        from lisjong_arena.stage3_optimization_saturation.__main__ import _parser

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
            "--resume",
            "--retrain",
            "320",
            "test partition",
        ):
            self.assertNotIn(forbidden, help_text)

    def test_no_subcommand_accepts_an_epoch_seed_resume_or_rescue_option(self):
        from lisjong_arena.stage3_optimization_saturation.__main__ import _parser

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
                "--resume",
                "--resume-from",
                "--extend",
                "--force",
            ):
                self.assertNotIn(forbidden, options)

    def test_no_module_exposes_a_three_twenty_epoch_budget(self):
        """本childのpublic APIに320 epochへ進むentry pointが無いこと。"""
        from lisjong_arena import stage3_optimization_saturation as package

        for name in package.__all__:
            value = getattr(package, name)
            self.assertNotEqual(value, 320)
            if type(value) is dict:
                self.assertNotIn(320, value.values())


class HistoricalBoundaryTest(unittest.TestCase):
    """#150 / #157 historical protocolを本childのために書き換えていないこと。"""

    def test_the_epoch_budget_training_lock_still_caps_at_eighty_epochs(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            budget_training_lock,
        )

        config = budget_training_lock()["training_config"]
        self.assertEqual(config["max_epochs"], BUDGET_MAX_EPOCHS)
        self.assertEqual(config["max_epochs"], 80)
        self.assertEqual(config["patience"], BUDGET_PATIENCE)

    def test_the_epoch_budget_determinism_prefix_is_still_forty(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            DETERMINISM_PREFIX_EPOCHS as BUDGET_PREFIX,
        )

        self.assertEqual(BUDGET_PREFIX, 40)

    def test_the_epoch_budget_outcomes_are_unchanged(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            OUTCOMES as BUDGET_OUTCOMES,
        )

        self.assertEqual(
            set(BUDGET_OUTCOMES),
            {
                "STOP / INVALID",
                "BUDGET SUFFICIENT",
                "BUDGET BOUND",
                "BUDGET MARGINAL",
            },
        )

    def test_the_epoch_budget_selection_exposure_still_reads_two(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            SELECTION_EXPOSURE as BUDGET_EXPOSURE,
        )

        self.assertEqual(BUDGET_EXPOSURE["cumulative_uses"], 2)

    def test_the_epoch_budget_error_type_is_still_raised_by_its_own_validators(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            assert_single_axis as budget_assert_single_axis,
        )
        from lisjong_arena.stage3_epoch_budget.protocol import (
            baseline_training_lock as phase10_training_lock,
        )

        broken = saturation_training_lock()
        with self.assertRaises(BudgetError):
            budget_assert_single_axis(phase10_training_lock(), broken)

    def test_the_phase10_bootstrap_constants_are_unchanged(self):
        self.assertEqual(SCALE_BOOTSTRAP["replicates"], 10000)
        self.assertEqual(SCALE_BOOTSTRAP["seed"], 148)
        self.assertEqual(SCALE_BOOTSTRAP["order_statistic_indices"], [249, 9750])

    def test_the_phase10_split_is_unchanged(self):
        self.assertEqual(list(TRAIN_SEEDS), list(range(360, 424)))
        self.assertEqual(list(VALIDATION_SEEDS), list(range(424, 440)))


if __name__ == "__main__":
    unittest.main()
