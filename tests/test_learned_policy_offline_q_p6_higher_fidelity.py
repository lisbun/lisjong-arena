"""Issue #185 purpose-specific orchestration tests.

All game records are synthetic fixtures.  This module never invokes the fresh
622..646 scientific population through RiichiEnv.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q import p6_gate_b
from lisjong_arena.learned_policy_offline_q import p6_higher_fidelity as higher
from lisjong_arena.learned_policy_offline_q.p1_serving import (
    fallback_policy_block,
    hybrid_activation_block,
)
from lisjong_arena.learned_policy_offline_q.strength import ActivationDiagnostics
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
)
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics

COMMENT_URL = (
    "https://github.com/lisbun/lisjong-arena/issues/185#issuecomment-1234567890"
)
RUNTIME = {
    "python_version": "3.14.6",
    "torch_version": "2.13.0+cpu",
    "riichienv_version": "0.4.8",
}


def _provenance(
    *, arena_revision="a" * 40, lisjong_revision=None, engine_revision=None
):
    current = provenance()
    return type(current)(
        execution_environment=current.execution_environment,
        lisjong_arena_version=current.lisjong_arena_version,
        lisjong_arena_revision=arena_revision,
        lisjong_version=current.lisjong_version,
        lisjong_revision=(
            higher.P6_LISJONG_REVISION if lisjong_revision is None else lisjong_revision
        ),
        lisjong_engine_version=current.lisjong_engine_version,
        lisjong_engine_revision=(
            higher.P6_ENGINE_REVISION if engine_revision is None else engine_revision
        ),
        riichienv_version=higher.RIICHIENV_VERSION,
        python_version=higher.PYTHON_VERSION,
    )


def _locations(root: Path) -> dict[str, object]:
    return {
        "retention_backend": higher.RETENTION_BACKEND,
        "candidate_checkpoint": str(root / "candidate"),
        "gate_a_result": str(root / "gate-a-result.json"),
        "gate_a_classified": str(root / "gate-a-classified.json"),
        "gate_b_result": str(root / "gate-b-result.json"),
        "gate_b_classified": str(root / "gate-b-classified.json"),
        "strength_artifact": str(root / "strength.json"),
        "result": str(root / "result.json"),
        "classified_result": str(root / "classified.json"),
        "retention_keys": {
            "strength_artifact": higher.ARTIFACT_RETENTION_KEY,
            "result": higher.RESULT_RETENTION_KEY,
            "classified_result": higher.CLASSIFIED_RESULT_RETENTION_KEY,
        },
    }


def _lock(root: Path) -> dict[str, object]:
    run_provenance = _provenance()
    document = {
        "lock_schema_version": higher.LOCK_SCHEMA_VERSION,
        "experiment_id": higher.EXPERIMENT_ID,
        "source_issue": higher.SOURCE_ISSUE,
        "source_pr": higher.SOURCE_PR,
        "parent_issue": higher.PARENT_ISSUE,
        "predecessor_issues": list(higher.PREDECESSOR_ISSUES),
        "primary_changed_axis": higher.PRIMARY_CHANGED_AXIS,
        "locked_unchanged_axes": list(higher.LOCKED_UNCHANGED_AXES),
        "candidate": p6_gate_b._expected_candidate_block(),
        "prior_evidence": higher.prior_evidence_block(),
        "serving": hybrid_activation_block(),
        "selection_guard": None,
        "fallback_policy": fallback_policy_block(),
        "baseline": higher.baseline_block(),
        "p6_training": higher.p6_training_block(),
        "plan": higher.plan_block(),
        "classification_rule": dict(higher.CLASSIFICATION_RULE),
        "outcomes": [outcome.value for outcome in higher.P6HigherFidelityOutcome],
        "artifact_locations": _locations(root),
        "seed_freshness": {
            "ordered_seeds": list(higher.DEFAULT_ORDERED_SEEDS),
            "repository_declared_collisions": [],
            "external_review_collisions": [],
            "external_open_closed_issue_review_confirmed": True,
            "known_local_private_allocation_review_confirmed": True,
            "fresh": True,
            "result_exposed": False,
        },
        "provenance": execution_provenance_to_dict(run_provenance),
        "runtime": dict(RUNTIME),
        "execution_target": {
            "branch": "main",
            "head_equals_origin_main_at_lock": True,
            "merge_status": "merged-main",
            "merged_main_revision": run_provenance.lisjong_arena_revision,
            "source_issue": higher.SOURCE_ISSUE,
            "source_pr": higher.SOURCE_PR,
        },
        "no_rescue_boundary": higher.NO_RESCUE_BOUNDARY,
        "result_exposed": False,
        "lock_identity": None,
    }
    document["lock_identity"] = higher.lock_identity(document)
    return higher.validate_pre_execution_lock(document)


def _scores(candidate_seat: int) -> tuple[int, int, int, int]:
    scores = [24_000, 24_000, 24_000, 24_000]
    scores[candidate_seat] = 28_000
    return tuple(scores)


def _save_artifact(path: Path, lock: dict[str, object], *, baseline=None, seeds=None):
    selected_seeds = higher.DEFAULT_ORDERED_SEEDS if seeds is None else tuple(seeds)
    baseline_identity = higher.BASELINE_IDENTITY if baseline is None else baseline
    results = tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode=higher.GAME_MODE,
            candidate_seat=Seat(rotation),
            scores=_scores(rotation),
            seat_round_stats=neutral_seat_round_stats_tuple(_scores(rotation)),
        )
        for seed in selected_seeds
        for rotation in range(higher.ROTATIONS_PER_SEED)
    )
    evaluation = SingleRoundEvaluationResult(
        plan=SingleRoundEvaluationPlan(
            candidate=PolicySpec(
                identity=higher.EXPECTED_CANDIDATE_IDENTITY, factory=object
            ),
            baseline=PolicySpec(identity=baseline_identity, factory=object),
            seeds=selected_seeds,
            max_steps=higher.MAX_STEPS,
        ),
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(
            higher.EXPECTED_CANDIDATE_IDENTITY, results
        ),
    )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=parse_lock_provenance(lock),
    ):
        save_single_round_artifact(evaluation, path)
    return load_single_round_artifact(path)


def parse_lock_provenance(lock):
    from lisjong_arena.single_round_artifact import parse_execution_provenance

    return parse_execution_provenance(lock["provenance"])


def _diagnostics() -> ActivationDiagnostics:
    return ActivationDiagnostics(
        policy_instance_count=100,
        total_decisions=1000,
        total_activations=700,
        total_scaffold_fallbacks=300,
        total_support_fallbacks=0,
    )


class IdentityAndPopulationTest(unittest.TestCase):
    def test_issue_185_has_purpose_specific_identity_and_metadata(self):
        self.assertNotEqual(higher.EXPERIMENT_ID, p6_gate_b.EXPERIMENT_ID)
        self.assertNotEqual(higher.LOCK_SCHEMA_VERSION, p6_gate_b.LOCK_SCHEMA_VERSION)
        self.assertNotEqual(
            higher.RESULT_SCHEMA_VERSION, p6_gate_b.RESULT_SCHEMA_VERSION
        )
        self.assertEqual(higher.SOURCE_ISSUE, "lisbun/lisjong-arena#185")
        self.assertEqual(higher.SOURCE_PR, "lisbun/lisjong-arena#187")
        self.assertTrue(higher.ARTIFACT_RETENTION_KEY.startswith("offlineq-185-"))

    def test_population_is_exact_25_by_4_serial_non_formal(self):
        block = higher.plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(622, 647)))
        self.assertEqual(block["seed_block_count"], 25)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], 1)
        self.assertFalse(block["formal_test"])

    def test_prior_ranges_and_external_collisions_are_rejected(self):
        self.assertTrue(
            set(range(547, 622)).issubset(higher.declared_allocated_seeds())
        )
        with self.assertRaises(higher.P6HigherFidelityError):
            higher.require_seed_plan(range(597, 622))
        with self.assertRaisesRegex(
            higher.P6HigherFidelityError, "SEED PLAN REFORMULATE"
        ):
            higher.seed_freshness_block(
                external_freshness_confirmed=True,
                additional_allocated_seeds=(630,),
            )


class CandidateBaselineAndProvenanceTest(unittest.TestCase):
    def test_exact_prior_evidence_and_unguarded_p6_are_bound(self):
        binding = higher.prior_evidence_block()
        self.assertEqual(
            binding["gate_b_result_identity"],
            higher.EXPECTED_GATE_B_RESULT_IDENTITY,
        )
        self.assertEqual(
            binding["gate_b_strength_artifact_sha256"],
            higher.EXPECTED_GATE_B_ARTIFACT_SHA256,
        )
        self.assertEqual(hybrid_activation_block()["serving_feature_dimension"], 8241)
        with tempfile.TemporaryDirectory() as directory:
            lock = _lock(Path(directory))
        self.assertIsNone(lock["selection_guard"])
        self.assertEqual(lock["candidate"], p6_gate_b._expected_candidate_block())

    def test_baseline_is_exact_factory_class_module_and_source_revision(self):
        baseline = higher.baseline_block()
        self.assertEqual(baseline["identity"], "yakuhai-call")
        self.assertEqual(baseline["factory"], higher.BASELINE_FACTORY)
        self.assertEqual(
            baseline["implementation_class"], higher.BASELINE_IMPLEMENTATION_CLASS
        )
        self.assertEqual(
            baseline["implementation_module"], higher.BASELINE_IMPLEMENTATION_MODULE
        )
        self.assertEqual(
            baseline["implementation_source_revision"],
            "a0666d24e66179a45fd6e231a3cbd489b492d162",
        )
        self.assertFalse(baseline["mutable_alias"])

    def test_exact_183_dependencies_are_required_and_179_revisions_rejected(self):
        higher.require_p6_higher_fidelity_provenance(_provenance())
        for changed in (
            {"lisjong_revision": "4a5c1c724739882eb33a6915b278afb3697162ad"},
            {"engine_revision": "11e83d03fe06f9277eb5d6c6b9b41cc25a142bba"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(higher.P6HigherFidelityError):
                    higher.require_p6_higher_fidelity_provenance(_provenance(**changed))

    def test_runtime_drift_is_rejected(self):
        higher.require_runtime(RUNTIME)
        for field in RUNTIME:
            with self.subTest(field=field):
                changed = {**RUNTIME, field: "drift"}
                with self.assertRaises(higher.P6HigherFidelityError):
                    higher.require_runtime(changed)


class ExecutionTargetAndDestinationTest(unittest.TestCase):
    def test_execution_target_requires_clean_provenance_equal_origin_main(self):
        run_provenance = _provenance()
        with (
            mock.patch.object(higher, "_git_output", return_value="main\n"),
            mock.patch.object(
                higher,
                "_require_clean_arena_head",
                return_value=run_provenance.lisjong_arena_revision,
            ),
            mock.patch.object(
                higher,
                "_resolve_execution_target_revision",
                return_value=run_provenance.lisjong_arena_revision,
            ),
        ):
            target = higher.execution_target_block(run_provenance)
        self.assertEqual(target["source_issue"], higher.SOURCE_ISSUE)
        self.assertEqual(target["source_pr"], higher.SOURCE_PR)
        self.assertEqual(target["merge_status"], "merged-main")

        with mock.patch.object(higher, "_git_output", return_value="issue-185\n"):
            with self.assertRaisesRegex(higher.P6HigherFidelityError, "main branch"):
                higher.execution_target_block(run_provenance)

    def test_pr_branch_and_old_issue_metadata_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = _lock(Path(directory))
        for field, value in (
            ("merge_status", "pull-request-head"),
            ("source_issue", "lisbun/lisjong-arena#183"),
            ("source_pr", "lisbun/lisjong-arena#184"),
        ):
            with self.subTest(field=field):
                changed = {
                    **lock,
                    "execution_target": {**lock["execution_target"], field: value},
                }
                changed["lock_identity"] = higher.lock_identity(changed)
                with self.assertRaises(higher.P6HigherFidelityError):
                    higher.validate_pre_execution_lock(changed)

    def test_all_destination_failures_are_rejected_without_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locations = _locations(root)
            existing = root / "strength.json"
            existing.touch()
            with self.assertRaisesRegex(higher.P6HigherFidelityError, "already exists"):
                higher._require_output_destinations_ready(locations)
            existing.unlink()

            locations["strength_artifact"] = str(root / "missing" / "strength.json")
            with self.assertRaisesRegex(higher.P6HigherFidelityError, "does not exist"):
                higher._require_output_destinations_ready(locations)

            parent_file = root / "parent-file"
            parent_file.touch()
            locations["strength_artifact"] = str(parent_file / "strength.json")
            with self.assertRaisesRegex(
                higher.P6HigherFidelityError, "not a directory"
            ):
                higher._require_output_destinations_ready(locations)

            locations = _locations(root)
            locations["result"] = locations["strength_artifact"]
            with self.assertRaisesRegex(higher.P6HigherFidelityError, "distinct"):
                higher._require_output_destinations_ready(locations)

            locations = _locations(root)
            with mock.patch.object(higher.os, "access", return_value=False):
                with self.assertRaisesRegex(
                    higher.P6HigherFidelityError, "not writable"
                ):
                    higher._require_output_destinations_ready(locations)

    def test_run_destination_failure_stops_before_evaluator_and_does_not_refetch_main(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = _lock(root)
            missing = root / "missing" / "strength.json"
            lock["artifact_locations"] = {
                **lock["artifact_locations"],
                "strength_artifact": str(missing),
            }
            lock["lock_identity"] = higher.lock_identity(lock)
            runner = mock.Mock()
            with (
                mock.patch.object(higher, "_git_output", return_value="main\n"),
                mock.patch.object(
                    higher,
                    "_require_clean_arena_head",
                    return_value=_provenance().lisjong_arena_revision,
                ),
                mock.patch.object(
                    higher, "collect_execution_provenance", return_value=_provenance()
                ),
                mock.patch.object(higher, "runtime_block", return_value=RUNTIME),
                mock.patch.object(higher, "run_single_round_evaluation", runner),
                mock.patch.object(
                    higher,
                    "_resolve_execution_target_revision",
                    side_effect=AssertionError(
                        "run must not rebind moving origin/main"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    higher.P6HigherFidelityError, "parent directory does not exist"
                ):
                    higher.run_higher_fidelity(
                        lock, pre_execution_comment_url=COMMENT_URL
                    )
            runner.assert_not_called()


class ArtifactResultAndClassificationTest(unittest.TestCase):
    def test_comment_namespace_and_pre_result_states_are_exact(self):
        self.assertEqual(
            higher.require_pre_execution_comment_url(COMMENT_URL), COMMENT_URL
        )
        with self.assertRaises(higher.P6HigherFidelityError):
            higher.require_pre_execution_comment_url(
                "https://github.com/lisbun/lisjong-arena/issues/183#issuecomment-1"
            )
        self.assertIs(
            higher.classify_pre_result_state(
                evidence_available=False, protocol_valid=True
            ),
            higher.P6HigherFidelityOutcome.EVIDENCE_BLOCKED,
        )
        self.assertIs(
            higher.classify_pre_result_state(
                evidence_available=True, protocol_valid=False
            ),
            higher.P6HigherFidelityOutcome.STOP_INVALID,
        )

    def test_classification_is_exhaustive_and_diagnostics_do_not_override(self):
        cases = (
            ((0.1, 2.0), higher.P6HigherFidelityOutcome.SIGNAL),
            ((-2.0, -0.1), higher.P6HigherFidelityOutcome.NEGATIVE),
            ((-1.0, 1.0), higher.P6HigherFidelityOutcome.INCONCLUSIVE),
            ((0.0, 1.0), higher.P6HigherFidelityOutcome.INCONCLUSIVE),
        )
        for interval, outcome in cases:
            with self.subTest(interval=interval):
                self.assertIs(higher.classify_interval(*interval), outcome)
        self.assertFalse(
            higher.CLASSIFICATION_RULE["serving_diagnostics_may_alter_classification"]
        )

    def test_synthetic_artifact_and_result_rebind_raw_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = _lock(root)
            path = Path(lock["artifact_locations"]["strength_artifact"])
            artifact = _save_artifact(path, lock)
            document = higher.build_result(
                lock_document=lock,
                pre_execution_comment_url=COMMENT_URL,
                artifact=artifact,
                artifact_path=path,
                summary=artifact.summary,
                activation_diagnostics=_diagnostics(),
                baseline_policy_instance_count=300,
            )
            higher.bind_result_artifact(
                document, artifact_path=path, lock_document=lock
            )
            classified = higher.record_classification(
                document, higher.derive_classification(document)
            )
            higher.validate_result(classified, allow_classified=True)

            tampered = {**document, "baseline_policy_instance_count": 299}
            tampered["result_identity"] = higher.result_identity(tampered)
            with self.assertRaises(higher.P6HigherFidelityError):
                higher.validate_result(tampered)

    def test_wrong_baseline_or_partial_population_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = _lock(root)
            wrong = _save_artifact(root / "wrong.json", lock, baseline="champion")
            with self.assertRaises(higher.P6HigherFidelityError):
                higher.require_higher_fidelity_artifact(wrong, lock)

            partial = _save_artifact(
                root / "partial.json", lock, seeds=higher.DEFAULT_ORDERED_SEEDS[:-1]
            )
            with self.assertRaises(higher.P6HigherFidelityError):
                higher.require_higher_fidelity_artifact(partial, lock)


if __name__ == "__main__":
    unittest.main()
