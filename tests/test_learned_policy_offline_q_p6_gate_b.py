"""Issue #183 P6 Gate B non-ML protocol tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q import p6_gate_b as gate_b
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    comparator_block,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import (
    fallback_policy_block,
    hybrid_activation_block,
)
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.single_round_artifact import (
    execution_provenance_to_dict,
    load_single_round_artifact,
    save_single_round_artifact,
    summary_to_dict,
)
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics

RUNTIME = {
    "python_version": "3.14.6",
    "torch_version": "2.13.0+cpu",
    "riichienv_version": "0.4.8",
}


def _provenance(*, arena_revision="a" * 40, lisjong_revision=None):
    current = provenance()
    return type(current)(
        execution_environment=current.execution_environment,
        lisjong_arena_version=current.lisjong_arena_version,
        lisjong_arena_revision=arena_revision,
        lisjong_version=current.lisjong_version,
        lisjong_revision=(
            gate_b.GATE_B_LISJONG_REVISION
            if lisjong_revision is None
            else lisjong_revision
        ),
        lisjong_engine_version=current.lisjong_engine_version,
        lisjong_engine_revision=gate_b.GATE_B_ENGINE_REVISION,
        riichienv_version=current.riichienv_version,
        python_version=current.python_version,
    )


def _locations(root: Path):
    return {
        "retention_backend": gate_b.RETENTION_BACKEND,
        "candidate_checkpoint": str(root / "checkpoint"),
        "gate_a_result": str(root / "gate-a-result.json"),
        "gate_a_classified": str(root / "gate-a-classified.json"),
        "strength_artifact": str(root / "strength.json"),
        "result": str(root / "result.json"),
        "classified_result": str(root / "classified.json"),
        "retention_keys": {
            "strength_artifact": gate_b.ARTIFACT_RETENTION_KEY,
            "result": gate_b.RESULT_RETENTION_KEY,
            "classified_result": gate_b.CLASSIFIED_RESULT_RETENTION_KEY,
        },
    }


def _lock(root: Path):
    run_provenance = _provenance()
    document = {
        "lock_schema_version": gate_b.LOCK_SCHEMA_VERSION,
        "experiment_id": gate_b.EXPERIMENT_ID,
        "source_issue": gate_b.SOURCE_ISSUE,
        "parent_issue": gate_b.PARENT_ISSUE,
        "predecessor_issues": list(gate_b.PREDECESSOR_ISSUES),
        "candidate": gate_b._expected_candidate_block(),
        "gate_a_binding": {
            "unclassified_result_identity": gate_b.EXPECTED_GATE_A_RESULT_IDENTITY,
            "classified_result_identity": gate_b.EXPECTED_GATE_A_CLASSIFIED_IDENTITY,
            "classification": gate_b.EXPECTED_GATE_A_CLASSIFICATION,
        },
        "serving": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "comparator": comparator_block(),
        "p6_training": gate_b.p6_training_block(),
        "plan": gate_b.plan_block(),
        "classification_rule": dict(gate_b.CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in gate_b.P6GateBOutcome],
        "artifact_locations": _locations(root),
        "seed_freshness": {
            "ordered_seeds": list(gate_b.DEFAULT_ORDERED_SEEDS),
            "repository_declared_collisions": [],
            "external_review_collisions": [],
            "external_open_closed_issue_review_confirmed": True,
            "fresh": True,
            "result_exposed": False,
        },
        "provenance": execution_provenance_to_dict(run_provenance),
        "runtime": dict(RUNTIME),
        "execution_target": {
            "branch": "main",
            "head_equals_origin_main": True,
            "merge_status": "merged-main",
            "merged_main_revision": run_provenance.lisjong_arena_revision,
            "source_pr": "lisbun/lisjong-arena#184",
            "source_issue": gate_b.SOURCE_ISSUE,
        },
        "no_rescue_boundary": gate_b.NO_RESCUE_BOUNDARY,
        "result_exposed": False,
        "lock_identity": None,
    }
    document["lock_identity"] = gate_b.lock_identity(document)
    return document


def _scores(candidate_seat: int):
    scores = [24_000, 24_000, 24_000, 24_000]
    scores[candidate_seat] = 28_000
    return tuple(scores)


def _save_artifact(
    path: Path,
    *,
    candidate_identity=gate_b.EXPECTED_CANDIDATE_IDENTITY,
    baseline_identity=PASSIVE_TSUMOGIRI_IDENTITY,
    seeds=gate_b.DEFAULT_ORDERED_SEEDS,
    max_steps=gate_b.MAX_STEPS,
    run_provenance=None,
):
    results = tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode=gate_b.GAME_MODE,
            candidate_seat=Seat(rotation),
            scores=_scores(rotation),
            seat_round_stats=neutral_seat_round_stats_tuple(_scores(rotation)),
        )
        for seed in seeds
        for rotation in range(gate_b.ROTATIONS_PER_SEED)
    )
    evaluation = SingleRoundEvaluationResult(
        plan=SingleRoundEvaluationPlan(
            candidate=PolicySpec(identity=candidate_identity, factory=object),
            baseline=PolicySpec(identity=baseline_identity, factory=object),
            seeds=seeds,
            max_steps=max_steps,
        ),
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(candidate_identity, results),
    )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=_provenance() if run_provenance is None else run_provenance,
    ):
        save_single_round_artifact(evaluation, path)
    return load_single_round_artifact(path)


def _bound_result(lock, artifact, path: Path):
    document = _result(
        artifact.summary.seed_block_statistics.normal_approx_95_interval_lower,
        artifact.summary.seed_block_statistics.normal_approx_95_interval_upper,
    )
    document["lock_identity"] = lock["lock_identity"]
    for name in (
        "candidate",
        "gate_a_binding",
        "serving",
        "fallback_policy",
        "comparator",
        "plan",
        "provenance",
    ):
        document[name] = lock[name]
    document["strength_artifact"] = gate_b.artifact_block(artifact, path)
    document["canonical_summary"] = summary_to_dict(artifact.summary)
    document["result_identity"] = gate_b._result_identity(document)
    return document


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
        "provenance": execution_provenance_to_dict(_provenance()),
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

    def test_missing_duplicate_reordered_and_substitute_seeds_are_rejected(self):
        invalid = (
            gate_b.DEFAULT_ORDERED_SEEDS[:-1],
            gate_b.DEFAULT_ORDERED_SEEDS[:-1] + (620,),
            (598, 597, *gate_b.DEFAULT_ORDERED_SEEDS[2:]),
            tuple(range(598, 623)),
        )
        for seeds in invalid:
            with self.subTest(seeds=seeds):
                with self.assertRaises(gate_b.P6GateBError):
                    gate_b.require_seed_plan(seeds)


class ExecutionTargetTest(unittest.TestCase):
    def test_execution_target_is_issue_183_specific(self):
        current = _provenance()
        with (
            mock.patch.object(
                gate_b,
                "_require_clean_arena_head",
                return_value=current.lisjong_arena_revision,
            ),
            mock.patch.object(
                gate_b,
                "_resolve_execution_target_revision",
                return_value=current.lisjong_arena_revision,
            ),
        ):
            target = gate_b.execution_target_block(current)
        self.assertEqual(target["source_pr"], "lisbun/lisjong-arena#184")
        self.assertEqual(target["source_issue"], gate_b.SOURCE_ISSUE)
        self.assertIs(target["head_equals_origin_main"], True)
        self.assertEqual(target["merged_main_revision"], current.lisjong_arena_revision)

    def test_execution_target_rejects_provenance_revision_drift(self):
        current = _provenance()
        with (
            mock.patch.object(
                gate_b, "_require_clean_arena_head", return_value="f" * 40
            ),
            mock.patch.object(
                gate_b, "_resolve_execution_target_revision", return_value="f" * 40
            ),
        ):
            with self.assertRaises(gate_b.P6GateBError):
                gate_b.execution_target_block(current)

    def test_execution_target_rejects_head_not_at_origin_main(self):
        current = _provenance()
        with (
            mock.patch.object(
                gate_b,
                "_require_clean_arena_head",
                return_value=current.lisjong_arena_revision,
            ),
            mock.patch.object(
                gate_b, "_resolve_execution_target_revision", return_value="f" * 40
            ),
        ):
            with self.assertRaisesRegex(gate_b.P6GateBError, "origin/main"):
                gate_b.execution_target_block(current)

    def test_dirty_worktree_is_rejected(self):
        with mock.patch.object(
            gate_b,
            "_require_clean_arena_head",
            side_effect=gate_b.P6GateBError("Arena worktree must be clean"),
        ):
            with self.assertRaisesRegex(gate_b.P6GateBError, "must be clean"):
                gate_b.execution_target_block(_provenance())

    def test_run_rechecks_origin_main_before_game_one(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = _lock(Path(directory))
            with (
                mock.patch.object(
                    gate_b, "validate_pre_execution_lock", return_value=lock
                ),
                mock.patch.object(
                    gate_b,
                    "_require_clean_arena_head",
                    return_value=_provenance().lisjong_arena_revision,
                ),
                mock.patch.object(
                    gate_b, "_resolve_execution_target_revision", return_value="f" * 40
                ),
                mock.patch.object(gate_b, "run_single_round_evaluation") as run,
            ):
                with self.assertRaisesRegex(gate_b.P6GateBError, "origin/main"):
                    gate_b.run_gate_b(
                        lock,
                        pre_execution_comment_url=(
                            "https://github.com/lisbun/lisjong-arena/issues/183"
                            "#issuecomment-1"
                        ),
                    )
            run.assert_not_called()


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
            self.assertFalse(any(Path(value).exists() for value in locations.values()))
            (root / "result.json").write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(gate_b.P6GateBError, "already exists"):
                gate_b._require_output_destinations_ready(locations)

    def test_missing_nondirectory_and_unwritable_parents_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_file = root / "parent-file"
            parent_file.write_text("not a directory", encoding="utf-8")
            cases = (
                (
                    {
                        "strength_artifact": str(root / "missing" / "strength.json"),
                        "result": str(root / "result.json"),
                        "classified_result": str(root / "classified.json"),
                    },
                    None,
                ),
                (
                    {
                        "strength_artifact": str(parent_file / "strength.json"),
                        "result": str(root / "result.json"),
                        "classified_result": str(root / "classified.json"),
                    },
                    None,
                ),
                (
                    {
                        "strength_artifact": str(root / "strength.json"),
                        "result": str(root / "result.json"),
                        "classified_result": str(root / "classified.json"),
                    },
                    False,
                ),
            )
            for locations, writable in cases:
                with self.subTest(locations=locations, writable=writable):
                    access = (
                        mock.patch.object(gate_b.os, "access", return_value=writable)
                        if writable is not None
                        else mock.patch.object(
                            gate_b.os, "access", wraps=gate_b.os.access
                        )
                    )
                    with access:
                        with self.assertRaises(gate_b.P6GateBError):
                            gate_b._require_output_destinations_ready(locations)

    def test_output_locations_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "same.json")
            with self.assertRaisesRegex(gate_b.P6GateBError, "distinct"):
                gate_b._require_output_destinations_ready(
                    {
                        "strength_artifact": path,
                        "result": path,
                        "classified_result": str(Path(directory) / "classified.json"),
                    }
                )

    def test_tampered_issue_specific_execution_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = _lock(Path(directory))
            document["execution_target"]["source_pr"] = "lisbun/lisjong-arena#176"
            document["lock_identity"] = gate_b.lock_identity(document)
            with mock.patch.object(gate_b, "runtime_block", return_value=RUNTIME):
                with self.assertRaisesRegex(gate_b.P6GateBError, "execution target"):
                    gate_b.validate_pre_execution_lock(document)


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
        second["serving_diagnostics"]["total_scaffold_fallbacks"] = 999
        second["serving_diagnostics"]["scaffold_fallback_rate"] = 0.999
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

    def test_invalid_nonfinite_or_reversed_interval_is_rejected(self):
        for lower, upper in ((float("nan"), 1.0), (-1.0, float("inf")), (2.0, 1.0)):
            with self.subTest(lower=lower, upper=upper):
                document = {
                    "canonical_summary": {
                        "seed_block_statistics": {
                            "seed_block_count": gate_b.SEED_BLOCK_COUNT,
                            "normal_approx_95_interval_lower": lower,
                            "normal_approx_95_interval_upper": upper,
                        }
                    }
                }
                with self.assertRaises(gate_b.P6GateBError):
                    gate_b._summary_statistics(document)


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

    def test_all_exact_candidate_and_gate_a_evidence_fields_are_bound(self):
        candidate_mutations = (
            ("candidate_identity", "wrong"),
            ("canonical_model_weights_digest", "0" * 64),
            ("source_dataset_identity", "0" * 64),
            ("supported_indices_digest", "0" * 64),
            ("selected_epoch", 19),
            ("p1_feature", {"tampered": True}),
            ("action_vocabulary", {"tampered": True}),
            ("training", {"tampered": True}),
        )
        for name, value in candidate_mutations:
            with self.subTest(candidate_field=name):
                document = _result(0.1, 1.0)
                document["candidate"][name] = value
                document["result_identity"] = gate_b._result_identity(document)
                with mock.patch.object(gate_b, "require_gate_b_provenance"):
                    with self.assertRaises(gate_b.P6GateBError):
                        gate_b.validate_result(document)
        for name in (
            "unclassified_result_identity",
            "classified_result_identity",
            "classification",
        ):
            with self.subTest(gate_a_field=name):
                document = _result(0.1, 1.0)
                document["gate_a_binding"][name] = "tampered"
                document["result_identity"] = gate_b._result_identity(document)
                with mock.patch.object(gate_b, "require_gate_b_provenance"):
                    with self.assertRaises(gate_b.P6GateBError):
                        gate_b.validate_result(document)

    def test_result_rejects_wrong_plan_comparator_provenance_and_classification(self):
        mutations = (
            lambda document: document["plan"].__setitem__(
                "ordered_seeds", list(range(598, 623))
            ),
            lambda document: document["plan"].__setitem__("rotation_count", 3),
            lambda document: document.__setitem__("comparator", {"tampered": True}),
            lambda document: document["provenance"].__setitem__(
                "lisjong_revision", "0" * 40
            ),
        )
        for mutate in mutations:
            document = _result(0.1, 1.0)
            mutate(document)
            document["result_identity"] = gate_b._result_identity(document)
            with self.assertRaises(gate_b.P6GateBError):
                gate_b.validate_result(document)
        document = _result(0.1, 1.0)
        document["classification"] = gate_b.P6GateBOutcome.INCONCLUSIVE.value
        with self.assertRaises(gate_b.P6GateBError):
            gate_b.validate_result(document, allow_classified=True)


class ArtifactAndResultBindingTest(unittest.TestCase):
    def test_artifact_candidate_baseline_seeds_and_max_steps_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                {"candidate_identity": "wrong-candidate"},
                {"baseline_identity": "wrong-baseline"},
                {"seeds": tuple(range(598, 623))},
                {"max_steps": gate_b.MAX_STEPS - 1},
            )
            for index, kwargs in enumerate(cases):
                artifact = _save_artifact(root / f"wrong-{index}.json", **kwargs)
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(gate_b.P6GateBError):
                        gate_b.require_gate_b_artifact(
                            artifact,
                            candidate_identity=gate_b.EXPECTED_CANDIDATE_IDENTITY,
                            ordered_seeds=gate_b.DEFAULT_ORDERED_SEEDS,
                        )

    def test_artifact_reader_rejects_reordered_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strength.json"
            _save_artifact(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["game_results"][0], document["game_results"][1] = (
                document["game_results"][1],
                document["game_results"][0],
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(Exception):
                load_single_round_artifact(path)

    def test_result_rebinds_digest_summary_and_provenance_to_retained_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = _lock(root)
            path = Path(lock["artifact_locations"]["strength_artifact"])
            artifact = _save_artifact(path)
            document = _bound_result(lock, artifact, path)
            with mock.patch.object(gate_b, "runtime_block", return_value=RUNTIME):
                self.assertEqual(
                    gate_b.bind_result_artifact(
                        document, artifact_path=path, lock_document=lock
                    ),
                    artifact,
                )
                for name, mutate in (
                    (
                        "digest",
                        lambda changed: changed["strength_artifact"].__setitem__(
                            "sha256", "0" * 64
                        ),
                    ),
                    (
                        "summary",
                        lambda changed: changed["canonical_summary"][
                            "seed_block_statistics"
                        ].__setitem__("normal_approx_95_interval_lower", -123.0),
                    ),
                ):
                    with self.subTest(name=name):
                        changed = json.loads(json.dumps(document))
                        mutate(changed)
                        changed["result_identity"] = gate_b._result_identity(changed)
                        with self.assertRaises(gate_b.P6GateBError):
                            gate_b.bind_result_artifact(
                                changed, artifact_path=path, lock_document=lock
                            )

    def test_artifact_provenance_must_match_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = _lock(root)
            path = Path(lock["artifact_locations"]["strength_artifact"])
            artifact = _save_artifact(
                path, run_provenance=_provenance(arena_revision="b" * 40)
            )
            document = _bound_result(lock, artifact, path)
            document["provenance"] = lock["provenance"]
            document["result_identity"] = gate_b._result_identity(document)
            with mock.patch.object(gate_b, "runtime_block", return_value=RUNTIME):
                with self.assertRaisesRegex(gate_b.P6GateBError, "provenance"):
                    gate_b.bind_result_artifact(
                        document, artifact_path=path, lock_document=lock
                    )

    def test_gate_a_signal_identity_is_fixed(self):
        self.assertEqual(
            gate_b.EXPECTED_GATE_A_CLASSIFICATION,
            "P6 CONSERVATIVE-Q GATE A SIGNAL",
        )
        self.assertEqual(len(gate_b.EXPECTED_GATE_A_RESULT_IDENTITY), 64)
        self.assertEqual(len(gate_b.EXPECTED_GATE_A_CLASSIFIED_IDENTITY), 64)


if __name__ == "__main__":
    unittest.main()
