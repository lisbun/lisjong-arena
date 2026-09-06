"""Issue #165 FiniteHorizon-teacher curriculum contract tests (no ML extra).

このmoduleはteacher arm、dataset artifact、support binding、rollout protocol、
result validation、pre-execution lock、そしてpredecessor Issueのlocked
behaviorがdriftしていないことをtorch無しで検証する。

real dataset generation、real training、real 100-game rolloutは実行しない。
"""

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _learned_policy_offline_q_fh_curriculum_fixtures import (
    ARM_LEGAL_INDICES,
    FIXTURE_PROVENANCE,
    fixture_candidate,
    inconclusive_delta,
    negative_delta,
    positive_delta,
    rollout_game_results,
    rollout_result_document,
    save_rollout_artifact,
    write_curriculum_dataset,
    write_dataset_pair,
)
from _single_round_artifact_fixtures import provenance
from lisjong.policies import FiniteHorizonCompletionPolicy
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q import (
    fh_curriculum,
    fh_curriculum_candidate,
    fh_curriculum_dataset,
    fh_curriculum_diagnostics,
    fh_curriculum_generation,
    fh_curriculum_lock,
    fh_curriculum_rollout,
)
from lisjong_arena.learned_policy_offline_q.artifact import (
    DATASET_SCHEMA_VERSION,
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.errors import OfflineQArtifactError
from lisjong_arena.learned_policy_offline_q.fh_curriculum import (
    CurriculumArm,
    CurriculumOutcome,
)
from lisjong_arena.learned_policy_offline_q.p1_candidate import LOCKED_P1_CANDIDATE
from lisjong_arena.learned_policy_offline_q.p1_features import (
    LOCKED_P1_SCHEMA_FINGERPRINT,
    P1_FEATURE_DIMENSION,
    p1_schema_fingerprint,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b import (
    GATE_B_ORDERED_SEEDS,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    p1_training_block,
    verify_locked_q_protocol_delta,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import fallback_policy_block
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS as OFFLINE_Q_DATASET_SEEDS,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    MAXIMUM_EPOCHS,
    Split,
)
from lisjong_arena.learned_policy_offline_q.q_training import locked_training_block
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
from lisjong_arena.learned_policy_stage2 import protocol as stage2
from lisjong_arena.policy_catalog import (
    POLICY_CATALOG,
    create_finite_horizon,
    create_yakuhai_call,
)


class TeacherArmTests(unittest.TestCase):
    def test_arms_use_the_curated_catalog_factories(self):
        self.assertIs(
            fh_curriculum.teacher_factory(CurriculumArm.CONTROL), create_yakuhai_call
        )
        self.assertIs(
            fh_curriculum.teacher_factory(CurriculumArm.CURRICULUM),
            create_finite_horizon,
        )
        self.assertIs(POLICY_CATALOG["yakuhai-call"].factory, create_yakuhai_call)
        self.assertIs(POLICY_CATALOG["finite-horizon"].factory, create_finite_horizon)

    def test_teacher_blocks_record_identity_class_factory_and_population(self):
        control = fh_curriculum.teacher_block(CurriculumArm.CONTROL)
        curriculum = fh_curriculum.teacher_block(CurriculumArm.CURRICULUM)
        self.assertEqual(control["identity"], "yakuhai-call")
        self.assertEqual(
            control["policy_class"],
            "YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy",
        )
        self.assertEqual(control["population"], "yakuhai-call x4")
        self.assertEqual(curriculum["identity"], "finite-horizon")
        self.assertEqual(curriculum["policy_class"], "FiniteHorizonCompletionPolicy")
        self.assertEqual(curriculum["population"], "finite-horizon x4")
        self.assertEqual(
            curriculum["factory"], "lisjong_arena.policy_catalog:create_finite_horizon"
        )
        self.assertNotEqual(control, curriculum)

    def test_teacher_population_is_a_fresh_instance_per_seat(self):
        for arm, expected in (
            (CurriculumArm.CONTROL, create_yakuhai_call),
            (CurriculumArm.CURRICULUM, create_finite_horizon),
        ):
            population = fh_curriculum_generation.build_arm_teacher_population(arm)
            self.assertEqual(set(population), set(Seat))
            self.assertEqual(len({id(policy) for policy in population.values()}), 4)
            self.assertEqual(
                {type(policy) for policy in population.values()},
                {type(expected())},
            )

    def test_teacher_population_is_a_fresh_instance_per_game(self):
        first = fh_curriculum_generation.build_arm_teacher_population(
            CurriculumArm.CURRICULUM
        )
        second = fh_curriculum_generation.build_arm_teacher_population(
            CurriculumArm.CURRICULUM
        )
        self.assertTrue(
            {id(policy) for policy in first.values()}.isdisjoint(
                {id(policy) for policy in second.values()}
            )
        )

    def test_wrong_teacher_class_is_rejected(self):
        with mock.patch.dict(
            fh_curriculum._TEACHER_FACTORIES,
            {CurriculumArm.CURRICULUM: create_yakuhai_call},
        ):
            with self.assertRaises(Exception):
                fh_curriculum_generation.build_arm_teacher_population(
                    CurriculumArm.CURRICULUM
                )


class SeedPlanTests(unittest.TestCase):
    def test_dataset_population_shape(self):
        self.assertEqual(fh_curriculum.DATASET_ORDERED_SEEDS, tuple(range(465, 497)))
        self.assertEqual(fh_curriculum.DATASET_TRAIN_SEEDS, tuple(range(465, 485)))
        self.assertEqual(fh_curriculum.DATASET_VALIDATION_SEEDS, tuple(range(485, 491)))
        self.assertEqual(fh_curriculum.DATASET_TEST_SEEDS, tuple(range(491, 497)))
        self.assertEqual(fh_curriculum.DATASET_HANCHAN_COUNT, 32)
        self.assertEqual(fh_curriculum.DATASET_GAME_MODE, "4p-red-half")
        self.assertEqual(fh_curriculum.SPLIT_UNIT, "whole_hanchan")

    def test_rollout_population_shape(self):
        self.assertEqual(fh_curriculum.ROLLOUT_ORDERED_SEEDS, tuple(range(497, 522)))
        self.assertEqual(fh_curriculum.ROLLOUT_SEED_BLOCK_COUNT, 25)
        self.assertEqual(fh_curriculum.ROLLOUT_ROTATIONS_PER_SEED, 4)
        self.assertEqual(fh_curriculum.ROLLOUT_GAME_COUNT, 100)
        self.assertEqual(fh_curriculum.ROLLOUT_GAME_MODE, "4p-red-single")
        self.assertEqual(fh_curriculum.ROLLOUT_MAX_WORKERS, 1)
        self.assertIs(fh_curriculum.ROLLOUT_FORMAL_TEST, False)

    def test_seeds_do_not_collide_with_declared_populations(self):
        report = fh_curriculum.check_seed_freshness()
        self.assertTrue(report["fresh"])
        self.assertEqual(report["dataset_collisions"], [])
        self.assertEqual(report["rollout_collisions"], [])
        self.assertIsNone(report["status"])

    def test_collision_is_reported_as_seed_plan_reformulate(self):
        with mock.patch.object(
            fh_curriculum,
            "declared_allocated_seeds",
            return_value=frozenset({465, 500}),
        ):
            report = fh_curriculum.check_seed_freshness()
            self.assertFalse(report["fresh"])
            self.assertEqual(report["dataset_collisions"], [465])
            self.assertEqual(report["rollout_collisions"], [500])
            self.assertEqual(report["status"], fh_curriculum.SEED_PLAN_REFORMULATE)
            with self.assertRaises(fh_curriculum.FiniteHorizonCurriculumError):
                fh_curriculum.require_fresh_seed_plan()

    def test_collision_after_result_exposure_is_stop_invalid(self):
        with mock.patch.object(
            fh_curriculum, "declared_allocated_seeds", return_value=frozenset({497})
        ):
            report = fh_curriculum.check_seed_freshness(result_exposed=True)
            self.assertEqual(report["status"], CurriculumOutcome.STOP_INVALID.value)

    def test_split_resolution_is_fail_closed(self):
        self.assertIs(fh_curriculum.split_for_seed(465), Split.TRAIN)
        self.assertIs(fh_curriculum.split_for_seed(485), Split.VALIDATION)
        self.assertIs(fh_curriculum.split_for_seed(496), Split.TEST)
        with self.assertRaises(fh_curriculum.FiniteHorizonCurriculumError):
            fh_curriculum.split_for_seed(464)
        with self.assertRaises(fh_curriculum.FiniteHorizonCurriculumError):
            fh_curriculum.require_rollout_seed(496)


class DatasetArtifactTests(unittest.TestCase):
    def test_both_arms_share_seeds_split_and_protocol(self):
        with TemporaryDirectory() as tmp:
            control, curriculum = write_dataset_pair(Path(tmp))
            fh_curriculum_dataset.require_dataset_pair(control, curriculum)
            self.assertEqual(
                control.manifest["protocol"], curriculum.manifest["protocol"]
            )
            for split in Split:
                self.assertEqual(
                    sorted(
                        {control.rows[i].seed for i in control.split_indices(split)}
                    ),
                    sorted(
                        {
                            curriculum.rows[i].seed
                            for i in curriculum.split_indices(split)
                        }
                    ),
                )
            self.assertNotEqual(control.identity, curriculum.identity)
            self.assertNotEqual(
                control.manifest["teacher"], curriculum.manifest["teacher"]
            )

    def test_manifest_binds_every_required_identity(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CURRICULUM
            )
            manifest = dataset.manifest
            self.assertEqual(
                manifest["dataset_schema_version"],
                "arena-learned-policy-finite-horizon-curriculum-dataset-v1",
            )
            self.assertEqual(manifest["arm"]["arm"], "F")
            self.assertEqual(manifest["teacher"]["identity"], "finite-horizon")
            self.assertEqual(manifest["feature"], feature_block())
            self.assertEqual(
                manifest["derived_feature"]["schema_fingerprint"],
                LOCKED_P1_SCHEMA_FINGERPRINT,
            )
            self.assertEqual(manifest["vocabulary"], vocabulary_block())
            self.assertEqual(manifest["provenance"], FIXTURE_PROVENANCE)
            self.assertEqual(
                manifest["transition_semantics"],
                fh_curriculum.transition_semantics_block(),
            )
            self.assertEqual(
                manifest["reward_semantics"], fh_curriculum.reward_semantics_block()
            )
            self.assertEqual(manifest["totals"]["game_count"], 32)
            self.assertEqual(len(manifest["files"]), 5)
            self.assertGreater(manifest["totals"]["terminal_row_count"], 0)
            self.assertGreater(
                manifest["totals"]["teacher_action_family_counts"]["discard"], 0
            )

    def test_write_once(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "dataset"
            write_curriculum_dataset(destination, CurriculumArm.CONTROL)
            with self.assertRaises(FileExistsError):
                write_curriculum_dataset(destination, CurriculumArm.CONTROL)

    def test_wrong_arm_is_rejected_on_readback(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CONTROL
            )
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.load_curriculum_dataset(
                    dataset.path, arm=CurriculumArm.CURRICULUM
                )

    def test_wrong_teacher_in_the_manifest_is_rejected(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CURRICULUM
            )
            manifest = json.loads(
                (dataset.path / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["teacher"] = fh_curriculum.teacher_block(CurriculumArm.CONTROL)
            self._rewrite(dataset.path, manifest)
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.load_curriculum_dataset(dataset.path)

    def test_corrupted_payload_is_rejected(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CONTROL
            )
            payload = bytearray((dataset.path / "features.f32").read_bytes())
            payload[0] ^= 0xFF
            (dataset.path / "features.f32").write_bytes(bytes(payload))
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.load_curriculum_dataset(dataset.path)

    def test_edited_manifest_identity_is_rejected(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CONTROL
            )
            manifest = json.loads(
                (dataset.path / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["totals"]["row_count"] += 1
            self._rewrite(dataset.path, manifest)
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.load_curriculum_dataset(dataset.path)

    def test_pair_rejects_mismatched_source_revisions(self):
        with TemporaryDirectory() as tmp:
            control = write_curriculum_dataset(
                Path(tmp) / "arm-y", CurriculumArm.CONTROL
            )
            curriculum = write_curriculum_dataset(
                Path(tmp) / "arm-f",
                CurriculumArm.CURRICULUM,
                provenance_document={
                    **FIXTURE_PROVENANCE,
                    "lisjong_revision": "d" * 40,
                },
            )
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.require_dataset_pair(control, curriculum)

    def test_pair_rejects_swapped_arms(self):
        with TemporaryDirectory() as tmp:
            control, curriculum = write_dataset_pair(Path(tmp))
            with self.assertRaises(OfflineQArtifactError):
                fh_curriculum_dataset.require_dataset_pair(curriculum, control)

    def test_family_counts_must_partition_the_decisions(self):
        with TemporaryDirectory() as tmp:
            writer = fh_curriculum_dataset.FiniteHorizonCurriculumDatasetWriter(
                Path(tmp) / "dataset",
                arm=CurriculumArm.CONTROL,
                provenance=FIXTURE_PROVENANCE,
            )
            try:
                with self.assertRaises(OfflineQArtifactError):
                    writer.add_game(
                        seed=465,
                        split=Split.TRAIN,
                        scores=(25_000,) * 4,
                        ranks=(1, 2, 3, 4),
                        decision_count=9,
                        teacher_action_family_counts={"discard": 6},
                        rows=(),
                    )
            finally:
                writer.discard()

    @staticmethod
    def _rewrite(path: Path, manifest: dict) -> None:
        from lisjong_arena._artifact_io import canonical_json_text

        (path / "manifest.json").write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )


class SupportBindingTests(unittest.TestCase):
    def test_support_is_derived_from_the_arms_own_train_split(self):
        with TemporaryDirectory() as tmp:
            control, curriculum = write_dataset_pair(Path(tmp))
            control_support = fh_curriculum_candidate.derive_train_support(control)
            curriculum_support = fh_curriculum_candidate.derive_train_support(
                curriculum
            )
            self.assertEqual(
                control_support, tuple(sorted(ARM_LEGAL_INDICES[CurriculumArm.CONTROL]))
            )
            self.assertEqual(
                curriculum_support,
                tuple(sorted(ARM_LEGAL_INDICES[CurriculumArm.CURRICULUM])),
            )
            self.assertNotEqual(control_support, curriculum_support)

    def test_support_block_persists_indices_digest_size_and_coverage(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CURRICULUM
            )
            block = fh_curriculum_candidate.support_block(dataset)
            indices = fh_curriculum_candidate.derive_train_support(dataset)
            self.assertEqual(block["supported_indices"], list(indices))
            self.assertEqual(
                block["supported_indices_digest"], support_set_identity(indices)
            )
            self.assertEqual(block["support_size"], len(indices))
            self.assertEqual(
                set(block["support_coverage"]),
                {
                    "train_row_count",
                    "validation_row_count",
                    "train_support_complete_rate",
                    "validation_support_complete_rate",
                    "combined_support_complete_rate",
                    "unsupported_index_count",
                },
            )

    def test_cross_arm_support_substitution_is_rejected(self):
        with TemporaryDirectory() as tmp:
            control, curriculum = write_dataset_pair(Path(tmp))
            other = fh_curriculum_candidate.derive_train_support(curriculum)
            with self.assertRaises(fh_curriculum_candidate.CurriculumCandidateError):
                fh_curriculum_candidate.require_own_train_support(control, other)

    def test_historical_support_substitution_is_rejected(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CONTROL
            )
            derived = fh_curriculum_candidate.derive_train_support(dataset)
            self.assertNotEqual(
                support_set_identity(derived),
                LOCKED_P1_CANDIDATE.support_set_digest,
            )
            with self.assertRaises(fh_curriculum_candidate.CurriculumCandidateError):
                fh_curriculum_candidate.require_own_train_support(dataset, (0, 1, 2))


class LockedTrainingSemanticsTests(unittest.TestCase):
    def test_training_block_is_identical_to_the_locked_158_block(self):
        verify_locked_q_protocol_delta()
        self.assertEqual(p1_training_block(), locked_training_block())
        fh_curriculum_candidate.verify_locked_training_contract()

    def test_epoch_budget_is_the_offline_q_one_and_not_the_157_study(self):
        from lisjong_arena.stage3_epoch_budget.protocol import (
            BASELINE_MAX_EPOCHS,
            BUDGET_MAX_EPOCHS,
        )

        self.assertEqual(MAXIMUM_EPOCHS, stage2.MAXIMUM_EPOCHS)
        self.assertEqual(p1_training_block()["maximum_epochs"], MAXIMUM_EPOCHS)
        self.assertNotIn(MAXIMUM_EPOCHS, (BASELINE_MAX_EPOCHS, BUDGET_MAX_EPOCHS))
        self.assertEqual(fh_curriculum_candidate.SELECTED_EPOCH, MAXIMUM_EPOCHS)
        self.assertEqual(
            p1_training_block()["checkpoint_selection"], "fixed_final_iteration"
        )

    def test_curriculum_modules_do_not_import_the_epoch_budget_study(self):
        root = Path(fh_curriculum.__file__).parent
        for path in sorted(root.glob("fh_curriculum*.py")):
            self.assertNotIn("stage3_epoch_budget", path.read_text(encoding="utf-8"))

    def test_training_entry_point_has_no_result_driven_override(self):
        signature = inspect.signature(fh_curriculum_candidate.train_arm_candidate)
        self.assertEqual(list(signature.parameters), ["dataset"])
        for name in ("epoch", "seed", "learning_rate", "patience"):
            self.assertNotIn(name, str(signature))

    def test_both_arms_share_the_same_training_seeds(self):
        block = p1_training_block()
        self.assertEqual(block["training_seed"], stage2.TRAINING_SEED)
        self.assertEqual(block["dataloader_seed"], stage2.DATALOADER_SEED)
        self.assertEqual(block["dataloader_workers"], stage2.DATALOADER_WORKERS)
        self.assertEqual(block["torch_threads"], stage2.TORCH_THREADS)
        self.assertIs(block["deterministic_algorithms"], True)


class ServingSemanticsTests(unittest.TestCase):
    def test_fallback_is_yakuhai_call_for_both_arms(self):
        block = fh_curriculum_lock.serving_semantics_block()
        self.assertEqual(block["fallback_policy"], fallback_policy_block())
        self.assertEqual(block["fallback_policy"]["identity"], "yakuhai-call")
        self.assertIs(block["same_across_arms"], True)
        self.assertIs(block["curriculum_arm_fallback_is_finite_horizon"], False)

    def test_curriculum_arm_fallback_is_not_finite_horizon(self):
        self.assertNotEqual(
            fallback_policy_block()["policy_class"],
            FiniteHorizonCompletionPolicy.__name__,
        )
        self.assertIs(
            POLICY_CATALOG[fallback_policy_block()["identity"]].factory,
            create_yakuhai_call,
        )


class RolloutProtocolTests(unittest.TestCase):
    def test_plan_block_records_the_locked_abbb_protocol(self):
        block = fh_curriculum.rollout_plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(497, 522)))
        self.assertEqual(block["seed_block_count"], 25)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], 1)
        self.assertEqual(block["candidate_arm"], "F")
        self.assertEqual(block["baseline_arm"], "Y")
        self.assertEqual(block["seat_rotation"], "ABBB")
        self.assertIs(block["formal_test"], False)

    def test_artifact_requires_the_exact_membership(self):
        curriculum = fixture_candidate(CurriculumArm.CURRICULUM)
        control = fixture_candidate(CurriculumArm.CONTROL)
        with TemporaryDirectory() as tmp:
            artifact = save_rollout_artifact(
                Path(tmp) / "rollout.json",
                rollout_game_results(),
                candidate_identity=curriculum.candidate_identity,
                baseline_identity=control.candidate_identity,
            )
            fh_curriculum_rollout.require_rollout_artifact(
                artifact,
                candidate_identity=curriculum.candidate_identity,
                baseline_identity=control.candidate_identity,
            )
            self.assertEqual(len(artifact.game_results), 100)
            seats = {}
            for game in artifact.game_results:
                seats[int(game.candidate_seat)] = (
                    seats.get(int(game.candidate_seat), 0) + 1
                )
            self.assertEqual(seats, {0: 25, 1: 25, 2: 25, 3: 25})

    def test_partial_run_is_rejected(self):
        curriculum = fixture_candidate(CurriculumArm.CURRICULUM)
        control = fixture_candidate(CurriculumArm.CONTROL)
        with TemporaryDirectory() as tmp:
            artifact = save_rollout_artifact(
                Path(tmp) / "rollout.json",
                rollout_game_results(seeds=tuple(range(497, 521))),
                candidate_identity=curriculum.candidate_identity,
                baseline_identity=control.candidate_identity,
            )
            with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                fh_curriculum_rollout.require_rollout_artifact(
                    artifact,
                    candidate_identity=curriculum.candidate_identity,
                    baseline_identity=control.candidate_identity,
                )

    def test_duplicate_game_is_rejected(self):
        results = rollout_game_results()
        duplicated = results[:4] + results[:4] + results[8:]
        self.assertEqual(len(duplicated), 100)
        with self.assertRaises(ValueError):
            _artifact_from_results(duplicated)

    def test_wrong_rotation_is_rejected(self):
        results = list(rollout_game_results())
        results[1] = results[0]
        with self.assertRaises(ValueError):
            _artifact_from_results(tuple(results))

    def test_wrong_arm_identities_are_rejected(self):
        curriculum = fixture_candidate(CurriculumArm.CURRICULUM)
        control = fixture_candidate(CurriculumArm.CONTROL)
        with TemporaryDirectory() as tmp:
            artifact = save_rollout_artifact(
                Path(tmp) / "rollout.json",
                rollout_game_results(),
                candidate_identity=control.candidate_identity,
                baseline_identity=curriculum.candidate_identity,
            )
            with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                fh_curriculum_rollout.require_rollout_artifact(
                    artifact,
                    candidate_identity=curriculum.candidate_identity,
                    baseline_identity=control.candidate_identity,
                )


def _artifact_from_results(results):
    """raw resultsから`SingleRoundEvaluationResult`を作る（validation経路のため）。"""
    from _learned_policy_offline_q_fh_curriculum_fixtures import (
        rollout_evaluation_result,
    )

    return rollout_evaluation_result(
        results, candidate_identity="x", baseline_identity="y"
    )


class RolloutResultTests(unittest.TestCase):
    def test_valid_result_is_accepted_and_classified(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            fh_curriculum_rollout.validate_rollout_result(document)
            self.assertIsNone(document["classification"])
            self.assertIs(
                fh_curriculum_rollout.derive_classification(document),
                CurriculumOutcome.ROLLOUT_SIGNAL,
            )

    def test_classification_boundaries(self):
        for delta, expected in (
            (positive_delta, CurriculumOutcome.ROLLOUT_SIGNAL),
            (negative_delta, CurriculumOutcome.ROLLOUT_NEGATIVE),
            (inconclusive_delta, CurriculumOutcome.ROLLOUT_INCONCLUSIVE),
        ):
            with TemporaryDirectory() as tmp:
                document = rollout_result_document(
                    Path(tmp) / "rollout.json", scaled_delta_for_seed=delta
                )
                self.assertIs(
                    fh_curriculum_rollout.derive_classification(document), expected
                )

    def test_classify_interval_rule(self):
        self.assertIs(
            fh_curriculum_rollout.classify_interval(1.0, 2.0),
            CurriculumOutcome.ROLLOUT_SIGNAL,
        )
        self.assertIs(
            fh_curriculum_rollout.classify_interval(-2.0, -1.0),
            CurriculumOutcome.ROLLOUT_NEGATIVE,
        )
        self.assertIs(
            fh_curriculum_rollout.classify_interval(-1.0, 1.0),
            CurriculumOutcome.ROLLOUT_INCONCLUSIVE,
        )
        self.assertIs(
            fh_curriculum_rollout.classify_interval(0.0, 1.0),
            CurriculumOutcome.ROLLOUT_INCONCLUSIVE,
        )

    def test_diagnostics_cannot_alter_the_classification(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(
                Path(tmp) / "rollout.json",
                scaled_delta_for_seed=negative_delta,
                offline_diagnostics={"curriculum": {"looks": "better"}},
            )
            self.assertIs(
                fh_curriculum_rollout.derive_classification(document),
                CurriculumOutcome.ROLLOUT_NEGATIVE,
            )
            source = Path(fh_curriculum_rollout.__file__).read_text(encoding="utf-8")
            body = source.split("def derive_classification(")[1].split("\ndef ")[0]
            for name in (
                "secondary_diagnostics",
                "serving_diagnostics",
                "offline_diagnostics",
            ):
                self.assertNotIn(name, body)

    def test_edited_result_identity_is_rejected(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            tampered = {
                **document,
                "canonical_summary": {
                    **document["canonical_summary"],
                    "mean_candidate_game_delta": 12345.0,
                },
            }
            with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                fh_curriculum_rollout.validate_rollout_result(tampered)

    def test_candidate_identity_binds_the_checkpoint_bytes(self):
        base = fixture_candidate(CurriculumArm.CURRICULUM)
        other = fixture_candidate(CurriculumArm.CURRICULUM, weights_digest="9" * 64)
        self.assertNotEqual(base.candidate_identity, other.candidate_identity)
        self.assertNotEqual(
            base.manifest["candidate_binding"], other.manifest["candidate_binding"]
        )

    def test_result_requires_two_distinct_arm_candidates(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            tampered = {
                **document,
                "control_candidate": document["curriculum_candidate"],
            }
            with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                fh_curriculum_rollout.validate_rollout_result(tampered)

    def test_secondary_diagnostics_cover_both_candidates(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            secondary = document["secondary_diagnostics"]
            self.assertEqual(secondary["curriculum"]["round_count"], 100)
            self.assertEqual(secondary["control"]["round_count"], 300)
            for arm in ("curriculum", "control"):
                for name in (
                    "win_count",
                    "win_rate",
                    "mean_win_points",
                    "tenpai_reached_count",
                    "mean_first_tenpai_turn",
                    "exhaustive_draw_count",
                    "exhaustive_draw_tenpai_rate",
                    "deal_in_count",
                    "deal_in_rate",
                    "mean_deal_in_loss",
                    "mean_round_score_delta",
                ):
                    self.assertIn(name, secondary[arm])

    def test_serving_diagnostics_are_fail_closed_at_zero(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            for arm in ("curriculum", "control"):
                block = document["serving_diagnostics"][arm]
                self.assertEqual(block["illegal_selection_count"], 0)
                self.assertEqual(block["non_finite_model_output_count"], 0)
                self.assertEqual(block["resolve_failure_count"], 0)
                self.assertIs(block["fail_closed_at_decision_time"], True)

    def test_pre_result_outcomes_are_not_recordable(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(Path(tmp) / "rollout.json")
            for outcome in (
                CurriculumOutcome.EVIDENCE_BLOCKED,
                CurriculumOutcome.STOP_INVALID,
            ):
                with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                    fh_curriculum_rollout.record_classification(
                        document,
                        outcome,
                        curriculum=fixture_candidate(CurriculumArm.CURRICULUM),
                        control=fixture_candidate(CurriculumArm.CONTROL),
                    )

    def test_mismatched_outcome_is_rejected_before_binding(self):
        with TemporaryDirectory() as tmp:
            document = rollout_result_document(
                Path(tmp) / "rollout.json", scaled_delta_for_seed=positive_delta
            )
            with self.assertRaises(fh_curriculum_rollout.CurriculumRolloutError):
                fh_curriculum_rollout.record_classification(
                    document,
                    CurriculumOutcome.ROLLOUT_NEGATIVE,
                    curriculum=fixture_candidate(CurriculumArm.CURRICULUM),
                    control=fixture_candidate(CurriculumArm.CONTROL),
                )


class OfflineDiagnosticsTests(unittest.TestCase):
    def test_split_diagnostics_report_rows_decisions_and_rewards(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CURRICULUM
            )
            document = fh_curriculum_diagnostics.split_diagnostics(dataset)
            self.assertEqual(set(document), {"TRAIN", "VALIDATION", "TEST"})
            train = document["TRAIN"]
            self.assertEqual(train["hanchan_count"], 20)
            self.assertGreater(train["transition_rows"], 0)
            self.assertGreater(train["terminal_rows"], 0)
            self.assertGreater(train["discard_decisions"], 0)
            self.assertIn("riichi_decisions", train)
            self.assertIn("call_decisions", train)
            self.assertIn("winning_decisions", train)
            self.assertEqual(
                train["reward_distribution"]["count"], train["transition_rows"]
            )

    def test_teacher_decision_block_folds_families_into_groups(self):
        block = fh_curriculum_diagnostics.teacher_decision_block(
            {"discard": 5, "riichi": 1, "chi": 2, "pon": 1, "ron": 1, "tsumo": 2}
        )
        self.assertEqual(block["discard_decisions"], 5)
        self.assertEqual(block["riichi_decisions"], 1)
        self.assertEqual(block["call_decisions"], 3)
        self.assertEqual(block["winning_decisions"], 3)

    def test_train_support_diagnostics_bind_the_own_train_support(self):
        with TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset", CurriculumArm.CONTROL
            )
            document = fh_curriculum_diagnostics.train_support_diagnostics(dataset)
            self.assertEqual(
                document["support_digest"],
                support_set_identity(
                    fh_curriculum_candidate.derive_train_support(dataset)
                ),
            )
            self.assertEqual(
                document["derived_from"], "own TRAIN split behavior actions only"
            )


class PreExecutionLockTests(unittest.TestCase):
    def _lock(self, **overrides):
        from lisjong_arena.single_round_artifact import execution_provenance_to_dict

        locations = fh_curriculum_lock.CurriculumArtifactLocations(
            dataset_control="/srv/165/dataset-arm-y",
            dataset_curriculum="/srv/165/dataset-arm-f",
            candidate_control="/srv/165/candidate-arm-y",
            candidate_curriculum="/srv/165/candidate-arm-f",
            result_artifact="/srv/165/rollout.json",
        )
        return fh_curriculum_lock.build_pre_execution_lock(
            locations=locations,
            provenance=execution_provenance_to_dict(provenance()),
            runtime={
                "python_version": "3.14.0",
                "torch_version": "2.13.0+cpu",
                "riichienv_version": "0.4.8",
                "platform": "Linux-test",
                "device": "cpu",
                "torch_threads": 1,
                "deterministic_algorithms": True,
                "free_threaded": False,
                **overrides,
            },
        )

    def test_lock_binds_every_required_condition(self):
        lock = self._lock()
        fh_curriculum_lock.validate_pre_execution_lock(lock)
        self.assertIs(lock["result_exposed"], False)
        self.assertEqual(lock["arms"]["F"]["teacher"]["identity"], "finite-horizon")
        self.assertEqual(lock["arms"]["Y"]["teacher"]["identity"], "yakuhai-call")
        self.assertEqual(lock["dataset_protocol"]["ordered_seeds"][0], 465)
        self.assertEqual(lock["rollout_plan"]["ordered_seeds"][-1], 521)
        self.assertEqual(
            lock["derived_feature"]["schema_fingerprint"], LOCKED_P1_SCHEMA_FINGERPRINT
        )
        self.assertEqual(lock["model"]["parameter_count"], P1_EXPECTED_PARAMETER_COUNT)
        self.assertEqual(lock["training"], p1_training_block())
        self.assertIs(
            lock["serving_semantics"]["curriculum_arm_fallback_is_finite_horizon"],
            False,
        )
        self.assertTrue(lock["seed_freshness"]["fresh"])
        self.assertEqual(
            lock["outcomes"],
            [outcome.value for outcome in CurriculumOutcome],
        )

    def test_lock_identity_rejects_post_hoc_edits(self):
        lock = self._lock()
        tampered = {**lock, "primary_changed_axis": "something else"}
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            fh_curriculum_lock.validate_pre_execution_lock(tampered)
        renamed = {
            **lock,
            "artifact_locations": {
                **lock["artifact_locations"],
                "result_artifact": "/srv/165/other.json",
            },
        }
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            fh_curriculum_lock.validate_pre_execution_lock(renamed)

    def test_lock_rejects_an_exposed_result(self):
        lock = self._lock()
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            fh_curriculum_lock.validate_pre_execution_lock(
                {**lock, "result_exposed": True}
            )

    def test_lock_rejects_shared_artifact_locations(self):
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            fh_curriculum_lock.CurriculumArtifactLocations(
                dataset_control="/srv/165/shared",
                dataset_curriculum="/srv/165/shared",
                candidate_control="/srv/165/candidate-arm-y",
                candidate_curriculum="/srv/165/candidate-arm-f",
                result_artifact="/srv/165/rollout.json",
            )

    def test_lock_rejects_a_non_deterministic_runtime(self):
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            self._lock(deterministic_algorithms=False)
        with self.assertRaises(fh_curriculum_lock.CurriculumLockError):
            self._lock(free_threaded=True)

    def test_rendered_comment_carries_the_locked_values(self):
        text = fh_curriculum_lock.render_pre_execution_lock(self._lock())
        for fragment in (
            "Issue #165 pre-execution lock",
            "dataset seeds          465..496",
            "rollout seeds          497..521",
            "maximum epochs         20",
            "serving fallback       yakuhai-call (both arms)",
            "teacher class        FiniteHorizonCompletionPolicy",
            "seed freshness         fresh",
        ):
            self.assertIn(fragment, text)


class PredecessorRegressionTests(unittest.TestCase):
    def test_offline_q_140_dataset_contract_is_unchanged(self):
        self.assertEqual(
            DATASET_SCHEMA_VERSION, "arena-learned-policy-offlineq-dataset-v1"
        )
        self.assertEqual(OFFLINE_Q_DATASET_SEEDS, tuple(range(245, 277)))
        self.assertEqual(stage2.TEACHER_IDENTITY, "yakuhai-call")
        self.assertEqual(stage2.TEACHER_POPULATION, "yakuhai-call x4")
        self.assertEqual(stage2.GAME_MODE, "4p-red-half")

    def test_p1_158_representation_is_unchanged(self):
        self.assertEqual(p1_schema_fingerprint(), LOCKED_P1_SCHEMA_FINGERPRINT)
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)
        self.assertEqual(P1_EXPECTED_PARAMETER_COUNT, 1_158_434)

    def test_p1_162_candidate_identities_are_unchanged(self):
        self.assertEqual(
            LOCKED_P1_CANDIDATE.canonical_model_weights_digest,
            "f8108bf1e671007f22a8b36295ff545b3198479f74d83bb48da8a45df194461f",
        )
        self.assertEqual(GATE_B_ORDERED_SEEDS, tuple(range(440, 465)))

    def test_policy_catalog_behaviour_is_unchanged(self):
        self.assertEqual(
            sorted(POLICY_CATALOG),
            [
                "combined",
                "extended-combined",
                "finite-horizon",
                "hand-value-aware",
                "two-step",
                "yakuhai-call",
            ],
        )
        for name, spec in POLICY_CATALOG.items():
            self.assertEqual(spec.identity, name)


if __name__ == "__main__":
    unittest.main()
