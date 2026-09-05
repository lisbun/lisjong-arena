"""Replacement offline TEST artifact / protocol lock tests (Issue #140).

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、artifact writer /
reader boundaryは契約上有効な合成macro-transition rowで検証し、teacher実行を
再現しない。ここでの重点は正常系よりも、locked seed population、artifact
identity、original datasetからの分離、fail closed条件である。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _learned_policy_offline_q_artifact_fixtures import (
    FIXTURE_PROVENANCE,
    replacement_transition_row,
    write_synthetic_dataset,
    write_synthetic_replacement_test,
)

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q import protocol
from lisjong_arena.learned_policy_offline_q.artifact import (
    MANIFEST_FILENAME,
    load_dataset,
)
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQArtifactError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    REPLACEMENT_TEST_HANCHAN_COUNT,
    REPLACEMENT_TEST_SEEDS,
    Split,
    require_replacement_test_seed,
)
from lisjong_arena.learned_policy_offline_q.replacement_test import (
    REPLACEMENT_TEST_SCHEMA_VERSION,
    ReplacementTestWriter,
    artifact_identity,
    load_replacement_test,
)


class ReplacementTestSeedLockTests(unittest.TestCase):
    """re-lockされたseed populationそのものの不変条件。"""

    def test_locked_population_is_354_to_359(self) -> None:
        self.assertEqual(REPLACEMENT_TEST_SEEDS, tuple(range(354, 360)))
        self.assertEqual(len(REPLACEMENT_TEST_SEEDS), REPLACEMENT_TEST_HANCHAN_COUNT)

    def test_does_not_collide_with_any_other_locked_offline_q_population(self) -> None:
        for name, population in (
            ("dataset", protocol.DATASET_ORDERED_SEEDS),
            ("smoke", protocol.SERVING_SMOKE_SEEDS),
            ("screening", protocol.STRENGTH_SCREEN_SEEDS),
        ):
            with self.subTest(population=name):
                self.assertEqual(
                    set(REPLACEMENT_TEST_SEEDS).intersection(population), set()
                )

    def test_does_not_collide_with_sibling_experiment_populations(self) -> None:
        """#146 / #148が後から取得したdevelopment populationとの衝突を機械的に防ぐ。

        当初amendmentがlockした`306..311`は、そのamendment記録後にmergeされた
        Arena #147 (`306..329`) / #149 (`330..353`) と衝突した。同じ事故を
        再発させないよう、siblingのlocked populationをcross-checkする。
        """
        from lisjong_arena.learned_policy_stage4a.protocol import SCREENING_SEEDS
        from lisjong_arena.phase5_belief_dataset.split import (
            KAN_COVERAGE_DEVELOPMENT_SEEDS,
            MIX_PILOT_DEVELOPMENT_SEEDS,
        )

        offline_q = (
            set(protocol.DATASET_ORDERED_SEEDS)
            | set(protocol.SERVING_SMOKE_SEEDS)
            | set(protocol.STRENGTH_SCREEN_SEEDS)
            | set(REPLACEMENT_TEST_SEEDS)
        )
        for name, population in (
            ("stage4a-screening", SCREENING_SEEDS),
            ("kan-coverage-development", KAN_COVERAGE_DEVELOPMENT_SEEDS),
            ("mix-pilot-development", MIX_PILOT_DEVELOPMENT_SEEDS),
        ):
            with self.subTest(population=name):
                self.assertEqual(offline_q.intersection(population), set())

    def test_require_replacement_test_seed_fails_closed(self) -> None:
        for seed in REPLACEMENT_TEST_SEEDS:
            self.assertEqual(require_replacement_test_seed(seed), seed)
        for seed in (245, 271, 280, 305, 306, 311, 353, 360):
            with self.subTest(seed=seed):
                with self.assertRaises(OfflineQProtocolError):
                    require_replacement_test_seed(seed)
        with self.assertRaises(TypeError):
            require_replacement_test_seed("354")


class ReplacementTestArtifactTests(unittest.TestCase):
    def test_round_trips_and_reports_locked_population(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(
                Path(directory) / "replacement", rows_per_game=6
            )
            self.assertEqual(artifact.hanchan_count, REPLACEMENT_TEST_HANCHAN_COUNT)
            self.assertEqual(artifact.row_count, 6 * REPLACEMENT_TEST_HANCHAN_COUNT)
            self.assertEqual(
                artifact.terminal_row_count, REPLACEMENT_TEST_HANCHAN_COUNT
            )
            self.assertEqual(
                artifact.nonterminal_row_count,
                artifact.row_count - REPLACEMENT_TEST_HANCHAN_COUNT,
            )
            self.assertEqual(artifact.count_non_finite_features(), 0)
            self.assertEqual(
                tuple(sorted({row.seed for row in artifact.rows})),
                REPLACEMENT_TEST_SEEDS,
            )
            self.assertTrue(all(row.split is Split.TEST for row in artifact.rows))

            reloaded = load_replacement_test(artifact.path)
            self.assertEqual(reloaded.identity, artifact.identity)

    def test_manifest_declares_purpose_and_transition_schema(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            manifest = artifact.manifest
            self.assertEqual(
                manifest["replacement_test_schema_version"],
                REPLACEMENT_TEST_SCHEMA_VERSION,
            )
            self.assertEqual(manifest["purpose"], protocol.REPLACEMENT_TEST_PURPOSE)
            self.assertEqual(
                manifest["protocol"]["replacement_test_seeds"],
                list(REPLACEMENT_TEST_SEEDS),
            )
            self.assertEqual(
                manifest["protocol"]["teacher_source_revision"],
                protocol.TEACHER_SOURCE_REVISION,
            )
            self.assertEqual(manifest["protocol"]["game_mode"], protocol.GAME_MODE)
            self.assertEqual(
                manifest["feature"]["schema_fingerprint"],
                protocol.LOCKED_FEATURE_SCHEMA_FINGERPRINT,
            )
            self.assertEqual(
                manifest["vocabulary"]["fingerprint"],
                protocol.LOCKED_VOCABULARY_FINGERPRINT,
            )

    def test_identity_differs_from_the_training_dataset(self) -> None:
        """replacement TESTはoriginal training datasetと別のartifactである。"""
        with TemporaryDirectory() as directory:
            dataset = write_synthetic_dataset(Path(directory) / "dataset")
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            self.assertNotEqual(artifact.identity, dataset.identity)
            # original datasetはreplacement TEST生成後も一切変わらない。
            self.assertEqual(load_dataset(dataset.path).identity, dataset.identity)
            self.assertNotIn("replacement_test_seeds", dataset.manifest["protocol"])

    def test_rejects_a_seed_outside_the_locked_population(self) -> None:
        with TemporaryDirectory() as directory:
            writer = ReplacementTestWriter(
                Path(directory) / "replacement", provenance=FIXTURE_PROVENANCE
            )
            try:
                with self.assertRaises(OfflineQProtocolError):
                    writer.add_game(
                        seed=306,
                        scores=(25000,) * 4,
                        ranks=(1, 2, 3, 4),
                        rows=(replacement_transition_row(306, 0, rows_per_game=2),),
                    )
            finally:
                writer.discard()

    def test_rejects_an_incomplete_population(self) -> None:
        with TemporaryDirectory() as directory:
            writer = ReplacementTestWriter(
                Path(directory) / "replacement", provenance=FIXTURE_PROVENANCE
            )
            try:
                writer.add_game(
                    seed=REPLACEMENT_TEST_SEEDS[0],
                    scores=(25000,) * 4,
                    ranks=(1, 2, 3, 4),
                    rows=(
                        replacement_transition_row(
                            REPLACEMENT_TEST_SEEDS[0], ordinal, rows_per_game=2
                        )
                        for ordinal in range(2)
                    ),
                )
                with self.assertRaises(OfflineQArtifactError):
                    writer.finalize()
            finally:
                writer.discard()

    def test_rejects_descending_or_duplicate_seed_order(self) -> None:
        with TemporaryDirectory() as directory:
            writer = ReplacementTestWriter(
                Path(directory) / "replacement", provenance=FIXTURE_PROVENANCE
            )
            try:
                for seed in (REPLACEMENT_TEST_SEEDS[1], REPLACEMENT_TEST_SEEDS[0]):
                    rows = [
                        replacement_transition_row(seed, ordinal, rows_per_game=2)
                        for ordinal in range(2)
                    ]
                    if seed == REPLACEMENT_TEST_SEEDS[0]:
                        with self.assertRaises(OfflineQArtifactError):
                            writer.add_game(
                                seed=seed,
                                scores=(25000,) * 4,
                                ranks=(1, 2, 3, 4),
                                rows=rows,
                            )
                    else:
                        writer.add_game(
                            seed=seed,
                            scores=(25000,) * 4,
                            ranks=(1, 2, 3, 4),
                            rows=rows,
                        )
            finally:
                writer.discard()

    def test_does_not_overwrite_an_existing_destination(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "replacement"
            write_synthetic_replacement_test(destination)
            with self.assertRaises(FileExistsError):
                ReplacementTestWriter(destination, provenance=FIXTURE_PROVENANCE)

    def test_rejects_provenance_from_a_different_teacher_revision(self) -> None:
        provenance = dict(FIXTURE_PROVENANCE)
        provenance["lisjong_revision"] = "9" * 40
        with TemporaryDirectory() as directory:
            with self.assertRaises(OfflineQArtifactError):
                ReplacementTestWriter(
                    Path(directory) / "replacement", provenance=provenance
                )

    def test_detects_a_tampered_manifest(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            manifest_path = artifact.path / MANIFEST_FILENAME
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["totals"]["row_count"] += 1
            manifest_path.write_text(
                canonical_json_text(document), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(OfflineQArtifactError):
                load_replacement_test(artifact.path)

    def test_detects_a_tampered_identity(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            manifest_path = artifact.path / MANIFEST_FILENAME
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["artifact_identity"] = "0" * 64
            manifest_path.write_text(
                canonical_json_text(document), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(OfflineQArtifactError):
                load_replacement_test(artifact.path)

    def test_detects_a_tampered_payload(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            features = artifact.path / "features.f32"
            payload = bytearray(features.read_bytes())
            payload[0] ^= 0xFF
            features.write_bytes(bytes(payload))
            with self.assertRaises(OfflineQArtifactError):
                load_replacement_test(artifact.path)

    def test_detects_missing_or_extra_files(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            (artifact.path / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(OfflineQArtifactError):
                load_replacement_test(artifact.path)

    def test_identity_excludes_only_itself(self) -> None:
        with TemporaryDirectory() as directory:
            artifact = write_synthetic_replacement_test(Path(directory) / "replacement")
            self.assertEqual(
                artifact_identity(artifact.manifest),
                artifact.manifest["artifact_identity"],
            )


if __name__ == "__main__":
    unittest.main()
