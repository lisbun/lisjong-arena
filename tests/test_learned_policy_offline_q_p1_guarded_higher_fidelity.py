"""Issue #175 guarded candidate higher-fidelity contract tests.

No test in this module runs the planned 100-game RiichiEnv evaluation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_p1_shanten_guard_fixtures import (
    fixture_activation_diagnostics,
    fixture_guard_diagnostics,
    inconclusive_delta,
    locked_checkpoint,
    negative_delta,
    positive_delta,
    scores_for_scaled_delta,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    LOCKED_P1_CANDIDATE,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_higher_fidelity import (
    BASELINE_IDENTITY,
    BASELINE_IMPLEMENTATION,
    BASELINE_SELECTED_LISJONG_REVISION,
    DEFAULT_ORDERED_SEEDS,
    EXPECTED_BASE_CANDIDATE_IDENTITY,
    EXPECTED_GUARDED_CANDIDATE_IDENTITY,
    GAME_COUNT,
    GAME_MODE,
    LOCKED_ENGINE_REVISION,
    MAX_WORKERS,
    ROTATIONS_PER_SEED,
    SEED_BLOCK_COUNT,
    HigherFidelityArtifactLocations,
    HigherFidelityError,
    HigherFidelityOutcome,
    baseline_block,
    bind_recorded_artifact,
    build_pre_execution_lock,
    build_result,
    candidate_block,
    classify_interval,
    classify_pre_result_state,
    derive_classification,
    load_result,
    lock_identity,
    plan_block,
    record_classification,
    render_pre_execution_lock,
    require_higher_fidelity_artifact,
    require_pre_execution_comment_url,
    require_seed_plan,
    result_identity,
    save_result,
    seed_freshness_block,
    validate_pre_execution_lock,
    validate_result,
)
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    load_single_round_artifact,
    save_single_round_artifact,
    summary_to_dict,
)
from lisjong_arena.single_round_evaluation import (
    aggregate_candidate_metrics,
    summarize_single_round_strength,
)

COMMENT_URL = (
    "https://github.com/lisbun/lisjong-arena/issues/175#issuecomment-1234567890"
)
RUNTIME = {
    "python_version": "3.14.6",
    "torch_version": "2.13.0+cpu",
    "riichienv_version": "0.4.8",
}


def provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision="a" * 40,
        lisjong_version="0.1.0",
        lisjong_revision=BASELINE_SELECTED_LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=LOCKED_ENGINE_REVISION,
        riichienv_version="0.4.8",
        python_version="3.14.6",
    )


def game_results(delta_for_seed=positive_delta, *, seeds=DEFAULT_ORDERED_SEEDS):
    return tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode=GAME_MODE,
            candidate_seat=Seat(rotation),
            scores=scores_for_scaled_delta(rotation, delta_for_seed(seed)),
            seat_round_stats=neutral_seat_round_stats_tuple(
                scores_for_scaled_delta(rotation, delta_for_seed(seed))
            ),
        )
        for seed in seeds
        for rotation in range(ROTATIONS_PER_SEED)
    )


def save_fixture_artifact(
    path: str | Path,
    *,
    candidate_identity=EXPECTED_GUARDED_CANDIDATE_IDENTITY,
    baseline_identity=BASELINE_IDENTITY,
    seeds=DEFAULT_ORDERED_SEEDS,
    delta_for_seed=positive_delta,
):
    results = game_results(delta_for_seed, seeds=seeds)
    evaluation = SingleRoundEvaluationResult(
        plan=SingleRoundEvaluationPlan(
            candidate=PolicySpec(identity=candidate_identity, factory=object),
            baseline=PolicySpec(identity=baseline_identity, factory=object),
            seeds=seeds,
        ),
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(candidate_identity, results),
    )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=provenance(),
    ):
        save_single_round_artifact(evaluation, path)
    return load_single_round_artifact(path)


class HigherFidelityFixture:
    def __init__(self, root: Path, delta_for_seed=positive_delta) -> None:
        self.root = root
        self.checkpoint = replace(
            locked_checkpoint(), path=root / "candidate-checkpoint"
        )
        self.locations = HigherFidelityArtifactLocations(
            candidate_checkpoint=str(self.checkpoint.path),
            strength_artifact=str(root / "strength.json"),
            result=str(root / "result.json"),
            classified_result=str(root / "classified.json"),
        )
        with mock.patch(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.load_p1_serving_checkpoint",
            return_value=self.checkpoint,
        ):
            self.lock = build_pre_execution_lock(
                self.checkpoint,
                locations=self.locations,
                external_freshness_confirmed=True,
                provenance=provenance(),
                runtime=RUNTIME,
            )
        self.artifact = save_fixture_artifact(
            self.locations.strength_artifact, delta_for_seed=delta_for_seed
        )
        self.summary = summarize_single_round_strength(
            aggregate_candidate_metrics(
                self.artifact.plan.candidate_identity, self.artifact.game_results
            ),
            self.artifact.game_results,
        )
        self.document = build_result(
            lock_document=self.lock,
            pre_execution_comment_url=COMMENT_URL,
            artifact=self.artifact,
            artifact_path=self.locations.strength_artifact,
            summary=self.summary,
            guard_diagnostics=fixture_guard_diagnostics(),
            activation_diagnostics=fixture_activation_diagnostics(),
            baseline_policy_instance_count=3 * GAME_COUNT,
        )


class CandidateAndBaselineBindingTest(unittest.TestCase):
    def test_exact_issue_162_and_173_identities_are_bound(self):
        block = candidate_block(locked_checkpoint())
        self.assertEqual(
            block["base_candidate_identity"], EXPECTED_BASE_CANDIDATE_IDENTITY
        )
        self.assertEqual(block["identity"], EXPECTED_GUARDED_CANDIDATE_IDENTITY)
        self.assertEqual(
            block["canonical_model_weights_digest"],
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest,
        )

    def test_wrong_weights_feature_support_and_vocabulary_are_rejected(self):
        base = locked_checkpoint()
        mutations = (
            lambda manifest: manifest["candidate_binding"].__setitem__(
                "canonical_model_weights_digest", "0" * 64
            ),
            lambda manifest: manifest["candidate_binding"]["p1_feature"].__setitem__(
                "schema_fingerprint", "0" * 64
            ),
            lambda manifest: manifest.__setitem__("supported_indices_digest", "0" * 64),
            lambda manifest: manifest["candidate_binding"][
                "action_vocabulary"
            ].__setitem__("fingerprint", "0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest = json.loads(json.dumps(base.manifest))
                mutate(manifest)
                with self.assertRaises(HigherFidelityError):
                    candidate_block(replace(base, manifest=manifest))

    def test_baseline_is_exact_and_not_a_mutable_alias(self):
        block = baseline_block()
        self.assertEqual(block["identity"], "yakuhai-call")
        self.assertEqual(block["implementation_class"], BASELINE_IMPLEMENTATION)
        self.assertFalse(block["mutable_alias"])
        self.assertNotIn("champion", json.dumps(block).lower())

    def test_catalog_factory_drift_is_rejected(self):
        with mock.patch.dict(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.POLICY_CATALOG",
            {"yakuhai-call": PolicySpec("yakuhai-call", object)},
            clear=True,
        ):
            with self.assertRaises(HigherFidelityError):
                baseline_block()


class PlanAndLockTest(unittest.TestCase):
    def test_default_plan_is_25_by_4_single_round_serial_non_formal(self):
        block = plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(547, 572)))
        self.assertEqual(block["seed_block_count"], SEED_BLOCK_COUNT)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], MAX_WORKERS)
        self.assertFalse(block["formal_test"])

    def test_only_a_contiguous_25_seed_reformulation_is_accepted(self):
        self.assertEqual(require_seed_plan(range(600, 625)), tuple(range(600, 625)))
        for invalid in (range(600, 624), tuple(range(600, 624)) + (626,)):
            with self.assertRaises(HigherFidelityError):
                require_seed_plan(invalid)

    def test_live_issue_freshness_confirmation_is_required(self):
        with self.assertRaises(HigherFidelityError):
            seed_freshness_block(external_freshness_confirmed=False)
        with self.assertRaisesRegex(HigherFidelityError, "SEED PLAN REFORMULATE"):
            seed_freshness_block(
                external_freshness_confirmed=True,
                additional_allocated_seeds={547},
            )

    def test_lock_is_deterministic_and_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            first = HigherFidelityFixture(Path(directory)).lock
            second = validate_pre_execution_lock(json.loads(json.dumps(first)))
            self.assertEqual(first, second)
            self.assertEqual(first["lock_identity"], lock_identity(first))
            rendered = render_pre_execution_lock(first)
            self.assertIn("Machine-readable lock", rendered)
            self.assertIn(EXPECTED_GUARDED_CANDIDATE_IDENTITY, rendered)
            self.assertIn(BASELINE_IDENTITY, rendered)

    def test_lock_requires_strict_checkpoint_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = replace(
                locked_checkpoint(), path=Path(directory) / "candidate"
            )
            changed_manifest = json.loads(json.dumps(checkpoint.manifest))
            changed_manifest["canonical_model_weights_digest"] = "0" * 64
            changed = replace(checkpoint, manifest=changed_manifest)
            locations = HigherFidelityArtifactLocations(
                str(checkpoint.path),
                str(Path(directory) / "artifact"),
                str(Path(directory) / "result"),
                str(Path(directory) / "classified"),
            )
            with mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.load_p1_serving_checkpoint",
                return_value=changed,
            ):
                with self.assertRaises(HigherFidelityError):
                    build_pre_execution_lock(
                        checkpoint,
                        locations=locations,
                        external_freshness_confirmed=True,
                        provenance=provenance(),
                        runtime=RUNTIME,
                    )

    def test_a_rehashed_lock_cannot_switch_to_issue_173_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = HigherFidelityFixture(Path(directory)).lock
            changed = json.loads(json.dumps(lock))
            changed["plan"] = plan_block(range(522, 547))
            changed["seed_freshness"]["ordered_seeds"] = list(range(522, 547))
            changed["lock_identity"] = lock_identity(changed)
            with self.assertRaisesRegex(HigherFidelityError, "collides"):
                validate_pre_execution_lock(changed)

    def test_baseline_source_revision_drift_requires_reformulation(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = replace(
                locked_checkpoint(), path=Path(directory) / "candidate"
            )
            locations = HigherFidelityArtifactLocations(
                str(checkpoint.path),
                str(Path(directory) / "artifact"),
                str(Path(directory) / "result"),
                str(Path(directory) / "classified"),
            )
            wrong = replace(provenance(), lisjong_revision="b" * 40)
            with mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.load_p1_serving_checkpoint",
                return_value=checkpoint,
            ):
                with self.assertRaisesRegex(HigherFidelityError, "BASELINE PLAN"):
                    build_pre_execution_lock(
                        checkpoint,
                        locations=locations,
                        external_freshness_confirmed=True,
                        provenance=wrong,
                        runtime=RUNTIME,
                    )

    def test_real_execution_requires_the_issue_175_lock_comment_url(self):
        self.assertEqual(require_pre_execution_comment_url(COMMENT_URL), COMMENT_URL)
        for invalid in (
            "https://github.com/lisbun/lisjong-arena/issues/173#issuecomment-1",
            "https://github.com/lisbun/lisjong-arena/issues/175",
            "not-a-url",
        ):
            with self.assertRaises(HigherFidelityError):
                require_pre_execution_comment_url(invalid)


class ClassificationTest(unittest.TestCase):
    def test_signal_negative_and_inconclusive_are_exhaustive(self):
        self.assertIs(classify_interval(0.01, 2.0), HigherFidelityOutcome.SIGNAL)
        self.assertIs(classify_interval(-2.0, -0.01), HigherFidelityOutcome.NEGATIVE)
        self.assertIs(classify_interval(-1.0, 1.0), HigherFidelityOutcome.INCONCLUSIVE)

    def test_zero_boundaries_are_inconclusive(self):
        self.assertIs(classify_interval(0.0, 1.0), HigherFidelityOutcome.INCONCLUSIVE)
        self.assertIs(classify_interval(-1.0, 0.0), HigherFidelityOutcome.INCONCLUSIVE)

    def test_blocked_and_invalid_are_distinct_pre_result_states(self):
        self.assertIs(
            classify_pre_result_state(evidence_available=False, protocol_valid=True),
            HigherFidelityOutcome.EVIDENCE_BLOCKED,
        )
        self.assertIs(
            classify_pre_result_state(evidence_available=True, protocol_valid=False),
            HigherFidelityOutcome.STOP_INVALID,
        )
        self.assertIsNone(
            classify_pre_result_state(evidence_available=True, protocol_valid=True)
        )

    def test_each_strength_branch_is_derived_from_canonical_seed_blocks(self):
        for delta, expected in (
            (positive_delta, HigherFidelityOutcome.SIGNAL),
            (negative_delta, HigherFidelityOutcome.NEGATIVE),
            (inconclusive_delta, HigherFidelityOutcome.INCONCLUSIVE),
        ):
            with self.subTest(outcome=expected):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HigherFidelityFixture(Path(directory), delta)
                    self.assertIs(derive_classification(fixture.document), expected)

    def test_secondary_metrics_cannot_change_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            changed = json.loads(json.dumps(fixture.document))
            changed["secondary_diagnostics"]["guarded_candidate"]["win_count"] = 0
            changed["secondary_diagnostics"]["guarded_candidate"]["win_rate"] = 0.0
            changed["result_identity"] = None
            changed["result_identity"] = result_identity(changed)
            self.assertIs(
                derive_classification(validate_result(changed)),
                HigherFidelityOutcome.SIGNAL,
            )


class ArtifactAndResultTest(unittest.TestCase):
    def test_matching_artifact_and_canonical_summary_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            self.assertIs(
                require_higher_fidelity_artifact(fixture.artifact, fixture.lock),
                fixture.artifact,
            )
            self.assertEqual(
                fixture.document["canonical_summary"], summary_to_dict(fixture.summary)
            )
            self.assertEqual(bind_recorded_artifact(fixture.document), fixture.artifact)

    def test_candidate_baseline_seed_and_plan_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            artifacts = (
                save_fixture_artifact(
                    Path(directory) / "wrong-candidate.json",
                    candidate_identity="wrong-candidate",
                ),
                save_fixture_artifact(
                    Path(directory) / "wrong-baseline.json",
                    baseline_identity="wrong-baseline",
                ),
                save_fixture_artifact(
                    Path(directory) / "wrong-seeds.json",
                    seeds=tuple(range(600, 625)),
                ),
            )
            for artifact in artifacts:
                with self.subTest(plan=artifact.plan):
                    with self.assertRaises(HigherFidelityError):
                        require_higher_fidelity_artifact(artifact, fixture.lock)

    def test_result_round_trip_is_strict_and_write_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            saved = save_result(fixture.locations.result, fixture.document)
            self.assertEqual(saved, fixture.document)
            with self.assertRaises(FileExistsError):
                save_result(fixture.locations.result, fixture.document)
            tampered = json.loads(Path(fixture.locations.result).read_text())
            tampered["baseline_policy_instance_count"] = 299
            Path(fixture.locations.result).write_text(json.dumps(tampered))
            with self.assertRaises(HigherFidelityError):
                load_result(fixture.locations.result)

    def test_artifact_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            path = Path(fixture.locations.strength_artifact)
            path.write_text(path.read_text() + " ")
            with self.assertRaises(HigherFidelityError):
                bind_recorded_artifact(fixture.document)

    def test_blocked_and_invalid_cannot_be_recorded_as_strength_results(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            for outcome in (
                HigherFidelityOutcome.EVIDENCE_BLOCKED,
                HigherFidelityOutcome.STOP_INVALID,
            ):
                with self.subTest(outcome=outcome):
                    with self.assertRaises(HigherFidelityError):
                        record_classification(
                            fixture.document, outcome, checkpoint=fixture.checkpoint
                        )

    def test_fresh_instance_counts_are_bound_into_the_result(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            self.assertEqual(
                fixture.document["candidate_serving_diagnostics"][
                    "policy_instance_count"
                ],
                GAME_COUNT,
            )
            self.assertEqual(
                fixture.document["baseline_policy_instance_count"], 3 * GAME_COUNT
            )
            changed = json.loads(json.dumps(fixture.document))
            changed["baseline_policy_instance_count"] = 299
            with self.assertRaises(HigherFidelityError):
                validate_result(changed)

    def test_serving_counts_and_rates_must_remain_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            changed = json.loads(json.dumps(fixture.document))
            changed["candidate_serving_diagnostics"]["activation_rate"] = 0.5
            changed["result_identity"] = result_identity(changed)
            with self.assertRaisesRegex(HigherFidelityError, "activation_rate"):
                validate_result(changed)

    def test_result_builder_rejects_a_noncanonical_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = HigherFidelityFixture(Path(directory))
            wrong_summary = replace(
                fixture.summary,
                mean_candidate_game_delta=(
                    fixture.summary.mean_candidate_game_delta + 1.0
                ),
            )
            with self.assertRaisesRegex(HigherFidelityError, "canonical"):
                build_result(
                    lock_document=fixture.lock,
                    pre_execution_comment_url=COMMENT_URL,
                    artifact=fixture.artifact,
                    artifact_path=fixture.locations.strength_artifact,
                    summary=wrong_summary,
                    guard_diagnostics=fixture_guard_diagnostics(),
                    activation_diagnostics=fixture_activation_diagnostics(),
                    baseline_policy_instance_count=3 * GAME_COUNT,
                )


if __name__ == "__main__":
    unittest.main()
