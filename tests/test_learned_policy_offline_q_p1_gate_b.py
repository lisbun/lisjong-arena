"""Issue #162 P1 Gate B torch-free tests.

comparator semantics、candidate logical identity、seed plan、result
validation、classification ladderをtorchなしで固定する。実RiichiEnvも
real #158 candidateも使わない。
"""

import json
import shutil
import tempfile
import unittest
from itertools import permutations
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_p1_gate_b_fixtures import (
    FIXTURE_DATASET_IDENTITY,
    FIXTURE_SUPPORT_DIGEST,
    FIXTURE_WEIGHTS_DIGEST,
    ankan_action,
    chi_action,
    daiminkan_action,
    decision,
    fixture_binding,
    fixture_candidate_identity,
    fixture_checkpoint,
    fixture_diagnostics,
    gate_b_game_results,
    gate_b_result_document,
    inconclusive_delta,
    kyuushu_action,
    negative_delta,
    pass_action,
    pon_action,
    positive_delta,
    riichi_action,
    ron_action,
    save_gate_b_artifact,
    tedashi_discards,
    tsumo_action,
    tsumogiri_discard,
)
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q import p1_candidate, p1_gate_b
from lisjong_arena.learned_policy_offline_q.diagnosis import LOCKED_SOURCE_IDENTITIES
from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    CANDIDATE_IDENTITY_PREFIX,
    LOCKED_P1_CANDIDATE,
    LOCKED_SELECTED_EPOCH,
    P1CandidateError,
    candidate_logical_identity,
    require_candidate_identity,
)
from lisjong_arena.learned_policy_offline_q.p1_features import (
    LOCKED_P1_SCHEMA_FINGERPRINT,
    P1_FEATURE_DIMENSION,
    P1_FEATURE_SEMANTICS_ID,
    P1_TENSOR_SCHEMA_VERSION,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b import (
    GATE_B_GAME_COUNT,
    GATE_B_GAME_MODE,
    GATE_B_MAX_WORKERS,
    GATE_B_ORDERED_SEEDS,
    GATE_B_ROTATIONS_PER_SEED,
    GATE_B_SEED_BLOCK_COUNT,
    P1GateBError,
    P1GateBOutcome,
    classify_interval,
    derive_classification,
    record_classification,
    require_gate_b_artifact,
    require_gate_b_seed,
    result_identity,
    validate_gate_b_result,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    PassiveTsumogiriError,
    PassiveTsumogiriPolicy,
    create_passive_tsumogiri,
    passive_tsumogiri_spec,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    LOCKED_VOCABULARY_FINGERPRINT,
    LOCKED_VOCABULARY_VERSION,
    MAXIMUM_EPOCHS,
    VOCABULARY_SIZE,
)
from lisjong_arena.learned_policy_offline_q.serving import (
    HybridRuntime,
    HybridServingError,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.single_round_artifact import (
    SingleRoundArtifactError,
    load_single_round_artifact,
)


class PassiveTsumogiriComparatorTest(unittest.TestCase):
    def setUp(self):
        self.policy = create_passive_tsumogiri()

    def test_ron_is_selected_when_it_is_legal(self):
        ron = ron_action()
        context = decision((ron, pass_action(), chi_action()))
        self.assertIs(self.policy.choose_action(context), ron)

    def test_tsumo_is_selected_when_it_is_legal(self):
        tsumo = tsumo_action()
        context = decision((tsumogiri_discard(), tsumo, riichi_action()))
        self.assertIs(self.policy.choose_action(context), tsumo)

    def test_pass_is_selected_over_every_call(self):
        pass_ = pass_action()
        context = decision((chi_action(), pon_action(), daiminkan_action(), pass_))
        self.assertIs(self.policy.choose_action(context), pass_)

    def test_own_turn_selects_the_exact_drawn_tile_tsumogiri_discard(self):
        tsumogiri = tsumogiri_discard()
        context = decision((*tedashi_discards(), tsumogiri))
        self.assertIs(self.policy.choose_action(context), tsumogiri)

    def test_riichi_is_never_selected_over_the_tsumogiri_discard(self):
        tsumogiri = tsumogiri_discard()
        context = decision((riichi_action(), *tedashi_discards(), tsumogiri))
        self.assertIs(self.policy.choose_action(context), tsumogiri)

    def test_kan_is_never_selected_over_the_tsumogiri_discard(self):
        tsumogiri = tsumogiri_discard()
        context = decision((ankan_action(), *tedashi_discards(), tsumogiri))
        self.assertIs(self.policy.choose_action(context), tsumogiri)

    def test_kyuushu_kyuuhai_is_never_selected(self):
        tsumogiri = tsumogiri_discard()
        context = decision((kyuushu_action(), tsumogiri))
        self.assertIs(self.policy.choose_action(context), tsumogiri)

    def test_no_unique_tsumogiri_discard_fails_closed(self):
        context = decision(tedashi_discards())
        with self.assertRaises(PassiveTsumogiriError):
            self.policy.choose_action(context)

    def test_a_tsumogiri_of_another_tile_is_not_accepted(self):
        from lisjong.policy_contract.action import DiscardAction
        from lisjong.policy_contract.tile import Tile, TileCategory, TileType

        other = DiscardAction(
            actor=Seat.SEAT_0,
            tile=Tile(TileType(TileCategory.MANZU, 4)),
            tsumogiri=True,
        )
        context = decision((*tedashi_discards(ranks=(1, 2)), other))
        with self.assertRaises(PassiveTsumogiriError):
            self.policy.choose_action(context)

    def test_legal_action_input_order_does_not_change_the_decision(self):
        actions = (
            riichi_action(),
            *tedashi_discards(ranks=(1, 2)),
            tsumogiri_discard(),
        )
        chosen = {
            self.policy.choose_action(decision(order))
            for order in permutations(actions)
        }
        self.assertEqual(chosen, {tsumogiri_discard()})

    def test_multiple_winning_actions_resolve_deterministically(self):
        first = ron_action(target=Seat.SEAT_1, rank=1)
        second = ron_action(target=Seat.SEAT_2, rank=2)
        forward = self.policy.choose_action(decision((first, second)))
        backward = self.policy.choose_action(decision((second, first)))
        self.assertEqual(forward, backward)
        self.assertIn(forward, (first, second))

    def test_the_comparator_holds_no_mutable_state(self):
        self.assertEqual(PassiveTsumogiriPolicy.__slots__, ())
        self.assertFalse(hasattr(create_passive_tsumogiri(), "__dict__"))
        context = decision((*tedashi_discards(), tsumogiri_discard()))
        first = self.policy.choose_action(context)
        second = self.policy.choose_action(context)
        third = create_passive_tsumogiri().choose_action(context)
        self.assertIs(first, second)
        self.assertIs(first, third)

    def test_each_factory_call_returns_a_fresh_instance(self):
        self.assertIsNot(create_passive_tsumogiri(), create_passive_tsumogiri())

    def test_a_non_decision_context_is_rejected(self):
        with self.assertRaises(TypeError):
            self.policy.choose_action(object())

    def test_the_spec_identity_is_the_locked_working_identity(self):
        spec = passive_tsumogiri_spec()
        self.assertEqual(spec.identity, PASSIVE_TSUMOGIRI_IDENTITY)
        self.assertIs(spec.factory, create_passive_tsumogiri)

    def test_the_comparator_is_not_registered_in_the_curated_catalog(self):
        self.assertNotIn(PASSIVE_TSUMOGIRI_IDENTITY, POLICY_CATALOG)
        self.assertNotIn(
            create_passive_tsumogiri,
            [spec.factory for spec in POLICY_CATALOG.values()],
        )


class CandidateLogicalIdentityTest(unittest.TestCase):
    def test_the_identity_is_a_prefixed_binding_digest(self):
        identity = fixture_candidate_identity()
        self.assertTrue(identity.startswith(CANDIDATE_IDENTITY_PREFIX))
        digest = identity[len(CANDIDATE_IDENTITY_PREFIX) :]
        self.assertEqual(len(digest), 64)
        self.assertEqual(set(digest) - set("0123456789abcdef"), set())

    def test_a_different_weights_digest_changes_the_identity(self):
        self.assertNotEqual(
            fixture_candidate_identity(),
            fixture_candidate_identity(weights_digest="4" * 64),
        )

    def test_a_different_support_digest_changes_the_identity(self):
        self.assertNotEqual(
            fixture_candidate_identity(),
            fixture_candidate_identity(support_digest="5" * 64),
        )

    def _identity_with(self, name, block):
        with mock.patch.object(p1_candidate, name, return_value=block):
            return fixture_candidate_identity()

    def test_a_different_p1_feature_fingerprint_changes_the_identity(self):
        block = {**p1_candidate.p1_feature_block(), "schema_fingerprint": "6" * 64}
        self.assertNotEqual(
            fixture_candidate_identity(),
            self._identity_with("p1_feature_block", block),
        )

    def test_a_different_action_vocabulary_changes_the_identity(self):
        block = {**p1_candidate.vocabulary_block(), "fingerprint": "7" * 64}
        self.assertNotEqual(
            fixture_candidate_identity(),
            self._identity_with("vocabulary_block", block),
        )

    def test_different_hybrid_activation_semantics_change_the_identity(self):
        block = {
            **p1_candidate.hybrid_activation_block(),
            "selection": "unmasked-argmax",
        }
        self.assertNotEqual(
            fixture_candidate_identity(),
            self._identity_with("hybrid_activation_block", block),
        )

    def test_a_different_fallback_policy_changes_the_identity(self):
        block = {**p1_candidate.fallback_policy_block(), "identity": "two-step"}
        self.assertNotEqual(
            fixture_candidate_identity(),
            self._identity_with("fallback_policy_block", block),
        )

    def test_a_free_form_alias_is_not_a_candidate_identity(self):
        with self.assertRaises(P1CandidateError):
            require_candidate_identity("p1-gate-b-candidate", fixture_binding())

    def test_a_prefixed_but_undeducible_identity_is_rejected(self):
        with self.assertRaises(P1CandidateError):
            require_candidate_identity(
                f"{CANDIDATE_IDENTITY_PREFIX}{'8' * 64}", fixture_binding()
            )

    def test_the_weights_digest_alone_is_not_the_identity(self):
        self.assertNotEqual(fixture_candidate_identity(), FIXTURE_WEIGHTS_DIGEST)
        self.assertNotIn(FIXTURE_WEIGHTS_DIGEST, fixture_candidate_identity())

    def test_a_malformed_binding_input_fails_closed(self):
        with self.assertRaises(P1CandidateError):
            p1_candidate.candidate_binding_document(
                canonical_model_weights_digest="short",
                support_set_digest=FIXTURE_SUPPORT_DIGEST,
            )
        with self.assertRaises(P1CandidateError):
            p1_candidate.candidate_binding_document(
                canonical_model_weights_digest=FIXTURE_WEIGHTS_DIGEST,
                support_set_digest="short",
            )

    def test_a_non_object_binding_is_rejected(self):
        with self.assertRaises(P1CandidateError):
            candidate_logical_identity(["not", "an", "object"])


class LockedCandidateContractTest(unittest.TestCase):
    def test_the_locked_candidate_is_the_issue_158_gate_a_candidate(self):
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

    def test_the_locked_candidate_reuses_the_retained_source_identities(self):
        self.assertEqual(
            LOCKED_P1_CANDIDATE.source_dataset_identity,
            LOCKED_SOURCE_IDENTITIES.dataset_identity,
        )
        self.assertEqual(
            LOCKED_P1_CANDIDATE.support_set_digest,
            LOCKED_SOURCE_IDENTITIES.supported_indices_digest,
        )
        p1_candidate.verify_locked_candidate_contract()

    def test_the_selected_epoch_is_exactly_twenty(self):
        self.assertEqual(LOCKED_SELECTED_EPOCH, 20)
        self.assertEqual(LOCKED_SELECTED_EPOCH, MAXIMUM_EPOCHS)

    def test_the_fixture_candidate_is_not_the_locked_candidate(self):
        self.assertNotEqual(FIXTURE_WEIGHTS_DIGEST, LOCKED_P1_CANDIDATE)
        self.assertNotEqual(
            FIXTURE_DATASET_IDENTITY, LOCKED_P1_CANDIDATE.source_dataset_identity
        )


class GateBSeedPlanTest(unittest.TestCase):
    def test_the_locked_population_is_exactly_440_to_464(self):
        self.assertEqual(GATE_B_ORDERED_SEEDS, tuple(range(440, 465)))
        self.assertEqual(len(GATE_B_ORDERED_SEEDS), 25)
        self.assertEqual(GATE_B_SEED_BLOCK_COUNT, 25)

    def test_the_locked_shape_is_four_rotations_and_one_hundred_games(self):
        self.assertEqual(GATE_B_ROTATIONS_PER_SEED, 4)
        self.assertEqual(GATE_B_GAME_COUNT, 100)
        self.assertEqual(
            GATE_B_GAME_COUNT, GATE_B_SEED_BLOCK_COUNT * GATE_B_ROTATIONS_PER_SEED
        )

    def test_the_game_mode_and_worker_count_are_locked(self):
        self.assertEqual(GATE_B_GAME_MODE, "4p-red-single")
        self.assertEqual(GATE_B_MAX_WORKERS, 1)

    def test_the_population_does_not_overlap_a_known_allocation(self):
        self.assertEqual(
            p1_gate_b._KNOWN_ALLOCATED_SEEDS.intersection(GATE_B_ORDERED_SEEDS),
            set(),
        )
        self.assertIn(439, p1_gate_b._KNOWN_ALLOCATED_SEEDS)

    def test_seeds_outside_the_population_fail_closed(self):
        for seed in (439, 465, 281):
            with self.assertRaises(P1GateBError):
                require_gate_b_seed(seed)
        self.assertEqual(require_gate_b_seed(440), 440)

    def test_a_non_int_seed_is_rejected(self):
        with self.assertRaises(TypeError):
            require_gate_b_seed("440")

    def test_the_plan_block_is_the_locked_one(self):
        block = p1_gate_b.plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(440, 465)))
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], 1)
        self.assertIs(block["formal_test"], False)


