"""Issue #173 shanten guard torch-free tests.

guard candidate logical identity、`GuardDecisionSample` / `GuardDiagnostics`の
内部不変条件、seed plan、result document validation、classification ladderを
torchなしで固定する。実RiichiEnvもreal `#162` candidateも使わない。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_p1_shanten_guard_fixtures import (
    FIXTURE_DATASET_IDENTITY,
    FIXTURE_SUPPORT_DIGEST,
    FIXTURE_WEIGHTS_DIGEST,
    KEEP_TILES,
    WORSEN_TILES,
    diagnostic_result_document,
    fixture_activation_diagnostics,
    fixture_base_candidate_identity,
    fixture_binding,
    fixture_checkpoint,
    fixture_guard_diagnostics,
    inconclusive_delta,
    locked_checkpoint,
    negative_delta,
    policy_input,
    positive_delta,
)

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_input.feature import TILE_AXIS
from lisjong_arena.learned_policy_offline_q import (
    p1_shanten_guard,
    p1_shanten_guard_diagnostic,
)
from lisjong_arena.learned_policy_offline_q.errors import OfflineQError
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    calculate_shanten,
    keep_shanten_tile_mask,
    reconstruct_concealed_tiles,
)
from lisjong_arena.learned_policy_offline_q.p1_candidate import LOCKED_P1_CANDIDATE
from lisjong_arena.learned_policy_offline_q.p1_features import (
    LOCKED_P1_SCHEMA_FINGERPRINT,
    P1_FEATURE_DIMENSION,
    P1_FEATURE_SEMANTICS_ID,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b import GATE_B_ORDERED_SEEDS
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard import (
    GUARD_CANDIDATE_IDENTITY_PREFIX,
    GUARD_RULE,
    GUARD_SEMANTICS_ID,
    GuardDecisionSample,
    GuardDiagnostics,
    ShantenGuardError,
    collect_guard_diagnostics,
    guard_binding_document,
    guard_candidate_identity,
    require_guard_candidate_identity,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic import (
    CLASSIFICATION_RULE,
    GAME_COUNT,
    GAME_MODE,
    MAX_WORKERS,
    ORDERED_SEEDS,
    ROTATIONS_PER_SEED,
    SEED_BLOCK_COUNT,
    SEED_PLAN_REFORMULATE,
    ShantenGuardDiagnosticError,
    ShantenGuardOutcome,
    bind_recorded_artifact,
    check_seed_freshness,
    classify_interval,
    derive_classification,
    load_diagnostic_result,
    plan_block,
    record_classification,
    require_diagnostic_seed,
    require_fresh_seed_plan,
    result_identity,
    save_classified_result,
    save_diagnostic_result,
    validate_diagnostic_result,
)


class GuardCandidateIdentityTest(unittest.TestCase):
    def test_the_identity_is_a_prefixed_binding_digest(self):
        identity = guard_candidate_identity(fixture_binding())
        self.assertTrue(identity.startswith(GUARD_CANDIDATE_IDENTITY_PREFIX))
        digest = identity[len(GUARD_CANDIDATE_IDENTITY_PREFIX) :]
        self.assertEqual(len(digest), 64)
        self.assertEqual(set(digest) - set("0123456789abcdef"), set())

    def test_a_different_base_weights_digest_changes_the_identity(self):
        self.assertNotEqual(
            guard_candidate_identity(fixture_binding()),
            guard_candidate_identity(fixture_binding(weights_digest="4" * 64)),
        )

    def test_a_different_base_support_digest_changes_the_identity(self):
        self.assertNotEqual(
            guard_candidate_identity(fixture_binding()),
            guard_candidate_identity(fixture_binding(support_digest="5" * 64)),
        )

    def test_a_different_guard_rule_changes_the_identity(self):
        binding = fixture_binding()
        default = guard_candidate_identity(binding)
        with mock.patch.object(
            p1_shanten_guard,
            "GUARD_RULE",
            {**GUARD_RULE, "empty_subset_behavior": "yakuhai-call-fallback"},
        ):
            changed = guard_candidate_identity(binding)
        self.assertNotEqual(default, changed)

    def test_the_guard_identity_differs_from_the_base_candidate_identity(self):
        binding = fixture_binding()
        base_identity = fixture_base_candidate_identity()
        self.assertNotEqual(guard_candidate_identity(binding), base_identity)

    def test_a_free_form_alias_is_not_a_guard_candidate_identity(self):
        with self.assertRaises(ShantenGuardError):
            require_guard_candidate_identity(
                "shanten-guard-candidate", fixture_binding()
            )

    def test_a_prefixed_but_undeducible_identity_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            require_guard_candidate_identity(
                f"{GUARD_CANDIDATE_IDENTITY_PREFIX}{'8' * 64}", fixture_binding()
            )

    def test_a_non_object_base_binding_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            guard_binding_document(["not", "an", "object"])

    def test_the_guard_rule_forbids_a_fallback_on_empty_subset(self):
        self.assertEqual(
            GUARD_RULE["empty_subset_behavior"],
            "original-legal-masked-argmax-q-no-fallback",
        )
        self.assertEqual(GUARD_RULE["q_value_modification"], "none")
        self.assertIs(GUARD_RULE["fallback_path_guarded"], False)
        self.assertIs(GUARD_RULE["non_discard_decisions_guarded"], False)

    def test_the_guard_semantics_id_is_locked(self):
        self.assertEqual(
            GUARD_SEMANTICS_ID, "arena-learned-policy-offlineq-p1-shanten-guard-v1"
        )


class GuardDecisionSampleTest(unittest.TestCase):
    def _sample(self, **overrides):
        fields = {
            "keep_shanten_available": True,
            "unguarded_worsens_shanten": True,
            "action_changed": True,
            "guarded_worsens_shanten": False,
            "baseline_post_discard_shanten": 1,
            "guarded_post_discard_shanten": 0,
        }
        fields.update(overrides)
        return GuardDecisionSample(**fields)

    def test_a_normal_sample_is_accepted(self):
        sample = self._sample()
        self.assertTrue(sample.keep_shanten_available)

    def test_a_non_bool_field_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._sample(keep_shanten_available=1)

    def test_a_negative_shanten_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._sample(baseline_post_discard_shanten=-1)

    def test_a_guard_available_worsening_sample_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._sample(
                keep_shanten_available=True,
                guarded_worsens_shanten=True,
            )

    def test_guarded_shanten_worse_than_baseline_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._sample(
                baseline_post_discard_shanten=0, guarded_post_discard_shanten=1
            )

    def test_a_no_change_sample_is_internally_consistent(self):
        sample = self._sample(
            keep_shanten_available=False,
            action_changed=False,
            guarded_worsens_shanten=True,
            baseline_post_discard_shanten=1,
            guarded_post_discard_shanten=1,
        )
        self.assertFalse(sample.action_changed)


class GuardDiagnosticsTest(unittest.TestCase):
    def test_a_fixture_active_diagnostics_is_internally_consistent(self):
        diagnostics = fixture_guard_diagnostics()
        self.assertEqual(diagnostics.learned_decision_count, 100)
        self.assertEqual(diagnostics.action_change_rate, 1.0)
        self.assertEqual(diagnostics.keep_shanten_available_rate, 1.0)

    def test_a_fixture_inactive_diagnostics_has_zero_action_changes(self):
        diagnostics = fixture_guard_diagnostics(active=False)
        self.assertEqual(diagnostics.action_change_count, 0)

    def test_zero_decisions_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            GuardDiagnostics(
                learned_decision_count=0,
                keep_shanten_available_count=0,
                no_keep_shanten_available_count=0,
                unguarded_worsen_count=0,
                unguarded_keep_count=0,
                action_change_count=0,
                guarded_worsen_among_available_count=0,
                mean_baseline_post_discard_shanten=0.0,
                mean_guarded_post_discard_shanten=0.0,
                paired_lower_count=0,
                paired_equal_count=0,
                paired_higher_count=0,
            )

    def _replace(self, **overrides):
        base = dict(
            learned_decision_count=100,
            keep_shanten_available_count=100,
            no_keep_shanten_available_count=0,
            unguarded_worsen_count=100,
            unguarded_keep_count=0,
            action_change_count=100,
            guarded_worsen_among_available_count=0,
            mean_baseline_post_discard_shanten=1.0,
            mean_guarded_post_discard_shanten=0.0,
            paired_lower_count=100,
            paired_equal_count=0,
            paired_higher_count=0,
        )
        base.update(overrides)
        return GuardDiagnostics(**base)

    def test_availability_counts_must_partition(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(keep_shanten_available_count=50)

    def test_worsen_keep_counts_must_partition(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(unguarded_worsen_count=50)

    def test_action_change_cannot_exceed_keep_available(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(keep_shanten_available_count=10, action_change_count=100)

    def test_a_nonzero_guarded_worsen_among_available_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(guarded_worsen_among_available_count=1)

    def test_paired_counts_must_partition(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(paired_lower_count=50)

    def test_a_nonzero_paired_higher_count_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(paired_equal_count=99, paired_higher_count=1)

    def test_mean_guarded_exceeding_mean_baseline_is_rejected(self):
        with self.assertRaises(ShantenGuardError):
            self._replace(
                mean_baseline_post_discard_shanten=0.0,
                mean_guarded_post_discard_shanten=0.5,
            )

    def test_collect_guard_diagnostics_requires_at_least_one_sample(self):
        with self.assertRaises(ShantenGuardError):
            collect_guard_diagnostics([])

    def test_collect_guard_diagnostics_aggregates_across_instances(self):
        class _FakePolicy:
            def __init__(self, samples):
                self.guard_samples = samples

        sample_keep = GuardDecisionSample(
            keep_shanten_available=True,
            unguarded_worsens_shanten=True,
            action_changed=True,
            guarded_worsens_shanten=False,
            baseline_post_discard_shanten=1,
            guarded_post_discard_shanten=0,
        )
        sample_no_keep = GuardDecisionSample(
            keep_shanten_available=False,
            unguarded_worsens_shanten=True,
            action_changed=False,
            guarded_worsens_shanten=True,
            baseline_post_discard_shanten=1,
            guarded_post_discard_shanten=1,
        )
        diagnostics = collect_guard_diagnostics(
            [_FakePolicy((sample_keep,)), _FakePolicy((sample_no_keep,))]
        )
        self.assertEqual(diagnostics.learned_decision_count, 2)
        self.assertEqual(diagnostics.keep_shanten_available_count, 1)
        self.assertEqual(diagnostics.action_change_count, 1)
        self.assertEqual(diagnostics.paired_lower_count, 1)
        self.assertEqual(diagnostics.paired_equal_count, 1)
        self.assertEqual(diagnostics.paired_higher_count, 0)


class DiagnosticSeedPlanTest(unittest.TestCase):
    def test_the_locked_population_is_exactly_522_to_546(self):
        self.assertEqual(ORDERED_SEEDS, tuple(range(522, 547)))
        self.assertEqual(len(ORDERED_SEEDS), 25)
        self.assertEqual(SEED_BLOCK_COUNT, 25)

    def test_the_locked_shape_is_four_rotations_and_one_hundred_games(self):
        self.assertEqual(ROTATIONS_PER_SEED, 4)
        self.assertEqual(GAME_COUNT, 100)
        self.assertEqual(GAME_COUNT, SEED_BLOCK_COUNT * ROTATIONS_PER_SEED)

    def test_the_game_mode_and_worker_count_are_locked(self):
        self.assertEqual(GAME_MODE, "4p-red-single")
        self.assertEqual(MAX_WORKERS, 1)

    def test_the_population_does_not_overlap_a_known_allocation(self):
        report = check_seed_freshness()
        self.assertTrue(report["fresh"])
        self.assertEqual(report["collisions"], [])
        self.assertIn(521, p1_shanten_guard_diagnostic.declared_allocated_seeds())
        self.assertIn(464, p1_shanten_guard_diagnostic.declared_allocated_seeds())

    def test_a_collision_reports_seed_plan_reformulate_before_result_exposure(self):
        with mock.patch.object(
            p1_shanten_guard_diagnostic,
            "declared_allocated_seeds",
            return_value=frozenset(GATE_B_ORDERED_SEEDS) | frozenset({522}),
        ):
            report = check_seed_freshness()
            self.assertFalse(report["fresh"])
            self.assertEqual(report["status"], SEED_PLAN_REFORMULATE)
            self.assertEqual(report["collisions"], [522])
            with self.assertRaises(ShantenGuardDiagnosticError):
                require_fresh_seed_plan()

    def test_a_collision_after_result_exposure_is_stop_invalid(self):
        with mock.patch.object(
            p1_shanten_guard_diagnostic,
            "declared_allocated_seeds",
            return_value=frozenset({522}),
        ):
            report = check_seed_freshness(result_exposed=True)
            self.assertEqual(report["status"], ShantenGuardOutcome.STOP_INVALID.value)

    def test_seeds_outside_the_population_fail_closed(self):
        for seed in (521, 547, 440):
            with self.assertRaises(ShantenGuardDiagnosticError):
                require_diagnostic_seed(seed)
        self.assertEqual(require_diagnostic_seed(522), 522)

    def test_a_non_int_seed_is_rejected(self):
        with self.assertRaises(TypeError):
            require_diagnostic_seed("522")

    def test_the_plan_block_is_the_locked_one(self):
        block = plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(522, 547)))
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], 1)
        self.assertIs(block["formal_test"], False)
        self.assertEqual(block["candidate_arm"], "G")
        self.assertEqual(block["baseline_arm"], "U")


class DiagnosticClassificationTest(unittest.TestCase):
    def test_a_strictly_positive_lower_bound_is_a_rollout_signal(self):
        self.assertIs(classify_interval(0.1, 5.0), ShantenGuardOutcome.ROLLOUT_SIGNAL)

    def test_a_strictly_negative_upper_bound_is_a_rollout_negative(self):
        self.assertIs(
            classify_interval(-5.0, -0.1), ShantenGuardOutcome.ROLLOUT_NEGATIVE
        )

    def test_an_interval_that_touches_zero_is_inconclusive(self):
        self.assertIs(
            classify_interval(0.0, 5.0), ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE
        )
        self.assertIs(
            classify_interval(-5.0, 0.0), ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE
        )

    def test_the_outcome_ladder_is_exhaustive_and_locked(self):
        self.assertEqual(
            [outcome.value for outcome in ShantenGuardOutcome],
            [
                "SHANTEN GUARD ROLLOUT SIGNAL",
                "SHANTEN GUARD ROLLOUT NEGATIVE",
                "SHANTEN GUARD ROLLOUT INCONCLUSIVE",
                "SHANTEN GUARD INACTIVE",
                "SHANTEN GUARD EVIDENCE BLOCKED",
                "STOP / INVALID",
            ],
        )

    def test_the_classification_rule_excludes_diagnostics(self):
        self.assertIs(
            CLASSIFICATION_RULE["secondary_metrics_may_alter_classification"], False
        )
        self.assertIs(
            CLASSIFICATION_RULE["serving_diagnostics_may_alter_classification"], False
        )

    def test_inactive_overrides_a_positive_interval(self):
        """action_change_count == 0なら、intervalがpositiveでもINACTIVE。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            document = diagnostic_result_document(
                tmp / "inactive.json",
                scaled_delta_for_seed=positive_delta,
                guard_diagnostics=fixture_guard_diagnostics(active=False),
            )
            statistics = document["canonical_summary"]["seed_block_statistics"]
            self.assertGreater(statistics["normal_approx_95_interval_lower"], 0.0)
            self.assertIs(derive_classification(document), ShantenGuardOutcome.INACTIVE)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DiagnosticResultDocumentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.document = diagnostic_result_document(self._tmp / "artifact.json")

    def _document(self, **kwargs):
        path = self._tmp / f"artifact-{len(list(self._tmp.iterdir()))}.json"
        return diagnostic_result_document(path, **kwargs)

    def test_a_freshly_built_result_validates(self):
        self.assertEqual(validate_diagnostic_result(self.document), self.document)
        self.assertIsNone(self.document["classification"])

    def test_each_classification_branch_is_reachable_from_real_metrics(self):
        for delta, expected in (
            (positive_delta, ShantenGuardOutcome.ROLLOUT_SIGNAL),
            (negative_delta, ShantenGuardOutcome.ROLLOUT_NEGATIVE),
            (inconclusive_delta, ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE),
        ):
            document = self._document(scaled_delta_for_seed=delta)
            self.assertIs(derive_classification(document), expected)

    def test_secondary_and_serving_diagnostics_cannot_alter_the_classification(self):
        baseline = derive_classification(self.document)
        document = self._document(
            guarded_activation_diagnostics=fixture_activation_diagnostics(
                total_decisions=10,
                total_activations=0,
                total_scaffold_fallbacks=10,
                total_support_fallbacks=0,
            )
        )
        self.assertIs(derive_classification(document), baseline)

    def test_the_result_identity_is_derived_from_the_document(self):
        self.assertEqual(
            self.document["result_identity"], result_identity(self.document)
        )

    def test_an_edited_measurement_breaks_the_result_identity(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["seed_block_statistics"]["normal_approx_95_interval_lower"] = 1.0
        tampered = {**self.document, "canonical_summary": summary}
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(tampered)

    def test_a_missing_or_extra_field_is_rejected(self):
        without = {
            name: value
            for name, value in self.document.items()
            if name != "guard_diagnostics"
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(without)
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "extra": 1})

    def test_a_non_object_result_is_rejected(self):
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result([self.document])

    def test_a_changed_seed_plan_is_rejected(self):
        plan = {**self.document["plan"], "ordered_seeds": list(range(522, 552))}
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "plan": plan})

    def test_changed_limitations_or_interpretation_boundary_are_rejected(self):
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "limitations": []})
        boundary = {
            **self.document["interpretation_boundary"],
            "forbidden_claims": [],
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(
                {**self.document, "interpretation_boundary": boundary}
            )

    def test_a_tampered_guarded_candidate_binding_is_rejected(self):
        candidate = json.loads(json.dumps(self.document["guarded_candidate"]))
        candidate["binding"]["base_candidate_binding"]["support_set_digest"] = "9" * 64
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(
                {**self.document, "guarded_candidate": candidate}
            )

    def test_the_guarded_and_unguarded_arms_must_share_the_base_identity(self):
        candidate = {
            **self.document["unguarded_candidate"],
            "canonical_model_weights_digest": "a" * 64,
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(
                {**self.document, "unguarded_candidate": candidate}
            )

    def test_a_free_form_guarded_identity_is_rejected(self):
        candidate = {**self.document["guarded_candidate"], "identity": "guarded"}
        with self.assertRaises(OfflineQError):
            validate_diagnostic_result(
                {**self.document, "guarded_candidate": candidate}
            )

    def test_a_partial_game_count_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["candidate_metrics"]["game_count"] = 96
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "canonical_summary": summary})

    def test_an_undefined_interval_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["seed_block_statistics"]["normal_approx_95_interval_lower"] = None
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "canonical_summary": summary})

    def test_guard_diagnostics_that_do_not_partition_are_rejected(self):
        guard = {
            **self.document["guard_diagnostics"],
            "keep_shanten_available_count": 1,
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "guard_diagnostics": guard})

    def test_a_nonzero_guarded_worsen_among_available_in_the_document_is_rejected(self):
        guard = {
            **self.document["guard_diagnostics"],
            "guarded_worsen_among_available_count": 1,
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "guard_diagnostics": guard})

    def test_a_nonzero_paired_higher_count_in_the_document_is_rejected(self):
        guard = {
            **self.document["guard_diagnostics"],
            "paired_equal_count": self.document["guard_diagnostics"][
                "paired_equal_count"
            ]
            - 1,
            "paired_higher_count": 1,
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**self.document, "guard_diagnostics": guard})

    def test_diagnostics_that_do_not_partition_the_decisions_are_rejected(self):
        diagnostics = {
            **self.document["serving_diagnostics"],
            "guarded": {
                **self.document["serving_diagnostics"]["guarded"],
                "total_activations": 999,
            },
        }
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(
                {**self.document, "serving_diagnostics": diagnostics}
            )

    def test_a_nonzero_illegal_selection_count_is_rejected(self):
        for name in (
            "illegal_selection_count",
            "non_finite_model_output_count",
            "resolve_failure_count",
        ):
            diagnostics = {
                **self.document["serving_diagnostics"],
                "guarded": {**self.document["serving_diagnostics"]["guarded"], name: 1},
            }
            with self.assertRaises(ShantenGuardDiagnosticError):
                validate_diagnostic_result(
                    {**self.document, "serving_diagnostics": diagnostics}
                )

    def test_a_strength_artifact_block_change_is_rejected(self):
        for name, value in (
            ("schema_version", 2),
            ("evaluation_protocol", "aabb-comparison-v1"),
            ("game_count", 96),
            ("sha256", "not-a-digest"),
            ("filename", "nested/path.json"),
            ("retention", {"backend": "x", "key": "y"}),
        ):
            artifact = {**self.document["strength_artifact"], name: value}
            with self.assertRaises(ShantenGuardDiagnosticError):
                validate_diagnostic_result(
                    {**self.document, "strength_artifact": artifact}
                )


