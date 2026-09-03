"""Learned Policy Stage 4a protocol / freeze / orchestration unit test。

実RiichiEnvもtorchも起動しない。単一game実行境界
``lisjong_arena.single_round_evaluation._run_single_game``だけを差し替えて、
locked screening population、comparator、candidate identity binding、artifact
readback、screening classificationを高速に固定する。
"""

import json
import unittest
from inspect import signature
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _learned_policy_stage4a_fixtures import (
    fake_single_game,
    retention_target,
    serving_checkpoint,
    stage4a_candidate,
)
from _single_round_artifact_fixtures import provenance

from lisjong_arena.learned_policy_stage2.protocol import (
    ORDERED_SEEDS,
    TEST_SEEDS,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)
from lisjong_arena.learned_policy_stage3.protocol import SERVING_SEEDS
from lisjong_arena.learned_policy_stage4a.__main__ import main as cli_main
from lisjong_arena.learned_policy_stage4a.candidate import (
    BUNDLE_CHECKPOINT_DIRNAME,
    CANDIDATE_PURPOSE,
    FREEZE_RECORD_FILENAME,
    FREEZE_RECORD_SCHEMA_VERSION,
    Stage4aFreeze,
    build_freeze_document,
    freeze_candidate,
    load_freeze_record,
    parse_freeze_document,
    resolve_retention_target,
    verify_freeze_binding,
    write_freeze_record,
)
from lisjong_arena.learned_policy_stage4a.errors import (
    Stage4aFreezeError,
    Stage4aProtocolError,
    Stage4aRetentionError,
    Stage4aScreeningError,
)
from lisjong_arena.learned_policy_stage4a.evaluation import (
    artifact_filename,
    baseline_spec,
    build_screening_plan,
    run_comparison,
)
from lisjong_arena.learned_policy_stage4a.protocol import (
    CANDIDATE_GENERATION_SEEDS,
    CANDIDATE_IDENTITY_PREFIX,
    GAMES_PER_COMPARATOR,
    PRIMARY_BASELINE_IDENTITY,
    ROTATIONS_PER_SEED,
    SCREENING_GAME_MODE,
    SCREENING_SEEDS,
    SECONDARY_BASELINE_IDENTITY,
    SEED_BLOCK_COUNT,
    ComparisonRole,
    ScreeningSignal,
    Stage4aOutcome,
    classify_screening_signal,
    decide_outcome,
    derive_candidate_identity,
    require_candidate_generation_seed,
    require_screening_seeds,
)
from lisjong_arena.learned_policy_stage4a.result import (
    build_screening_result,
    measurement_document,
    write_result,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundArtifactError,
    merge_single_round_artifacts,
)
from lisjong_arena.single_round_evaluation import SeedBlockStatistics


def seed_block_statistics(lower, upper) -> SeedBlockStatistics:
    """classificationだけを検証するためのinterval入りstatistics。"""
    return SeedBlockStatistics(
        seed_block_count=SEED_BLOCK_COUNT,
        mean_seed_block_delta=(lower + upper) / 2,
        sample_standard_deviation=1.0,
        standard_error=1.0,
        normal_approx_95_interval_lower=lower,
        normal_approx_95_interval_upper=upper,
        positive_seed_block_count=1,
        zero_seed_block_count=0,
        negative_seed_block_count=0,
    )


