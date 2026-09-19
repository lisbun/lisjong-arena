"""Issue #250 Overall bundle lock / result artifact / strict verification tests.

CIでactual 400-hanchan formal evaluationも実RiichiEnvの半荘も実行しない。
comparison結果はsyntheticに合成し、execution境界は差し替える。
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _overall_champion_aabb_fixtures import (
    ARENA_REVISION,
    CHECKPOINT_DIGEST,
    CHECKPOINT_IDENTITY,
    HEURISTIC_ADVANTAGE,
    LEARNING_ADVANTAGE,
    LISJONG_REVISION,
    TIE,
    alternating_profile,
    clear_checkpoint,
    comparison_provenance,
    comparison_result,
    destinations,
    heuristic_binding,
    heuristic_spec,
    install_checkpoint,
    learning_binding,
    learning_spec,
    lock_document,
    lock_provenance,
    merged_main_execution,
    other_learning_policy,
    save_synthetic_comparison,
    seeds,
    uniform_profile,
)

import lisjong_arena.overall_champion_aabb.lock as lock_module
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena._execution_safety import ExecutionSafetyError
from lisjong_arena.model import ComparisonPlan, PolicySpec
from lisjong_arena.overall_champion_aabb.experiment import (
    build_comparison_plan,
    run_overall_evaluation,
)
from lisjong_arena.overall_champion_aabb.lock import (
    NO_RESCUE_BOUNDARY,
    OverallChampionLockError,
    build_lock_document,
    collect_ml_runtime,
    document_identity,
    load_lock_document,
    locked_participants,
    locked_seeds,
    parse_lock_document,
    require_comparison_provenance,
    require_live_execution_target,
    require_live_ml_runtime,
    require_participant_sources,
    save_lock_document,
)
from lisjong_arena.overall_champion_aabb.protocol import (
    GAME_MODE,
    HANCHAN_COUNT,
    HEURISTIC_SUPERIOR_LABEL,
    INCONCLUSIVE_LABEL,
    LEARNING_SUPERIOR_LABEL,
    MAX_STEPS,
    PROTOCOL_ID,
    SEAT_RESULT_COUNT,
    SEED_BLOCK_COUNT,
    STOP_INVALID_LABEL,
    OverallChampionProtocolError,
    resolve_binding_callable,
)
from lisjong_arena.overall_champion_aabb.result import (
    OverallChampionResultError,
    build_overall_result,
    load_overall_result,
    save_overall_result,
    verify_overall_bundle,
)
from lisjong_arena.overall_champion_aabb.statistics import (
    OverallChampionStatisticsError,
    OverallSeedBlock,
    block_sign_counts,
    classify,
    summarize_seed_blocks,
)

SEEDS = seeds()


class _Directory:
    """TemporaryDirectory + bundle path helper。"""

    def __init__(self) -> None:
        self._temporary = TemporaryDirectory()
        self.path = Path(self._temporary.name)

    def close(self) -> None:
        self._temporary.cleanup()

    @property
    def lock(self) -> Path:
        return self.path / "overall-lock.json"

    @property
    def comparison(self) -> Path:
        return self.path / "comparison.json"

    @property
    def result(self) -> Path:
        return self.path / "overall-result.json"


class LockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)

    def test_lock_binds_participants_population_protocol_and_provenance(self) -> None:
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        loaded = load_lock_document(self.directory.lock)
        protocol = loaded["protocol"]
        self.assertEqual(protocol["protocol_id"], PROTOCOL_ID)
        self.assertEqual(protocol["game_mode"], GAME_MODE)
        self.assertEqual(protocol["seed_block_count"], SEED_BLOCK_COUNT)
        self.assertEqual(protocol["hanchan_count"], HANCHAN_COUNT)
        self.assertEqual(protocol["max_steps"], MAX_STEPS)
        self.assertEqual(protocol["ordered_seeds"], list(SEEDS))
        self.assertIs(loaded["result_exposed"], False)
        self.assertEqual(loaded["no_rescue_boundary"], list(NO_RESCUE_BOUNDARY))
        self.assertEqual(loaded["provenance"]["lisjong_arena_revision"], ARENA_REVISION)
        self.assertEqual(
            loaded["provenance"]["lisjong_engine_revision"],
            lock_provenance().lisjong_engine_revision,
        )
        heuristic, learning = locked_participants(loaded)
        self.assertEqual(heuristic.family, "heuristic")
        self.assertEqual(learning.family, "learning")
        self.assertIsNotNone(learning.checkpoint_identity)
        self.assertEqual(locked_seeds(loaded), SEEDS)

    def test_lock_is_write_once(self) -> None:
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        with self.assertRaises(OverallChampionLockError):
            save_lock_document(document, self.directory.lock)

    def test_lock_rejects_duplicate_destinations(self) -> None:
        same = self.directory.path / "same.json"
        with merged_main_execution():
            with self.assertRaises(OverallChampionLockError):
                build_lock_document(
                    destinations={
                        "comparison_artifact": same,
                        "overall_result": same,
                    },
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_lock_rejects_an_existing_destination(self) -> None:
        existing = destinations(self.directory.path)["comparison_artifact"]
        existing.write_text("{}", encoding="utf-8")
        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "write-once"):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_lock_requires_a_clean_merged_main_execution_target(self) -> None:
        live = lock_provenance()
        with (
            mock.patch.object(lock_module, "_require_environment_consistent"),
            mock.patch.object(
                lock_module, "collect_execution_provenance", return_value=live
            ),
            mock.patch.object(
                lock_module,
                "require_clean_arena_head",
                side_effect=ExecutionSafetyError("worktree must be clean"),
            ),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "clean"):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_lock_cannot_be_built_from_an_unmerged_pr_branch(self) -> None:
        live = lock_provenance()
        with (
            mock.patch.object(lock_module, "_require_environment_consistent"),
            mock.patch.object(
                lock_module, "collect_execution_provenance", return_value=live
            ),
            mock.patch.object(
                lock_module,
                "require_clean_arena_head",
                return_value=live.lisjong_arena_revision,
            ),
            mock.patch.object(
                lock_module,
                "require_merged_arena_revision",
                side_effect=ExecutionSafetyError("not contained in main"),
            ),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "main"):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_lock_rejects_invalid_participants_and_population(self) -> None:
        with merged_main_execution():
            with self.assertRaises(OverallChampionLockError):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=learning_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )
            with self.assertRaises(OverallChampionLockError):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS[:-1],
                    max_workers=1,
                )
            with self.assertRaises(OverallChampionLockError):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=0,
                )

    def test_declared_ml_runtime_must_be_installed(self) -> None:
        self.assertEqual(collect_ml_runtime(()), {})
        self.assertEqual(set(collect_ml_runtime(("lisjong-arena",))), {"lisjong-arena"})
        with self.assertRaises(OverallChampionLockError):
            collect_ml_runtime(("definitely-not-installed-package",))

    def test_tampered_lock_fields_are_rejected(self) -> None:
        document = lock_document(self.directory.path)
        tampered = copy.deepcopy(document)
        tampered["protocol"]["max_steps"] = 1
        with self.assertRaises(OverallChampionProtocolError):
            parse_lock_document(tampered)

        exposed = copy.deepcopy(document)
        exposed["result_exposed"] = True
        exposed["lock_identity"] = document_identity(
            {key: value for key, value in exposed.items() if key != "lock_identity"}
        )
        with self.assertRaisesRegex(OverallChampionLockError, "result_exposed"):
            parse_lock_document(exposed)

        identity_drift = copy.deepcopy(document)
        identity_drift["max_workers"] = 99
        with self.assertRaisesRegex(OverallChampionLockError, "identity mismatch"):
            parse_lock_document(identity_drift)

        rescue = copy.deepcopy(document)
        rescue["no_rescue_boundary"] = []
        with self.assertRaises(OverallChampionLockError):
            parse_lock_document(rescue)

    def test_bool_as_int_and_missing_or_unexpected_fields_are_rejected(self) -> None:
        document = lock_document(self.directory.path)
        bool_as_int = copy.deepcopy(document)
        bool_as_int["result_exposed"] = 0
        with self.assertRaises(OverallChampionLockError):
            parse_lock_document(bool_as_int)

        missing = {
            key: value for key, value in document.items() if key != "max_workers"
        }
        with self.assertRaises(OverallChampionLockError):
            parse_lock_document(missing)

        extra = copy.deepcopy(document)
        extra["unexpected"] = "x"
        with self.assertRaises(OverallChampionLockError):
            parse_lock_document(extra)

    def test_live_execution_target_must_match_the_lock(self) -> None:
        document = lock_document(self.directory.path)
        with merged_main_execution():
            self.assertEqual(
                require_live_execution_target(document).lisjong_arena_revision,
                ARENA_REVISION,
            )
        drifted = lock_provenance()
        drifted = type(drifted)(
            execution_environment=drifted.execution_environment,
            lisjong_arena_version=drifted.lisjong_arena_version,
            lisjong_arena_revision="9" * 40,
            lisjong_version=drifted.lisjong_version,
            lisjong_revision=drifted.lisjong_revision,
            lisjong_engine_version=drifted.lisjong_engine_version,
            lisjong_engine_revision=drifted.lisjong_engine_revision,
            riichienv_version=drifted.riichienv_version,
            python_version=drifted.python_version,
        )
        with merged_main_execution(drifted):
            with self.assertRaisesRegex(OverallChampionLockError, "differs"):
                require_live_execution_target(document)

    def test_comparison_provenance_is_cross_bound_to_the_lock(self) -> None:
        document = lock_document(self.directory.path)
        require_comparison_provenance(document, comparison_provenance())
        with self.assertRaisesRegex(OverallChampionLockError, "lisjong_revision"):
            require_comparison_provenance(
                document, comparison_provenance(lisjong_revision="9" * 40)
            )


class BundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)
        self.lock = lock_document(self.directory.path)
        save_lock_document(self.lock, self.directory.lock)

    def _write_comparison(self, profile_of, population: tuple[int, ...] = SEEDS):
        save_synthetic_comparison(
            comparison_result(population, profile_of), self.directory.comparison
        )

    def _build_and_save(self, profile_of, population: tuple[int, ...] = SEEDS):
        self._write_comparison(profile_of, population)
        from lisjong_arena.overall_champion_aabb.result import load_bundle_comparison

        document = build_overall_result(
            lock_document=self.lock,
            comparison_artifact=load_bundle_comparison(self.directory.comparison),
            comparison_artifact_path=self.directory.comparison,
        )
        save_overall_result(document, self.directory.result)
        return document

    def _verify(self):
        return verify_overall_bundle(
            lock_path=self.directory.lock,
            comparison_path=self.directory.comparison,
            result_path=self.directory.result,
        )

    def _rewrite_result(self, document: dict[str, object]) -> Path:
        self._tamper_index = getattr(self, "_tamper_index", 0) + 1
        path = self.directory.path / f"tampered-result-{self._tamper_index}.json"
        write_new_artifact_file(path, canonical_json_text(document))
        return path

    def _verify_tampered(self, document: dict[str, object]):
        return verify_overall_bundle(
            lock_path=self.directory.lock,
            comparison_path=self.directory.comparison,
            result_path=self._rewrite_result(document),
        )

    def test_bundle_round_trip_and_strict_verification(self) -> None:
        document = self._build_and_save(uniform_profile(HEURISTIC_ADVANTAGE))
        self.assertEqual(document["classification"]["label"], HEURISTIC_SUPERIOR_LABEL)
        self.assertEqual(document["lock_identity"], self.lock["lock_identity"])
        self.assertEqual(len(document["primary"]["seed_blocks"]), SEED_BLOCK_COUNT)
        self.assertEqual(
            document["primary"]["summary"]["block_count"], SEED_BLOCK_COUNT
        )
        self.assertEqual(
            document["secondary_diagnostics"]["heuristic"]["seat_result_count"],
            SEAT_RESULT_COUNT // 2,
        )
        self.assertEqual(load_overall_result(self.directory.result), document)
        self.assertEqual(self._verify(), document)

    def test_learning_and_inconclusive_bundles_classify_exactly(self) -> None:
        document = self._build_and_save(uniform_profile(LEARNING_ADVANTAGE))
        self.assertEqual(document["classification"]["label"], LEARNING_SUPERIOR_LABEL)
        self.assertEqual(
            self._verify()["classification"]["label"], LEARNING_SUPERIOR_LABEL
        )

    def test_tie_bundle_is_inconclusive(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        self.assertEqual(document["classification"]["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(document["primary"]["summary"]["interval_lower"], 0.0)
        self.assertEqual(document["primary"]["summary"]["interval_upper"], 0.0)

    def test_mixed_bundle_is_inconclusive(self) -> None:
        document = self._build_and_save(alternating_profile(SEEDS))
        self.assertEqual(document["classification"]["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(
            document["primary"]["block_sign_counts"],
            {
                "negative_block_count": 50,
                "positive_block_count": 50,
                "zero_block_count": 0,
            },
        )

    def test_bundle_verification_is_portable_across_directories(self) -> None:
        """verifyはlocked absolute pathではなくbundle内容だけを信頼する。

        destinationのwrite-once bindingはexecution boundaryの契約であり、
        formal evidenceは別directoryへcopyしてもoffline verifyできる。
        """
        self._build_and_save(uniform_profile(HEURISTIC_ADVANTAGE))
        elsewhere = self.directory.path / "archived"
        elsewhere.mkdir()
        for source in (
            self.directory.lock,
            self.directory.comparison,
            self.directory.result,
        ):
            (elsewhere / source.name).write_bytes(source.read_bytes())
        moved = verify_overall_bundle(
            lock_path=elsewhere / self.directory.lock.name,
            comparison_path=elsewhere / self.directory.comparison.name,
            result_path=elsewhere / self.directory.result.name,
        )
        self.assertEqual(moved, self._verify())
        self.assertNotEqual(
            (elsewhere / self.directory.comparison.name), self.directory.comparison
        )

    def test_verification_does_not_need_the_live_execution_environment(self) -> None:
        """offline verifyはserving環境やML runtimeの再取得を要求しない。"""
        self._build_and_save(uniform_profile(TIE))
        clear_checkpoint()
        self.addCleanup(install_checkpoint, self.directory.path)
        with mock.patch.object(
            lock_module,
            "collect_execution_provenance",
            side_effect=AssertionError("verify must stay offline"),
        ):
            self.assertEqual(self._verify()["result_version"], 1)

    def test_result_artifact_is_write_once(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        with self.assertRaisesRegex(OverallChampionResultError, "write-once"):
            save_overall_result(document, self.directory.result)

    def test_self_consistent_result_tamper_is_rejected_by_raw_rederivation(
        self,
    ) -> None:
        document = self._build_and_save(alternating_profile(SEEDS))
        payload = copy.deepcopy(document)
        payload.pop("result_identity")
        for block in payload["primary"]["seed_blocks"]:
            block["heuristic_mean_rank"] = 1.5
            block["learning_mean_rank"] = 3.5
            block["delta"] = 2.0
        blocks = tuple(
            OverallSeedBlock(
                seed=block["seed"],
                heuristic_mean_rank=block["heuristic_mean_rank"],
                learning_mean_rank=block["learning_mean_rank"],
                delta=block["delta"],
            )
            for block in payload["primary"]["seed_blocks"]
        )
        summary = summarize_seed_blocks(blocks)
        payload["primary"]["summary"] = summary.to_document()
        payload["primary"]["block_sign_counts"] = block_sign_counts(blocks)
        payload["classification"] = classify(summary)
        tampered = dict(payload)
        tampered["result_identity"] = document_identity(payload)

        # The tampered document is internally self-consistent...
        self.assertEqual(tampered["classification"]["label"], HEURISTIC_SUPERIOR_LABEL)
        self.assertEqual(
            load_overall_result(self._rewrite_result(tampered))["result_identity"],
            tampered["result_identity"],
        )
        # ...but it no longer matches the raw comparison evidence.
        with self.assertRaisesRegex(OverallChampionResultError, "re-derived"):
            self._verify_tampered(tampered)

    def test_statistics_tamper_without_recomputation_is_rejected_on_read(self) -> None:
        document = self._build_and_save(alternating_profile(SEEDS))
        tampered = copy.deepcopy(document)
        tampered["primary"]["summary"]["interval_lower"] = 1.0
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "primary summary"):
            self._verify_tampered(tampered)

    def test_classification_tamper_is_rejected_on_read(self) -> None:
        document = self._build_and_save(alternating_profile(SEEDS))
        tampered = copy.deepcopy(document)
        tampered["classification"]["label"] = HEURISTIC_SUPERIOR_LABEL
        tampered["classification"]["kind"] = "HEURISTIC_CHAMPION_SUPERIOR"
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "classification"):
            self._verify_tampered(tampered)

    def test_result_identity_tamper_is_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["result_identity"] = "0" * 64
        with self.assertRaisesRegex(OverallChampionResultError, "identity mismatch"):
            self._verify_tampered(tampered)

    def test_secondary_diagnostic_tamper_is_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["secondary_diagnostics"]["heuristic"]["average_score"] = 1.0
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "re-derived"):
            self._verify_tampered(tampered)

    def test_secondary_diagnostics_can_never_claim_to_override_primary(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["secondary_diagnostics"]["overrides_primary_classification"] = True
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "override"):
            self._verify_tampered(tampered)

    def test_comparison_digest_mismatch_is_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["comparison"]["artifact_digest"] = "0" * 64
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "re-derived"):
            self._verify_tampered(tampered)

    def test_participant_tamper_is_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["participants"]["learning"]["policy_identity"] = "someone-else"
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaises(OverallChampionResultError):
            self._verify_tampered(tampered)

    def test_seed_order_tamper_is_rejected(self) -> None:
        document = self._build_and_save(alternating_profile(SEEDS))
        tampered = copy.deepcopy(document)
        blocks = tampered["primary"]["seed_blocks"]
        blocks[0], blocks[1] = blocks[1], blocks[0]
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "ordered population"):
            self._verify_tampered(tampered)

    def test_unknown_version_and_field_shape_are_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        for mutate in (
            lambda payload: payload.__setitem__("result_version", 2),
            lambda payload: payload.pop("secondary_diagnostics"),
            lambda payload: payload.__setitem__("unexpected", 1),
        ):
            tampered = copy.deepcopy(document)
            tampered.pop("result_identity")
            mutate(tampered)
            finished = dict(tampered)
            finished["result_identity"] = document_identity(tampered)
            with self.assertRaises(OverallChampionResultError):
                self._verify_tampered(finished)

    def test_malformed_numeric_types_are_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        tampered = copy.deepcopy(document)
        tampered["primary"]["seed_blocks"][0]["delta"] = 0
        payload = {
            key: value for key, value in tampered.items() if key != "result_identity"
        }
        tampered["result_identity"] = document_identity(payload)
        with self.assertRaisesRegex(OverallChampionResultError, "decimals"):
            self._verify_tampered(tampered)

    def test_non_finite_statistics_are_rejected(self) -> None:
        document = self._build_and_save(uniform_profile(TIE))
        path = self.directory.path / "non-finite-result.json"
        text = json.dumps(document).replace('"mean_delta": 0.0', '"mean_delta": NaN')
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(OverallChampionResultError):
            verify_overall_bundle(
                lock_path=self.directory.lock,
                comparison_path=self.directory.comparison,
                result_path=path,
            )

    def test_comparison_seed_population_mismatch_is_rejected(self) -> None:
        other = seeds(start=555_000)
        self._write_comparison(uniform_profile(TIE), other)
        from lisjong_arena.overall_champion_aabb.result import load_bundle_comparison

        with self.assertRaisesRegex(OverallChampionStatisticsError, "ordered seeds"):
            build_overall_result(
                lock_document=self.lock,
                comparison_artifact=load_bundle_comparison(self.directory.comparison),
                comparison_artifact_path=self.directory.comparison,
            )

    def test_comparison_provenance_mismatch_is_rejected(self) -> None:
        save_synthetic_comparison(
            comparison_result(SEEDS, uniform_profile(TIE)),
            self.directory.comparison,
            provenance=comparison_provenance(lisjong_revision="9" * 40),
        )
        from lisjong_arena.overall_champion_aabb.result import load_bundle_comparison

        with self.assertRaisesRegex(OverallChampionLockError, "provenance"):
            build_overall_result(
                lock_document=self.lock,
                comparison_artifact=load_bundle_comparison(self.directory.comparison),
                comparison_artifact_path=self.directory.comparison,
            )

    def test_corrupt_comparison_artifact_is_rejected(self) -> None:
        self._build_and_save(uniform_profile(TIE))
        corrupt = self.directory.path / "corrupt.json"
        corrupt.write_text("{", encoding="utf-8")
        with self.assertRaises(OverallChampionResultError):
            verify_overall_bundle(
                lock_path=self.directory.lock,
                comparison_path=corrupt,
                result_path=self.directory.result,
            )


class ParticipantSourceBindingTest(unittest.TestCase):
    """implementation revision / checkpointがlive executionへ束縛されること。"""

    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)

    def _no_execution(self, plan, *, max_workers):
        raise AssertionError("execution must not start")

    def _run_with(self, lock_path: Path):
        with merged_main_execution():
            return run_overall_evaluation(
                lock_path=lock_path,
                heuristic_spec=heuristic_spec(),
                learning_spec=learning_spec(),
                execute=self._no_execution,
            )

    def test_exact_binding_permits_execution(self) -> None:
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        learning = document["participants"]["learning"]
        self.assertEqual(learning["checkpoint_identity"], CHECKPOINT_IDENTITY)
        self.assertEqual(learning["checkpoint_digest"], CHECKPOINT_DIGEST)
        self.assertEqual(learning["implementation_source"], "lisjong-arena")
        with merged_main_execution():
            require_participant_sources(
                *locked_participants(document), lock_provenance()
            )
            self.assertEqual(
                require_live_execution_target(document).lisjong_arena_revision,
                ARENA_REVISION,
            )

    def test_implementation_revision_mismatch_is_rejected_at_lock_time(self) -> None:
        with merged_main_execution():
            with self.assertRaisesRegex(
                OverallChampionLockError, "implementation revision"
            ):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(implementation_revision="9" * 40),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_implementation_revision_mismatch_rejects_before_execution(self) -> None:
        document = lock_document(self.directory.path)
        drifted = copy.deepcopy(document)
        drifted["participants"]["heuristic"]["implementation_revision"] = "9" * 40
        payload = {
            key: value for key, value in drifted.items() if key != "lock_identity"
        }
        drifted["lock_identity"] = document_identity(payload)
        path = self.directory.path / "drifted-lock.json"
        write_new_artifact_file(path, canonical_json_text(drifted))
        with self.assertRaisesRegex(
            OverallChampionLockError, "implementation revision"
        ):
            self._run_with(path)

    def test_wrong_implementation_source_is_rejected(self) -> None:
        """lisjong実装をengine revisionへbindしても通らない。"""
        with merged_main_execution():
            with self.assertRaisesRegex(
                OverallChampionLockError, "implementation revision"
            ):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(
                        implementation_source="lisjong-engine",
                        implementation_revision=LISJONG_REVISION,
                    ),
                    learning=learning_binding(),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_checkpoint_identity_mismatch_is_rejected(self) -> None:
        with merged_main_execution():
            with self.assertRaisesRegex(
                OverallChampionLockError, "checkpoint identity"
            ):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(
                        checkpoint_identity="a-different-checkpoint"
                    ),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_checkpoint_digest_mismatch_is_rejected(self) -> None:
        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "digest"):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(checkpoint_digest="0" * 64),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_served_checkpoint_drift_rejects_before_execution(self) -> None:
        """lock後にserving側のweightsが差し替わったらrunを開始しない。"""
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        install_checkpoint(self.directory.path, payload=b"replaced weights")
        with self.assertRaisesRegex(OverallChampionLockError, "digest"):
            self._run_with(self.directory.lock)

    def test_served_checkpoint_identity_drift_rejects_before_execution(self) -> None:
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        install_checkpoint(self.directory.path, identity="swapped-checkpoint")
        with self.assertRaisesRegex(OverallChampionLockError, "checkpoint identity"):
            self._run_with(self.directory.lock)

    def test_missing_served_checkpoint_rejects_before_execution(self) -> None:
        document = lock_document(self.directory.path)
        save_lock_document(document, self.directory.lock)
        path = install_checkpoint(self.directory.path)
        path.unlink()
        with self.assertRaisesRegex(OverallChampionLockError, "cannot be read"):
            self._run_with(self.directory.lock)

    def test_unresolvable_checkpoint_binding_is_rejected(self) -> None:
        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "unusable"):
                build_lock_document(
                    destinations=destinations(self.directory.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(
                        checkpoint_binding="_overall_champion_aabb_fixtures:missing"
                    ),
                    seeds=SEEDS,
                    max_workers=1,
                )

    def test_partially_declared_checkpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(OverallChampionProtocolError, "declared together"):
            learning_binding(checkpoint_digest=None)
        with self.assertRaisesRegex(OverallChampionProtocolError, "declared together"):
            learning_binding(checkpoint_binding=None)

    def test_a_participant_without_weights_needs_no_checkpoint(self) -> None:
        binding = heuristic_binding()
        self.assertFalse(binding.has_checkpoint)
        self.assertIsNone(binding.checkpoint_binding)
        with merged_main_execution():
            require_participant_sources(binding, learning_binding(), lock_provenance())

    def test_binding_resolution_rejects_an_alias_or_re_export(self) -> None:
        import _overall_champion_aabb_fixtures as fixtures

        fixtures.aliased_checkpoint = fixtures.served_learning_checkpoint
        self.addCleanup(lambda: delattr(fixtures, "aliased_checkpoint"))
        with self.assertRaisesRegex(OverallChampionProtocolError, "re-export"):
            resolve_binding_callable(
                "_overall_champion_aabb_fixtures:aliased_checkpoint"
            )


class MlRuntimeBindingTest(unittest.TestCase):
    """lockされたML runtimeがrun前に再検証されること。"""

    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)
        self.lock = lock_document(
            self.directory.path, ml_runtime_packages=("lisjong-arena",)
        )
        save_lock_document(self.lock, self.directory.lock)

    def test_locked_ml_runtime_exact_match_passes(self) -> None:
        self.assertEqual(set(self.lock["ml_runtime"]), {"lisjong-arena"})
        self.assertEqual(require_live_ml_runtime(self.lock), self.lock["ml_runtime"])
        with merged_main_execution():
            require_live_execution_target(self.lock)

    def test_version_drift_is_rejected_before_execution(self) -> None:
        live = dict(self.lock["ml_runtime"])
        live["lisjong-arena"] = "9.9.9"
        with mock.patch.object(lock_module, "collect_ml_runtime", return_value=live):
            with self.assertRaisesRegex(OverallChampionLockError, "live ML runtime"):
                require_live_ml_runtime(self.lock)
            with merged_main_execution():
                with self.assertRaisesRegex(
                    OverallChampionLockError, "live ML runtime"
                ):
                    require_live_execution_target(self.lock)

    def test_missing_package_is_rejected_before_execution(self) -> None:
        with mock.patch.object(
            lock_module,
            "collect_ml_runtime",
            side_effect=OverallChampionLockError("not installed"),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "not installed"):
                require_live_ml_runtime(self.lock)

    def test_a_removed_package_is_rejected_before_execution(self) -> None:
        with mock.patch.object(lock_module, "collect_ml_runtime", return_value={}):
            with self.assertRaisesRegex(OverallChampionLockError, "live ML runtime"):
                require_live_ml_runtime(self.lock)

    def test_ml_runtime_drift_stops_a_run_before_any_execution(self) -> None:
        def execute(plan, *, max_workers):
            raise AssertionError("execution must not start")

        live = dict(self.lock["ml_runtime"])
        live["lisjong-arena"] = "9.9.9"
        with (
            merged_main_execution(),
            mock.patch.object(lock_module, "collect_ml_runtime", return_value=live),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "live ML runtime"):
                run_overall_evaluation(
                    lock_path=self.directory.lock,
                    heuristic_spec=heuristic_spec(),
                    learning_spec=learning_spec(),
                    execute=execute,
                )


class PostExecutionVerificationTest(unittest.TestCase):
    """formal run中のparticipant / runtime driftをpost-checkで拒否すること。

    400 hanchanは長時間になり得るうえ、Policy instanceはgame / seatごとに
    factoryから生成されるため、preflightだけではformal event全体をbindできない。
    """

    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)
        self.lock = lock_document(
            self.directory.path, ml_runtime_packages=("lisjong-arena",)
        )
        save_lock_document(self.lock, self.directory.lock)
        self.result = comparison_result(SEEDS, uniform_profile(HEURISTIC_ADVANTAGE))
        self.executed = 0

    def _run(self, during_execution=None):
        def execute(plan: ComparisonPlan, *, max_workers: int):
            self.executed += 1
            if during_execution is not None:
                during_execution()
            return self.result

        with (
            merged_main_execution(),
            mock.patch(
                "lisjong_arena.artifact._collect_execution_provenance",
                return_value=comparison_provenance(),
            ),
        ):
            return run_overall_evaluation(
                lock_path=self.directory.lock,
                heuristic_spec=heuristic_spec(),
                learning_spec=learning_spec(),
                execute=execute,
            )

    def _assert_no_formal_artifact_was_written(self) -> None:
        self.assertEqual(self.executed, 1, "execution should have been attempted")
        self.assertFalse(
            self.directory.comparison.exists(),
            "a drifted formal event must not publish comparison.json",
        )
        self.assertFalse(
            self.directory.result.exists(),
            "a drifted formal event must not publish overall-result.json",
        )

    def test_checkpoint_bytes_replaced_during_execution_is_rejected(self) -> None:
        def swap() -> None:
            install_checkpoint(self.directory.path, payload=b"weights swapped mid-run")

        with self.assertRaisesRegex(OverallChampionLockError, "digest"):
            self._run(swap)
        self._assert_no_formal_artifact_was_written()

    def test_checkpoint_identity_replaced_during_execution_is_rejected(self) -> None:
        def swap() -> None:
            install_checkpoint(self.directory.path, identity="swapped-mid-run")

        with self.assertRaisesRegex(OverallChampionLockError, "checkpoint identity"):
            self._run(swap)
        self._assert_no_formal_artifact_was_written()

    def test_checkpoint_removed_during_execution_is_rejected(self) -> None:
        def remove() -> None:
            (self.directory.path / "learning-champion.weights").unlink()

        with self.assertRaisesRegex(OverallChampionLockError, "cannot be read"):
            self._run(remove)
        self._assert_no_formal_artifact_was_written()

    def test_ml_runtime_drift_during_execution_is_rejected(self) -> None:
        drifted = dict(self.lock["ml_runtime"])
        drifted["lisjong-arena"] = "9.9.9"

        def drift() -> None:
            patcher = mock.patch.object(
                lock_module, "collect_ml_runtime", return_value=drifted
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.assertRaisesRegex(OverallChampionLockError, "live ML runtime"):
            self._run(drift)
        self._assert_no_formal_artifact_was_written()

    def test_implementation_revision_drift_during_execution_is_rejected(self) -> None:
        drifted = lock_provenance()
        drifted = type(drifted)(
            execution_environment=drifted.execution_environment,
            lisjong_arena_version=drifted.lisjong_arena_version,
            lisjong_arena_revision=drifted.lisjong_arena_revision,
            lisjong_version=drifted.lisjong_version,
            lisjong_revision="9" * 40,
            lisjong_engine_version=drifted.lisjong_engine_version,
            lisjong_engine_revision=drifted.lisjong_engine_revision,
            riichienv_version=drifted.riichienv_version,
            python_version=drifted.python_version,
        )

        def drift() -> None:
            patcher = mock.patch.object(
                lock_module, "collect_execution_provenance", return_value=drifted
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.assertRaises(OverallChampionLockError):
            self._run(drift)
        self._assert_no_formal_artifact_was_written()

    def test_stable_target_keeps_the_successful_path_green(self) -> None:
        outcome = self._run()
        self.assertEqual(self.executed, 1)
        self.assertEqual(outcome.classification["label"], HEURISTIC_SUPERIOR_LABEL)
        self.assertTrue(self.directory.comparison.exists())
        self.assertTrue(self.directory.result.exists())
        self.assertEqual(
            verify_overall_bundle(
                lock_path=self.directory.lock,
                comparison_path=self.directory.comparison,
                result_path=self.directory.result,
            ),
            outcome.overall_result,
        )

    def test_the_execution_target_is_verified_before_and_after_execution(self) -> None:
        calls: list[str] = []
        real = lock_module.require_live_execution_target

        def recording(document):
            calls.append("verified")
            return real(document)

        def during() -> None:
            calls.append("executed")

        with mock.patch(
            "lisjong_arena.overall_champion_aabb.experiment"
            ".require_live_execution_target",
            recording,
        ):
            self._run(during)
        self.assertEqual(calls, ["verified", "executed", "verified"])


class OperatorSurfaceTest(unittest.TestCase):
    """invalid evidenceがoperator surfaceで``STOP / INVALID``になること。"""

    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)
        self.lock = lock_document(self.directory.path)
        save_lock_document(self.lock, self.directory.lock)
        save_synthetic_comparison(
            comparison_result(SEEDS, uniform_profile(HEURISTIC_ADVANTAGE)),
            self.directory.comparison,
        )
        from lisjong_arena.overall_champion_aabb.result import load_bundle_comparison

        self.document = build_overall_result(
            lock_document=self.lock,
            comparison_artifact=load_bundle_comparison(self.directory.comparison),
            comparison_artifact_path=self.directory.comparison,
        )
        save_overall_result(self.document, self.directory.result)

    def test_verify_reports_the_classification_for_valid_evidence(self) -> None:
        from lisjong_arena.overall_champion_aabb.__main__ import main

        with mock.patch("sys.stdout", new=io.StringIO()) as stream:
            code = main(
                [
                    "verify",
                    "--lock",
                    str(self.directory.lock),
                    "--comparison",
                    str(self.directory.comparison),
                    "--result",
                    str(self.directory.result),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn(HEURISTIC_SUPERIOR_LABEL, stream.getvalue())

    def test_verify_reports_stop_invalid_for_corrupt_evidence(self) -> None:
        from lisjong_arena.overall_champion_aabb.__main__ import main

        corrupt = self.directory.path / "corrupt.json"
        corrupt.write_text("{", encoding="utf-8")
        with mock.patch("sys.stderr", new=io.StringIO()) as stream:
            code = main(
                [
                    "verify",
                    "--lock",
                    str(self.directory.lock),
                    "--comparison",
                    str(corrupt),
                    "--result",
                    str(self.directory.result),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn(STOP_INVALID_LABEL, stream.getvalue())


class ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _Directory()
        self.addCleanup(self.directory.close)
        install_checkpoint(self.directory.path)
        self.addCleanup(clear_checkpoint)
        self.lock = lock_document(self.directory.path)
        save_lock_document(self.lock, self.directory.lock)

    def _run(self, *, profile_of=None, **overrides):
        result = comparison_result(
            SEEDS, profile_of or uniform_profile(HEURISTIC_ADVANTAGE)
        )
        calls: list[ComparisonPlan] = []

        def execute(plan: ComparisonPlan, *, max_workers: int):
            calls.append(plan)
            return result

        arguments = {
            "lock_path": self.directory.lock,
            "heuristic_spec": heuristic_spec(),
            "learning_spec": learning_spec(),
            "execute": execute,
        }
        arguments.update(overrides)
        with (
            merged_main_execution(),
            mock.patch(
                "lisjong_arena.artifact._collect_execution_provenance",
                return_value=comparison_provenance(),
            ),
        ):
            outcome = run_overall_evaluation(**arguments)
        return outcome, calls

    def test_locked_one_shot_execution_produces_a_verified_bundle(self) -> None:
        outcome, calls = self._run()
        self.assertEqual(len(calls), 1)
        plan = calls[0]
        self.assertEqual(plan.game_mode, GAME_MODE)
        self.assertEqual(plan.max_steps, MAX_STEPS)
        self.assertEqual(plan.seeds, SEEDS)
        self.assertEqual(plan.policy_a.identity, heuristic_spec().identity)
        self.assertEqual(plan.policy_b.identity, learning_spec().identity)
        self.assertEqual(outcome.classification["label"], HEURISTIC_SUPERIOR_LABEL)
        self.assertTrue(outcome.comparison_artifact_path.exists())
        self.assertTrue(outcome.overall_result_path.exists())
        self.assertEqual(
            verify_overall_bundle(
                lock_path=self.directory.lock,
                comparison_path=outcome.comparison_artifact_path,
                result_path=outcome.overall_result_path,
            ),
            outcome.overall_result,
        )

    def test_policy_identity_mismatch_fails_before_any_execution(self) -> None:
        executed: list[object] = []

        def execute(plan, *, max_workers):
            executed.append(plan)
            raise AssertionError("execution must not start")

        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "identity"):
                run_overall_evaluation(
                    lock_path=self.directory.lock,
                    heuristic_spec=PolicySpec(
                        identity="not-the-champion", factory=other_learning_policy
                    ),
                    learning_spec=learning_spec(),
                    execute=execute,
                )
        self.assertEqual(executed, [])

    def test_policy_factory_binding_mismatch_fails_before_any_execution(self) -> None:
        def execute(plan, *, max_workers):
            raise AssertionError("execution must not start")

        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "factory binding"):
                run_overall_evaluation(
                    lock_path=self.directory.lock,
                    heuristic_spec=heuristic_spec(),
                    learning_spec=PolicySpec(
                        identity=learning_spec().identity,
                        factory=other_learning_policy,
                    ),
                    execute=execute,
                )

    def test_partial_execution_is_never_adopted(self) -> None:
        partial = seeds(SEED_BLOCK_COUNT - 1)
        result = comparison_result(partial, uniform_profile(TIE))

        def execute(plan, *, max_workers):
            return result

        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionResultError, "400 hanchan"):
                run_overall_evaluation(
                    lock_path=self.directory.lock,
                    heuristic_spec=heuristic_spec(),
                    learning_spec=learning_spec(),
                    execute=execute,
                )

    def test_execution_requires_the_live_merged_main_target(self) -> None:
        def execute(plan, *, max_workers):
            raise AssertionError("execution must not start")

        with (
            mock.patch.object(lock_module, "_require_environment_consistent"),
            mock.patch.object(
                lock_module,
                "collect_execution_provenance",
                return_value=lock_provenance(),
            ),
            mock.patch.object(
                lock_module,
                "require_clean_arena_head",
                side_effect=ExecutionSafetyError("worktree must be clean"),
            ),
        ):
            with self.assertRaises(OverallChampionLockError):
                run_overall_evaluation(
                    lock_path=self.directory.lock,
                    heuristic_spec=heuristic_spec(),
                    learning_spec=learning_spec(),
                    execute=execute,
                )

    def test_build_comparison_plan_locks_game_mode_and_max_steps(self) -> None:
        plan = build_comparison_plan(
            heuristic_spec=heuristic_spec(),
            learning_spec=learning_spec(),
            seeds=SEEDS,
        )
        self.assertEqual(plan.game_mode, GAME_MODE)
        self.assertEqual(plan.max_steps, MAX_STEPS)
        with self.assertRaises(OverallChampionProtocolError):
            build_comparison_plan(
                heuristic_spec=heuristic_spec(),
                learning_spec=learning_spec(),
                seeds=SEEDS[:-1],
            )

    def test_artifacts_are_not_overwritten_on_a_second_run(self) -> None:
        self._run()
        with self.assertRaises(OverallChampionLockError):
            self._run()


if __name__ == "__main__":
    unittest.main()