class DiagnosticClassificationRecordingTest(unittest.TestCase):
    """classificationはactual strict-loaded checkpointへbindされる。

    このclassにreal candidate classificationのpositive testは無い。real
    Issue #173 evidenceを作るにはexact #162 weights bytesを持つserving
    checkpoint bundleが要り、それはfixtureからは作れないためである。
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _artifact_path(self, name):
        return self._tmp / f"{name}.json"

    def _document(self, name, **kwargs):
        return diagnostic_result_document(self._artifact_path(name), **kwargs)

    def _locked_document(self, name):
        return self._document(name, checkpoint=locked_checkpoint())

    def test_locked_identity_strings_alone_cannot_record_an_outcome(self):
        document = self._locked_document("identity-only")
        self.assertIs(
            document["guarded_candidate"]["real_candidate_materialization"], True
        )
        self.assertIs(
            derive_classification(document), ShantenGuardOutcome.ROLLOUT_SIGNAL
        )
        with self.assertRaises(OfflineQError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                checkpoint=locked_checkpoint(),
                artifact_path=self._artifact_path("identity-only"),
            )

    def test_a_checkpoint_is_required(self):
        document = self._locked_document("required")
        with self.assertRaises(TypeError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                artifact_path=self._artifact_path("required"),
            )

    def test_an_artifact_path_is_required(self):
        document = self._locked_document("no-artifact-path")
        with self.assertRaises(TypeError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                checkpoint=locked_checkpoint(),
            )

    def test_a_non_checkpoint_is_rejected(self):
        document = self._locked_document("type-checkpoint")
        with self.assertRaises(TypeError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                checkpoint=object(),
                artifact_path=self._artifact_path("type-checkpoint"),
            )

    def test_an_outcome_the_rule_does_not_derive_is_rejected(self):
        document = self._locked_document("mismatch")
        with self.assertRaises(ShantenGuardDiagnosticError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_NEGATIVE,
                checkpoint=locked_checkpoint(),
                artifact_path=self._artifact_path("mismatch"),
            )

    def test_a_pre_result_state_is_never_recorded(self):
        document = self._locked_document("blocked")
        for outcome in (
            ShantenGuardOutcome.EVIDENCE_BLOCKED,
            ShantenGuardOutcome.STOP_INVALID,
        ):
            with self.assertRaises(ShantenGuardDiagnosticError):
                record_classification(
                    document,
                    outcome,
                    checkpoint=locked_checkpoint(),
                    artifact_path=self._artifact_path("blocked"),
                )

    def test_a_fixture_candidate_is_never_real_evidence(self):
        document = self._document("fixture")
        self.assertIs(
            document["guarded_candidate"]["real_candidate_materialization"], False
        )
        with self.assertRaises(ShantenGuardDiagnosticError):
            record_classification(
                document,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                checkpoint=fixture_checkpoint(),
                artifact_path=self._artifact_path("fixture"),
            )

    def test_an_outcome_cannot_be_recorded_twice(self):
        document = self._locked_document("twice")
        classified = {
            **document,
            "classification": derive_classification(document).value,
        }
        self.assertEqual(validate_diagnostic_result(classified), classified)
        with self.assertRaises(ShantenGuardDiagnosticError):
            record_classification(
                classified,
                ShantenGuardOutcome.ROLLOUT_SIGNAL,
                checkpoint=locked_checkpoint(),
                artifact_path=self._artifact_path("twice"),
            )

    def test_a_non_outcome_value_is_rejected(self):
        document = self._locked_document("type")
        with self.assertRaises(TypeError):
            record_classification(
                document,
                "SHANTEN GUARD ROLLOUT SIGNAL",
                checkpoint=locked_checkpoint(),
                artifact_path=self._artifact_path("type"),
            )

    def test_a_recorded_outcome_that_contradicts_the_interval_is_rejected(self):
        document = self._locked_document("contradiction")
        tampered = {**document, "classification": "SHANTEN GUARD ROLLOUT NEGATIVE"}
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result(tampered)

    def test_an_unknown_outcome_string_is_rejected(self):
        document = self._locked_document("unknown")
        with self.assertRaises(ShantenGuardDiagnosticError):
            validate_diagnostic_result({**document, "classification": "GREAT"})

    def test_inactive_is_derivable_but_still_requires_a_real_disk_checkpoint(self):
        """INACTIVEもSIGNAL同様、identity文字列だけでは記録できない。

        `locked_checkpoint()`はweight bytesを持たないsnapshotなので、real
        Issue #173 evidenceを作るにはexact #162 weights bytesを持つdisk
        bundleが要る（`bind_recorded_candidate()`参照）。
        """
        document = self._document(
            "inactive",
            checkpoint=locked_checkpoint(),
            guard_diagnostics=fixture_guard_diagnostics(active=False),
        )
        self.assertIs(derive_classification(document), ShantenGuardOutcome.INACTIVE)
        with self.assertRaises(OfflineQError):
            record_classification(
                document,
                ShantenGuardOutcome.INACTIVE,
                checkpoint=locked_checkpoint(),
                artifact_path=self._artifact_path("inactive"),
            )

    def test_save_classified_result_never_persists_for_a_fixture_candidate(self):
        """recordできないclassificationは、classified fileも一切作らない。"""
        document = self._document("fixture-save")
        outcome = derive_classification(document)
        classified_path = self._tmp / "fixture-save-classified.json"
        with self.assertRaises(ShantenGuardDiagnosticError):
            save_classified_result(
                classified_path,
                document,
                outcome,
                checkpoint=fixture_checkpoint(),
                artifact_path=self._artifact_path("fixture-save"),
            )
        self.assertFalse(classified_path.exists())


class SeedFreshnessEnforcementTest(unittest.TestCase):
    """seed freshnessはcaller disciplineではなくexecution boundaryでfail closedにする。"""

    def test_a_collision_stops_the_run_before_any_game_or_checkpoint_access(self):
        with (
            mock.patch.object(
                p1_shanten_guard_diagnostic,
                "declared_allocated_seeds",
                return_value=frozenset({522}),
            ),
            mock.patch(
                "lisjong_arena.single_round_evaluation._run_single_game"
            ) as fake_run_single_game,
        ):
            with self.assertRaises(ShantenGuardDiagnosticError) as ctx:
                p1_shanten_guard_diagnostic.run_shanten_guard_diagnostic(
                    None, "unused-artifact.json", "unused-result.json"
                )
            self.assertIn(SEED_PLAN_REFORMULATE, str(ctx.exception))
        fake_run_single_game.assert_not_called()


class DiagnosticResultPersistenceTest(unittest.TestCase):
    """diagnostic result document自体もwrite-once / strict readbackで永続化する。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.artifact_path = self._tmp / "artifact.json"
        self.document = diagnostic_result_document(self.artifact_path)

    def test_save_and_load_round_trip(self):
        result_path = self._tmp / "result.json"
        saved = save_diagnostic_result(result_path, self.document)
        self.assertEqual(saved, self.document)
        self.assertEqual(load_diagnostic_result(result_path), self.document)

    def test_an_existing_result_path_is_never_overwritten(self):
        result_path = self._tmp / "result.json"
        save_diagnostic_result(result_path, self.document)
        with self.assertRaises(ShantenGuardDiagnosticError):
            save_diagnostic_result(result_path, self.document)

    def test_a_missing_result_file_is_rejected(self):
        with self.assertRaises(ShantenGuardDiagnosticError):
            load_diagnostic_result(self._tmp / "absent.json")

    def test_a_tampered_result_file_is_rejected_on_readback(self):
        from lisjong_arena._artifact_io import canonical_json_text

        result_path = self._tmp / "result.json"
        save_diagnostic_result(result_path, self.document)
        tampered = json.loads(result_path.read_text(encoding="utf-8"))
        tampered["limitations"] = []
        result_path.write_text(
            canonical_json_text(tampered), encoding="utf-8", newline="\n"
        )
        with self.assertRaises(ShantenGuardDiagnosticError):
            load_diagnostic_result(result_path)

    def test_a_non_canonical_result_file_is_rejected(self):
        result_path = self._tmp / "result.json"
        save_diagnostic_result(result_path, self.document)
        text = result_path.read_text(encoding="utf-8")
        result_path.write_text(f" {text}", encoding="utf-8")
        with self.assertRaises(ShantenGuardDiagnosticError):
            load_diagnostic_result(result_path)

    def test_a_non_json_result_file_is_rejected(self):
        result_path = self._tmp / "result.json"
        result_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ShantenGuardDiagnosticError):
            load_diagnostic_result(result_path)

    def test_bind_recorded_artifact_accepts_the_matching_artifact(self):
        validated = validate_diagnostic_result(self.document)
        artifact = bind_recorded_artifact(validated, self.artifact_path)
        self.assertEqual(len(artifact.game_results), 100)

    def test_bind_recorded_artifact_rejects_a_sha_mismatch(self):
        validated = validate_diagnostic_result(self.document)
        self.artifact_path.write_bytes(self.artifact_path.read_bytes() + b"\ntampered")
        with self.assertRaises(ShantenGuardDiagnosticError):
            bind_recorded_artifact(validated, self.artifact_path)

    def test_bind_recorded_artifact_rejects_a_missing_artifact(self):
        validated = validate_diagnostic_result(self.document)
        self.artifact_path.unlink()
        with self.assertRaises(ShantenGuardDiagnosticError):
            bind_recorded_artifact(validated, self.artifact_path)

    def test_bind_recorded_artifact_rejects_a_renamed_artifact_file(self):
        validated = validate_diagnostic_result(self.document)
        renamed = self._tmp / "renamed.json"
        self.artifact_path.rename(renamed)
        with self.assertRaises(ShantenGuardDiagnosticError):
            bind_recorded_artifact(validated, renamed)