class Stage4aLockedProtocolTest(unittest.TestCase):
    def test_screening_population_is_the_locked_one(self):
        self.assertEqual(SCREENING_SEEDS, tuple(range(220, 245)))
        self.assertEqual(len(SCREENING_SEEDS), SEED_BLOCK_COUNT)
        self.assertEqual(SEED_BLOCK_COUNT, 25)
        self.assertEqual(ROTATIONS_PER_SEED, 4)
        self.assertEqual(GAMES_PER_COMPARATOR, 100)
        self.assertEqual(SCREENING_GAME_MODE, "4p-red-single")

    def test_screening_seeds_are_fresh_to_the_learned_policy_track(self):
        self.assertEqual(set(SCREENING_SEEDS) & set(ORDERED_SEEDS), set())
        self.assertEqual(set(SCREENING_SEEDS) & set(SERVING_SEEDS), set())

    def test_comparators_are_the_locked_ones(self):
        self.assertEqual(PRIMARY_BASELINE_IDENTITY, "yakuhai-call")
        self.assertEqual(SECONDARY_BASELINE_IDENTITY, "two-step")

    def test_generation_population_is_train_and_validation_only(self):
        self.assertEqual(CANDIDATE_GENERATION_SEEDS, TRAIN_SEEDS + VALIDATION_SEEDS)
        self.assertEqual(CANDIDATE_GENERATION_SEEDS, tuple(range(200, 213)))

    def test_outcomes_are_the_exhaustive_locked_set(self):
        self.assertEqual(
            {outcome.value for outcome in Stage4aOutcome},
            {
                "ADVANCE TO CONFIRMATORY STRENGTH EVIDENCE",
                "LOW-COST VALUE CANDIDATE",
                "INCONCLUSIVE",
                "DO NOT ADVANCE",
                "ARTIFACT RETENTION BLOCKED",
                "EVALUATION CONTRACT REFORMULATE",
                "STOP / INVALID",
            },
        )


class Stage4aSeedGuardTest(unittest.TestCase):
    def test_generation_rejects_every_stage2_test_hanchan(self):
        for seed in TEST_SEEDS:
            with self.assertRaises(Stage4aProtocolError) as caught:
                require_candidate_generation_seed(seed)
            self.assertIn("TEST", str(caught.exception))

    def test_generation_rejects_every_stage3_serving_seed(self):
        for seed in SERVING_SEEDS:
            with self.assertRaises(Stage4aProtocolError) as caught:
                require_candidate_generation_seed(seed)
            self.assertIn("Stage 3", str(caught.exception))

    def test_generation_accepts_train_and_validation_only(self):
        for seed in CANDIDATE_GENERATION_SEEDS:
            self.assertEqual(require_candidate_generation_seed(seed), seed)
        for seed in (199, 220, 244):
            with self.assertRaises(Stage4aProtocolError):
                require_candidate_generation_seed(seed)

    def test_screening_seeds_cannot_be_extended_or_substituted(self):
        self.assertEqual(require_screening_seeds(SCREENING_SEEDS), SCREENING_SEEDS)
        for seeds in (
            SCREENING_SEEDS + (245,),
            SCREENING_SEEDS[:-1],
            tuple(reversed(SCREENING_SEEDS)),
            tuple(range(245, 270)),
        ):
            with self.assertRaises(Stage4aProtocolError):
                require_screening_seeds(seeds)


class Stage4aCandidateIdentityTest(unittest.TestCase):
    def test_identity_is_mechanically_derived_from_the_checkpoint_identity(self):
        identity = "a1" * 32
        self.assertEqual(
            derive_candidate_identity(identity),
            f"{CANDIDATE_IDENTITY_PREFIX}{identity}",
        )

    def test_a_free_form_alias_is_rejected(self):
        for value in ("learned", "", "LEARNED-STAGE4A", "a1" * 31, "z" * 64):
            with self.assertRaises(Stage4aProtocolError):
                derive_candidate_identity(value)

    def test_a_different_checkpoint_yields_a_different_candidate_identity(self):
        first = serving_checkpoint()
        second = serving_checkpoint(dataset_identity="e" * 64)
        self.assertNotEqual(first.identity, second.identity)
        self.assertNotEqual(
            derive_candidate_identity(first.identity),
            derive_candidate_identity(second.identity),
        )