class GateBClassificationTest(unittest.TestCase):
    def test_a_strictly_positive_lower_bound_is_a_positive_signal(self):
        self.assertIs(classify_interval(0.1, 5.0), P1GateBOutcome.POSITIVE_SIGNAL)

    def test_a_strictly_negative_upper_bound_is_a_negative_signal(self):
        self.assertIs(classify_interval(-5.0, -0.1), P1GateBOutcome.NEGATIVE_SIGNAL)

    def test_an_interval_that_touches_zero_is_inconclusive(self):
        self.assertIs(classify_interval(0.0, 5.0), P1GateBOutcome.INCONCLUSIVE)
        self.assertIs(classify_interval(-5.0, 0.0), P1GateBOutcome.INCONCLUSIVE)
        self.assertIs(classify_interval(-1.0, 1.0), P1GateBOutcome.INCONCLUSIVE)

    def test_the_outcome_ladder_is_exhaustive_and_locked(self):
        self.assertEqual(
            [outcome.value for outcome in P1GateBOutcome],
            [
                "P1 GATE B POSITIVE SIGNAL",
                "P1 GATE B NEGATIVE SIGNAL",
                "P1 GATE B INCONCLUSIVE",
                "P1 GATE B EVIDENCE BLOCKED",
                "STOP / INVALID",
            ],
        )

    def test_the_classification_rule_excludes_diagnostics(self):
        rule = p1_gate_b.CLASSIFICATION_RULE
        self.assertIs(rule["secondary_metrics_may_alter_classification"], False)
        self.assertIs(rule["serving_diagnostics_may_alter_classification"], False)


class GateBResultDocumentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.document = gate_b_result_document(self._tmp / "artifact.json")

    def _document(self, **kwargs):
        path = self._tmp / f"artifact-{len(list(self._tmp.iterdir()))}.json"
        return gate_b_result_document(path, **kwargs)

    def test_a_freshly_built_result_validates(self):
        self.assertEqual(validate_gate_b_result(self.document), self.document)
        self.assertIsNone(self.document["classification"])

    def test_each_classification_branch_is_reachable_from_real_metrics(self):
        for delta, expected in (
            (positive_delta, P1GateBOutcome.POSITIVE_SIGNAL),
            (negative_delta, P1GateBOutcome.NEGATIVE_SIGNAL),
            (inconclusive_delta, P1GateBOutcome.INCONCLUSIVE),
        ):
            document = self._document(scaled_delta_for_seed=delta)
            self.assertIs(derive_classification(document), expected)

    def test_secondary_diagnostics_cannot_alter_the_classification(self):
        baseline = derive_classification(self.document)
        document = self._document(
            diagnostics=fixture_diagnostics(
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
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(tampered)

    def test_a_missing_or_extra_field_is_rejected(self):
        without = {
            name: value for name, value in self.document.items() if name != "comparator"
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(without)
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "extra": 1})

    def test_a_non_object_result_is_rejected(self):
        with self.assertRaises(P1GateBError):
            validate_gate_b_result([self.document])

    def test_a_substituted_comparator_is_rejected(self):
        comparator = {**self.document["comparator"], "identity": "yakuhai-call"}
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "comparator": comparator})

    def test_a_changed_seed_plan_is_rejected(self):
        plan = {**self.document["plan"], "ordered_seeds": list(range(440, 470))}
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "plan": plan})

    def test_changed_limitations_or_interpretation_boundary_are_rejected(self):
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "limitations": []})
        boundary = {
            **self.document["interpretation_boundary"],
            "forbidden_claims": [],
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(
                {**self.document, "interpretation_boundary": boundary}
            )

    def test_a_tampered_candidate_binding_is_rejected(self):
        candidate = json.loads(json.dumps(self.document["candidate"]))
        candidate["binding"]["support_set_digest"] = "9" * 64
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_a_free_form_candidate_identity_is_rejected(self):
        candidate = {**self.document["candidate"], "identity": "p1-candidate"}
        with self.assertRaises(P1CandidateError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_a_candidate_retention_target_change_is_rejected(self):
        candidate = {
            **self.document["candidate"],
            "retention": {"backend": "somewhere", "key": "elsewhere"},
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_a_candidate_epoch_other_than_twenty_is_rejected(self):
        candidate = {**self.document["candidate"], "selected_epoch": 19}
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_an_unknown_candidate_checkpoint_schema_is_rejected(self):
        candidate = {
            **self.document["candidate"],
            "checkpoint_schema_version": "arena-something-else-v1",
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_an_unknown_materialization_source_is_rejected(self):
        candidate = {
            **self.document["candidate"],
            "materialization_source": "hand-tuned",
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "candidate": candidate})

    def test_a_non_numeric_summary_value_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["mean_baseline_score"] = "0"
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "canonical_summary": summary})

    def test_a_partial_game_count_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["candidate_metrics"]["game_count"] = 96
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "canonical_summary": summary})

    def test_a_changed_seed_block_count_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["seed_block_statistics"]["seed_block_count"] = 24
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "canonical_summary": summary})

    def test_an_undefined_interval_is_rejected(self):
        summary = json.loads(json.dumps(self.document["canonical_summary"]))
        summary["seed_block_statistics"]["normal_approx_95_interval_lower"] = None
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**self.document, "canonical_summary": summary})

    def test_diagnostics_that_do_not_partition_the_decisions_are_rejected(self):
        diagnostics = {
            **self.document["serving_diagnostics"],
            "total_activations": 601,
        }
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(
                {**self.document, "serving_diagnostics": diagnostics}
            )

    def test_a_diagnostic_rate_that_is_not_derivable_is_rejected(self):
        diagnostics = {**self.document["serving_diagnostics"], "activation_rate": 1.0}
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(
                {**self.document, "serving_diagnostics": diagnostics}
            )

    def test_a_nonzero_illegal_selection_count_is_rejected(self):
        for name in (
            "illegal_selection_count",
            "non_finite_model_output_count",
            "resolve_failure_count",
        ):
            diagnostics = {**self.document["serving_diagnostics"], name: 1}
            with self.assertRaises(P1GateBError):
                validate_gate_b_result(
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
            with self.assertRaises(P1GateBError):
                validate_gate_b_result({**self.document, "strength_artifact": artifact})


class GateBClassificationRecordingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _document(self, name, **kwargs):
        return gate_b_result_document(self._tmp / f"{name}.json", **kwargs)

    def test_a_real_candidate_records_the_derived_outcome(self):
        document = self._document(
            "real", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        classified = record_classification(document, P1GateBOutcome.POSITIVE_SIGNAL)
        self.assertEqual(classified["classification"], "P1 GATE B POSITIVE SIGNAL")
        self.assertEqual(classified["result_identity"], document["result_identity"])

    def test_an_outcome_the_rule_does_not_derive_is_rejected(self):
        document = self._document(
            "mismatch", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        with self.assertRaises(P1GateBError):
            record_classification(document, P1GateBOutcome.NEGATIVE_SIGNAL)

    def test_a_pre_result_state_is_never_recorded(self):
        document = self._document(
            "blocked", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        for outcome in (
            P1GateBOutcome.EVIDENCE_BLOCKED,
            P1GateBOutcome.STOP_INVALID,
        ):
            with self.assertRaises(P1GateBError):
                record_classification(document, outcome)

    def test_a_fixture_candidate_is_never_real_gate_b_evidence(self):
        document = self._document("fixture")
        self.assertIs(document["candidate"]["real_candidate_materialization"], False)
        with self.assertRaises(P1GateBError):
            record_classification(document, P1GateBOutcome.POSITIVE_SIGNAL)

    def test_an_outcome_cannot_be_recorded_twice(self):
        document = self._document(
            "twice", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        classified = record_classification(document, P1GateBOutcome.POSITIVE_SIGNAL)
        with self.assertRaises(P1GateBError):
            record_classification(classified, P1GateBOutcome.POSITIVE_SIGNAL)

    def test_a_non_outcome_value_is_rejected(self):
        document = self._document(
            "type", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        with self.assertRaises(TypeError):
            record_classification(document, "P1 GATE B POSITIVE SIGNAL")

    def test_a_recorded_outcome_that_contradicts_the_interval_is_rejected(self):
        document = self._document(
            "contradiction", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        tampered = {**document, "classification": "P1 GATE B NEGATIVE SIGNAL"}
        with self.assertRaises(P1GateBError):
            validate_gate_b_result(tampered)

    def test_an_unknown_outcome_string_is_rejected(self):
        document = self._document(
            "unknown", real=True, checkpoint=fixture_checkpoint(real=True)
        )
        with self.assertRaises(P1GateBError):
            validate_gate_b_result({**document, "classification": "GREAT"})


class GateBArtifactBindingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.identity = fixture_candidate_identity()

    def _artifact(self, name, results):
        return save_gate_b_artifact(
            self._tmp / f"{name}.json", results, candidate_identity=self.identity
        )

    def test_a_complete_gate_b_artifact_is_accepted(self):
        artifact = self._artifact("full", gate_b_game_results())
        self.assertIs(
            require_gate_b_artifact(artifact, candidate_identity=self.identity),
            artifact,
        )
        self.assertEqual(len(artifact.game_results), 100)

    def test_the_candidate_occupies_each_seat_once_per_seed_block(self):
        artifact = self._artifact("seats", gate_b_game_results())
        counts = {seat: 0 for seat in Seat}
        for game_result in artifact.game_results:
            counts[game_result.candidate_seat] += 1
        self.assertEqual(set(counts.values()), {25})

    def test_a_partial_seed_population_is_rejected(self):
        artifact = self._artifact(
            "partial", gate_b_game_results(seeds=GATE_B_ORDERED_SEEDS[:24])
        )
        with self.assertRaises(P1GateBError):
            require_gate_b_artifact(artifact, candidate_identity=self.identity)

    def test_a_substituted_comparator_identity_is_rejected(self):
        results = gate_b_game_results()
        path = self._tmp / "comparator.json"
        artifact = save_gate_b_artifact(path, results, candidate_identity=self.identity)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["plan"]["baseline_identity"] = "yakuhai-call"
        rewritten = self._tmp / "comparator-2.json"
        rewritten.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(P1GateBError):
            require_gate_b_artifact(
                load_single_round_artifact(rewritten),
                candidate_identity=self.identity,
            )
        self.assertEqual(artifact.plan.baseline_identity, PASSIVE_TSUMOGIRI_IDENTITY)

    def test_another_candidate_identity_is_rejected(self):
        artifact = self._artifact("other", gate_b_game_results())
        with self.assertRaises(P1GateBError):
            require_gate_b_artifact(
                artifact,
                candidate_identity=fixture_candidate_identity(weights_digest="a" * 64),
            )

    def test_a_corrupt_artifact_file_is_rejected_on_readback(self):
        path = self._tmp / "corrupt.json"
        save_gate_b_artifact(
            path, gate_b_game_results(), candidate_identity=self.identity
        )
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: len(text) // 2], encoding="utf-8")
        with self.assertRaises(SingleRoundArtifactError):
            load_single_round_artifact(path)

    def test_a_dropped_rotation_is_rejected_on_readback(self):
        path = self._tmp / "dropped.json"
        save_gate_b_artifact(
            path, gate_b_game_results(), candidate_identity=self.identity
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["game_results"].pop()
        rewritten = self._tmp / "dropped-2.json"
        rewritten.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(SingleRoundArtifactError):
            load_single_round_artifact(rewritten)

    def test_a_non_artifact_input_is_rejected(self):
        with self.assertRaises(TypeError):
            require_gate_b_artifact(object(), candidate_identity=self.identity)


class LockedUpstreamContractTest(unittest.TestCase):
    """Gate Bはupstream contractを一切変更しない。"""

    def test_the_p1_feature_identity_is_unchanged(self):
        self.assertEqual(
            P1_FEATURE_SEMANTICS_ID,
            "arena-learned-policy-offlineq-p1-keep-shanten-feature-v1",
        )
        self.assertEqual(
            P1_TENSOR_SCHEMA_VERSION,
            "arena-learned-policy-offlineq-p1-keep-shanten-tensor-v1",
        )
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)
        self.assertEqual(
            LOCKED_P1_SCHEMA_FINGERPRINT,
            "beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409",
        )

    def test_the_parameter_count_is_unchanged(self):
        self.assertEqual(P1_EXPECTED_PARAMETER_COUNT, 1_158_434)

    def test_the_action_vocabulary_identity_is_unchanged(self):
        self.assertEqual(LOCKED_VOCABULARY_VERSION, "lisjong-action-vocabulary-1")
        self.assertEqual(VOCABULARY_SIZE, 802)
        self.assertEqual(
            LOCKED_VOCABULARY_FINGERPRINT,
            "543c6bca832069dd88b22554b8546ddcd958840a7be7ed291b4ebab6302d7952",
        )

    def test_the_default_v1_serving_runtime_is_unchanged(self):
        runtime = HybridRuntime(
            arm="q", model=object(), supported_indices=frozenset({0}), conditions={}
        )
        self.assertIsNone(runtime.derive_features)
        self.assertEqual(runtime.feature_dimension, 8204)

    def test_a_derived_dimension_without_a_deriver_fails_closed(self):
        with self.assertRaises(HybridServingError):
            HybridRuntime(
                arm="q",
                model=object(),
                supported_indices=frozenset({0}),
                conditions={},
                feature_dimension=8241,
            )

    def test_a_non_callable_deriver_is_rejected(self):
        with self.assertRaises(TypeError):
            HybridRuntime(
                arm="q",
                model=object(),
                supported_indices=frozenset({0}),
                conditions={},
                derive_features="not-callable",
                feature_dimension=8241,
            )


if __name__ == "__main__":
    unittest.main()