class LockedCandidateReuseTest(unittest.TestCase):
    """Issue #173はexact #162 candidateをbaseとし、identityをsilent substitutionしない。"""

    def test_the_locked_candidate_is_the_issue_162_gate_b_candidate(self):
        self.assertEqual(
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest,
            "f8108bf1e671007f22a8b36295ff545b3198479f74d83bb48da8a45df194461f",
        )
        self.assertEqual(
            LOCKED_P1_CANDIDATE.source_dataset_identity,
            "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4",
        )
        self.assertEqual(
            LOCKED_P1_CANDIDATE.support_set_digest,
            "230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e",
        )

    def test_the_fixture_candidate_is_not_the_locked_candidate(self):
        self.assertNotEqual(FIXTURE_WEIGHTS_DIGEST, LOCKED_P1_CANDIDATE)
        self.assertNotEqual(
            FIXTURE_DATASET_IDENTITY, LOCKED_P1_CANDIDATE.source_dataset_identity
        )
        self.assertNotEqual(
            FIXTURE_SUPPORT_DIGEST, LOCKED_P1_CANDIDATE.support_set_digest
        )


class UpstreamContractUnchangedTest(unittest.TestCase):
    """Issue #173はArenaでshantenを再実装せず、#152 / #158の既存contractを
    そのまま再利用するだけである。ここではそのlocked upstream contractが
    guard追加後も変わっていないことを固定する。
    """

    def test_the_p1_feature_identity_is_unchanged(self):
        self.assertEqual(
            P1_FEATURE_SEMANTICS_ID,
            "arena-learned-policy-offlineq-p1-keep-shanten-feature-v1",
        )
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)
        self.assertEqual(
            LOCKED_P1_SCHEMA_FINGERPRINT,
            "beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409",
        )

    def test_keep_shanten_tile_mask_semantics_are_unchanged_for_a_locked_hand(self):
        """#158のkeep-shanten mask定義をArena側で再実装/再定義していない。"""
        values = tensor_values(build_policy_input_feature(policy_input()))
        concealed = reconstruct_concealed_tiles(values)
        self.assertEqual(calculate_shanten(concealed), 0)

        mask = dict(zip(TILE_AXIS, keep_shanten_tile_mask(values), strict=True))
        for tile in KEEP_TILES:
            self.assertTrue(mask[tile], f"{tile} was expected to keep shanten")
        for tile in WORSEN_TILES:
            self.assertFalse(mask[tile], f"{tile} was expected to worsen shanten")


if __name__ == "__main__":
    unittest.main()