class Stage4aRetentionTargetTest(unittest.TestCase):
    def test_a_temporary_directory_is_not_a_retention_location(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(Stage4aRetentionError) as caught:
                resolve_retention_target(
                    backend="operator-declared-store",
                    root=directory,
                    key="stage4a/run-1",
                )
            self.assertIn("temporary", str(caught.exception))

    def test_a_git_work_tree_is_not_a_retention_location(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            (root / ".git").mkdir(parents=True)
            with mock.patch(
                "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots",
                return_value=(),
            ):
                with self.assertRaises(Stage4aRetentionError) as caught:
                    resolve_retention_target(
                        backend="operator-declared-store",
                        root=root,
                        key="stage4a/run-1",
                    )
        self.assertIn("Git work tree", str(caught.exception))

    def test_an_unprovisioned_or_relative_root_is_rejected(self):
        with mock.patch(
            "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots",
            return_value=(),
        ):
            for root in ("relative/path", "/nonexistent-stage4a-retention-root"):
                with self.assertRaises(Stage4aRetentionError):
                    resolve_retention_target(
                        backend="operator-declared-store",
                        root=root,
                        key="stage4a/run-1",
                    )

    def test_an_implicit_backend_or_key_is_rejected(self):
        with TemporaryDirectory() as directory:
            with mock.patch(
                "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots",
                return_value=(),
            ):
                for backend, key in (
                    ("", "stage4a/run-1"),
                    ("   ", "stage4a/run-1"),
                    ("operator-declared-store", ""),
                    ("operator-declared-store", "/absolute"),
                    ("operator-declared-store", "stage4a/../escape"),
                    ("operator-declared-store", "stage4a/."),
                ):
                    with self.assertRaises(Stage4aRetentionError):
                        resolve_retention_target(
                            backend=backend, root=directory, key=key
                        )

    def test_an_existing_destination_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stage4a" / "run-1").mkdir(parents=True)
            with mock.patch(
                "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots",
                return_value=(),
            ):
                with self.assertRaises(Stage4aRetentionError) as caught:
                    resolve_retention_target(
                        backend="operator-declared-store",
                        root=root,
                        key="stage4a/run-1",
                    )
        self.assertIn("already exists", str(caught.exception))

    def test_gate_0_refuses_to_generate_into_an_existing_bundle(self):
        """generationより前にdestinationをfail closedし、上書きを試みない。"""
        with TemporaryDirectory() as directory:
            target = retention_target(Path(directory), key="run-1")
            target.bundle_path.mkdir(parents=True)
            with mock.patch(
                "lisjong_arena.learned_policy_stage4a.candidate"
                ".build_fixture_checkpoint",
                side_effect=AssertionError("generation must not start"),
            ):
                with self.assertRaises(Stage4aRetentionError):
                    freeze_candidate(target)

    def test_the_cli_reports_a_blocked_retention_as_the_locked_outcome(self):
        with TemporaryDirectory() as directory:
            with mock.patch("sys.stdout", new=StringIO()) as stdout:
                with mock.patch("sys.stderr", new=StringIO()):
                    status = cli_main(
                        [
                            "freeze",
                            "--retention-backend",
                            "session-workspace",
                            "--retention-root",
                            directory,
                            "--retention-key",
                            "learned-stage4a/screen-1",
                        ]
                    )
        self.assertEqual(status, 1)
        self.assertIn(
            Stage4aOutcome.ARTIFACT_RETENTION_BLOCKED.value, stdout.getvalue()
        )

    def test_a_declared_non_ephemeral_root_resolves(self):
        with TemporaryDirectory() as directory:
            with mock.patch(
                "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots",
                return_value=(),
            ):
                target = resolve_retention_target(
                    backend="operator-declared-store",
                    root=directory,
                    key="stage4a/run-1",
                )
            self.assertEqual(target.backend, "operator-declared-store")
            self.assertEqual(target.key, "stage4a/run-1")
            self.assertEqual(target.checkpoint_path.name, BUNDLE_CHECKPOINT_DIRNAME)
            self.assertEqual(target.freeze_record_path.name, FREEZE_RECORD_FILENAME)
            # machine-localなabsolute rootはlogical referenceへ出さない。
            self.assertEqual(
                set(target.to_document()),
                {"backend", "key", "checkpoint_relative_path"},
            )
            self.assertNotIn(directory, json.dumps(target.to_document()))


class Stage4aFreezeRecordTest(unittest.TestCase):
    def setUp(self):
        self.checkpoint = serving_checkpoint()
        self.document = build_freeze_document(
            self.checkpoint, target=retention_target(Path("/declared/root"))
        )

    def test_a_valid_freeze_record_binds_the_checkpoint(self):
        freeze = parse_freeze_document(self.document)
        self.assertEqual(freeze.document["purpose"], CANDIDATE_PURPOSE)
        self.assertEqual(freeze.retention_key, "stage4a/run-1")
        verify_freeze_binding(freeze, self.checkpoint)

    def test_the_record_carries_the_required_frozen_identity(self):
        self.assertEqual(
            self.document["freeze_record_schema_version"], FREEZE_RECORD_SCHEMA_VERSION
        )
        self.assertEqual(self.document["purpose"], CANDIDATE_PURPOSE)
        self.assertIsNone(self.document["strength_claim"])
        checkpoint = self.document["checkpoint"]
        self.assertEqual(checkpoint["identity"], self.checkpoint.identity)
        self.assertEqual(checkpoint["weights_sha256"], self.checkpoint.weights_sha256)
        self.assertEqual(
            checkpoint["dataset_identity"], self.checkpoint.dataset_identity
        )
        self.assertEqual(
            self.document["candidate_identity"],
            derive_candidate_identity(self.checkpoint.identity),
        )
        for name in (
            "lisjong_arena_revision",
            "lisjong_revision",
            "lisjong_engine_revision",
        ):
            self.assertIn(name, self.document["source_revisions"])

    def test_a_missing_or_unknown_field_is_rejected(self):
        for mutate in (
            lambda document: document.pop("candidate_identity"),
            lambda document: document.pop("retention"),
            lambda document: document.update(extra_field=1),
        ):
            document = json.loads(json.dumps(self.document))
            mutate(document)
            with self.assertRaises(Stage4aFreezeError):
                parse_freeze_document(document)

    def test_a_free_form_candidate_identity_is_rejected(self):
        document = json.loads(json.dumps(self.document))
        document["candidate_identity"] = "learned"
        with self.assertRaises(Stage4aFreezeError):
            parse_freeze_document(document)

        document = json.loads(json.dumps(self.document))
        document["candidate_identity"] = f"{CANDIDATE_IDENTITY_PREFIX}{'b' * 64}"
        with self.assertRaises(Stage4aFreezeError):
            parse_freeze_document(document)

    def test_a_held_out_generation_population_is_rejected(self):
        document = json.loads(json.dumps(self.document))
        document["generation"]["train_seeds"] = [*TRAIN_SEEDS, TEST_SEEDS[0]]
        with self.assertRaises(Stage4aProtocolError):
            parse_freeze_document(document)

        document = json.loads(json.dumps(self.document))
        document["generation"]["validation_seeds"] = [SERVING_SEEDS[0]]
        with self.assertRaises(Stage4aProtocolError):
            parse_freeze_document(document)

    def test_a_declared_strength_claim_is_rejected(self):
        document = json.loads(json.dumps(self.document))
        document["strength_claim"] = "stronger than yakuhai-call"
        with self.assertRaises(Stage4aFreezeError):
            parse_freeze_document(document)

    def test_a_checkpoint_or_digest_mismatch_fails_closed(self):
        freeze = parse_freeze_document(self.document)
        other = serving_checkpoint(dataset_identity="e" * 64)
        with self.assertRaises(Stage4aFreezeError):
            verify_freeze_binding(freeze, other)

        tampered = serving_checkpoint()
        tampered.manifest["weights_sha256"] = "c" * 64
        with self.assertRaises(Stage4aFreezeError):
            verify_freeze_binding(freeze, tampered)

    def test_a_missing_or_malformed_record_is_rejected(self):
        with TemporaryDirectory() as directory:
            bundle = Path(directory)
            with self.assertRaises(Stage4aFreezeError):
                load_freeze_record(bundle)
            (bundle / FREEZE_RECORD_FILENAME).write_text("{", encoding="utf-8")
            with self.assertRaises(Stage4aFreezeError):
                load_freeze_record(bundle)

    def test_an_existing_freeze_record_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            target = retention_target(Path(directory), key="run-1")
            target.bundle_path.mkdir(parents=True)
            freeze = write_freeze_record(self.checkpoint, target=target)
            self.assertIsInstance(freeze, Stage4aFreeze)
            self.assertEqual(
                load_freeze_record(target.bundle_path).document, freeze.document
            )
            with self.assertRaises(FileExistsError):
                write_freeze_record(self.checkpoint, target=target)


class Stage4aScreeningClassificationTest(unittest.TestCase):
    def test_interval_boundaries_classify_the_effect_direction(self):
        self.assertIs(
            classify_screening_signal(seed_block_statistics(0.5, 9.0)),
            ScreeningSignal.POSITIVE_SIGNAL,
        )
        self.assertIs(
            classify_screening_signal(seed_block_statistics(-9.0, -0.5)),
            ScreeningSignal.NEGATIVE_SIGNAL,
        )
        for lower, upper in ((-1.0, 1.0), (0.0, 9.0), (-9.0, 0.0), (0.0, 0.0)):
            self.assertIs(
                classify_screening_signal(seed_block_statistics(lower, upper)),
                ScreeningSignal.UNRESOLVED,
            )

    def test_an_undefined_interval_is_not_rounded_to_unresolved(self):
        statistics = SeedBlockStatistics(
            seed_block_count=1,
            mean_seed_block_delta=100.0,
            sample_standard_deviation=None,
            standard_error=None,
            normal_approx_95_interval_lower=None,
            normal_approx_95_interval_upper=None,
            positive_seed_block_count=1,
            zero_seed_block_count=0,
            negative_seed_block_count=0,
        )
        with self.assertRaises(Stage4aProtocolError):
            classify_screening_signal(statistics)

    def test_outcome_mapping_is_exhaustive(self):
        positive = ScreeningSignal.POSITIVE_SIGNAL
        negative = ScreeningSignal.NEGATIVE_SIGNAL
        unresolved = ScreeningSignal.UNRESOLVED
        advance = Stage4aOutcome.ADVANCE_TO_CONFIRMATORY_STRENGTH_EVIDENCE
        expected = {
            (positive, positive): advance,
            (positive, negative): advance,
            (positive, unresolved): advance,
            (negative, positive): Stage4aOutcome.LOW_COST_VALUE_CANDIDATE,
            (negative, negative): Stage4aOutcome.DO_NOT_ADVANCE,
            (negative, unresolved): Stage4aOutcome.INCONCLUSIVE,
            (unresolved, positive): Stage4aOutcome.INCONCLUSIVE,
            (unresolved, negative): Stage4aOutcome.INCONCLUSIVE,
            (unresolved, unresolved): Stage4aOutcome.INCONCLUSIVE,
        }
        for (primary, secondary), outcome in expected.items():
            self.assertIs(decide_outcome(primary, secondary), outcome)
        self.assertEqual(len(expected), len(ScreeningSignal) ** 2)

    def test_there_is_no_result_driven_seed_extension_entry_point(self):
        for callable_object in (build_screening_plan, run_comparison):
            self.assertNotIn("seeds", signature(callable_object).parameters)


class Stage4aAbbbIntegrationTest(unittest.TestCase):
    """existing ABBB semanticsを再実装せず、そこへ正しく接続できているかを見る。"""

    def setUp(self):
        self.checkpoint = serving_checkpoint()
        self.freeze = parse_freeze_document(
            build_freeze_document(
                self.checkpoint, target=retention_target(Path("/declared/root"))
            )
        )
        self.candidate, self.factory = stage4a_candidate(self.freeze, self.checkpoint)

    def _run(self, role, path):
        with (
            mock.patch(
                "lisjong_arena.single_round_evaluation._run_single_game",
                fake_single_game,
            ),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
        ):
            return run_comparison(self.candidate, role, path)

    def test_the_plan_uses_the_locked_seeds_and_comparators(self):
        primary = build_screening_plan(self.candidate, ComparisonRole.PRIMARY)
        secondary = build_screening_plan(self.candidate, ComparisonRole.SECONDARY)
        self.assertEqual(primary.seeds, SCREENING_SEEDS)
        self.assertEqual(secondary.seeds, SCREENING_SEEDS)
        self.assertEqual(primary.baseline.identity, PRIMARY_BASELINE_IDENTITY)
        self.assertEqual(secondary.baseline.identity, SECONDARY_BASELINE_IDENTITY)
        self.assertEqual(primary.candidate.identity, self.freeze.candidate_identity)

    def test_the_comparators_resolve_from_the_curated_catalog(self):
        self.assertEqual(
            baseline_spec(ComparisonRole.PRIMARY).identity, PRIMARY_BASELINE_IDENTITY
        )
        self.assertEqual(
            baseline_spec(ComparisonRole.SECONDARY).identity,
            SECONDARY_BASELINE_IDENTITY,
        )

    def test_artifact_filenames_name_the_role_and_baseline(self):
        self.assertEqual(
            artifact_filename(ComparisonRole.PRIMARY), "primary-yakuhai-call.json"
        )
        self.assertEqual(
            artifact_filename(ComparisonRole.SECONDARY), "secondary-two-step.json"
        )

    def test_a_comparison_produces_a_readback_verified_artifact(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / artifact_filename(ComparisonRole.PRIMARY)
            measurement = self._run(ComparisonRole.PRIMARY, path)

        self.assertEqual(measurement.artifact.plan.seeds, SCREENING_SEEDS)
        self.assertEqual(measurement.artifact.plan.rotation_count, ROTATIONS_PER_SEED)
        self.assertEqual(measurement.artifact.plan.game_mode, SCREENING_GAME_MODE)
        self.assertEqual(len(measurement.artifact.game_results), GAMES_PER_COMPARATOR)
        self.assertEqual(
            measurement.artifact.plan.candidate_identity,
            derive_candidate_identity(self.checkpoint.identity),
        )
        self.assertEqual(
            measurement.artifact.plan.baseline_identity, PRIMARY_BASELINE_IDENTITY
        )
        # canonical summaryはraw resultsから再生成した値であり、保存値と一致する。
        self.assertEqual(measurement.summary, measurement.artifact.summary)
        self.assertEqual(
            measurement.summary.seed_block_statistics.seed_block_count,
            SEED_BLOCK_COUNT,
        )
        # candidateは各seatをちょうど25回担当する。
        self.assertEqual(len(self.factory.instances), GAMES_PER_COMPARATOR)
        self.assertEqual(
            len({id(instance) for instance in self.factory.instances}),
            GAMES_PER_COMPARATOR,
        )

    def test_an_artifact_whose_candidate_identity_drifts_fails_closed(self):
        drifted, _ = stage4a_candidate(
            self.freeze, serving_checkpoint(dataset_identity="e" * 64)
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "drifted.json"
            with self.assertRaises(Stage4aScreeningError):
                with (
                    mock.patch(
                        "lisjong_arena.single_round_evaluation._run_single_game",
                        fake_single_game,
                    ),
                    mock.patch(
                        "lisjong_arena.single_round_artifact"
                        ".collect_execution_provenance",
                        return_value=provenance(),
                    ),
                ):
                    run_comparison(drifted, ComparisonRole.PRIMARY, path)

    def test_artifacts_with_different_baselines_are_never_combined(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary = self._run(
                ComparisonRole.PRIMARY, root / artifact_filename(ComparisonRole.PRIMARY)
            )
            secondary = self._run(
                ComparisonRole.SECONDARY,
                root / artifact_filename(ComparisonRole.SECONDARY),
            )
            with self.assertRaises(SingleRoundArtifactError):
                merge_single_round_artifacts([primary.artifact, secondary.artifact])

            result = build_screening_result(
                self.freeze, primary, secondary, candidate_load_cost={"a": 1}
            )
            document = result.to_document()
            self.assertIsNone(document["cumulative_combination"])
            self.assertIsNone(document["promotion_claim"])
            self.assertEqual(len(document["comparisons"]), 2)
            self.assertEqual(document["execution_mode"], "serial")
            self.assertEqual(document["ordered_seeds"], list(SCREENING_SEEDS))
            self.assertEqual(document["outcome"], result.outcome.value)

            write_result(root / "stage4a-result.json", result)
            with self.assertRaises(FileExistsError):
                write_result(root / "stage4a-result.json", result)

    def test_candidate_only_metrics_are_not_reported_as_baseline_deltas(self):
        with TemporaryDirectory() as directory:
            measurement = self._run(
                ComparisonRole.PRIMARY, Path(directory) / "primary.json"
            )
        document = measurement_document(measurement)
        self.assertIn("candidate_only_mahjong_metrics", document)
        self.assertEqual(
            set(document["strength"]) & set(document["candidate_only_mahjong_metrics"]),
            set(),
        )
        self.assertEqual(
            set(document["runtime_cost"]), {"wall_clock_seconds", "cpu_seconds"}
        )


if __name__ == "__main__":
    unittest.main()
