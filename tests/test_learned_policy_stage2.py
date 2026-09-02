"""Learned Policy Stage 2 protocol, dataset artifact, and coverage tests."""

import json
import tempfile
import unittest
from pathlib import Path

from _learned_policy_stage2_fixtures import (
    FIXTURE_PROVENANCE,
    PASS_INDEX,
    RIICHI_INDEX,
    decision_row,
    feature_values,
    legal_mask,
    write_synthetic_dataset,
)

from lisjong_arena.learned_policy_stage2 import (
    Stage2ArtifactError,
    Stage2ContractIdentityError,
    Stage2DatasetWriter,
    Stage2DecisionRow,
    Stage2ProtocolError,
    Stage2RecordingError,
)
from lisjong_arena.learned_policy_stage2.artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    load_dataset,
)
from lisjong_arena.learned_policy_stage2.coverage import build_coverage
from lisjong_arena.learned_policy_stage2.evaluation import (
    conditional_uniform_reference,
)
from lisjong_arena.learned_policy_stage2.protocol import (
    ACTION_FAMILY_NAMES,
    FEATURE_DIMENSION,
    LOCKED_FEATURE_SCHEMA_FINGERPRINT,
    LOCKED_VOCABULARY_FINGERPRINT,
    ORDERED_SEEDS,
    SPLIT_SEEDS,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
    verify_contract_identity,
    vocabulary_fingerprint,
)


class ProtocolTest(unittest.TestCase):
    def test_installed_contracts_match_the_locked_stage2_identity(self):
        verify_contract_identity()
        self.assertEqual(vocabulary_fingerprint(), LOCKED_VOCABULARY_FINGERPRINT)
        from lisjong_arena.learned_policy_input import schema_fingerprint

        self.assertEqual(schema_fingerprint(), LOCKED_FEATURE_SCHEMA_FINGERPRINT)
        self.assertEqual(FEATURE_DIMENSION, 8204)
        self.assertEqual(VOCABULARY_SIZE, 802)

    def test_split_partitions_exactly_the_locked_population(self):
        self.assertEqual(ORDERED_SEEDS, tuple(range(200, 216)))
        self.assertEqual(SPLIT_SEEDS[Split.TRAIN], tuple(range(200, 210)))
        self.assertEqual(SPLIT_SEEDS[Split.VALIDATION], (210, 211, 212))
        self.assertEqual(SPLIT_SEEDS[Split.TEST], (213, 214, 215))
        assigned = [split_for_seed(seed) for seed in ORDERED_SEEDS]
        self.assertEqual(len(assigned), len(ORDERED_SEEDS))

    def test_unknown_seed_fails_closed(self):
        for seed in (199, 216, -1):
            with self.assertRaises(Stage2ProtocolError):
                split_for_seed(seed)

    def test_action_families_come_from_the_actual_vocabulary(self):
        self.assertEqual(
            ACTION_FAMILY_NAMES,
            (
                "discard",
                "riichi",
                "chi",
                "pon",
                "daiminkan",
                "ankan",
                "kakan",
                "ron",
                "tsumo",
                "pass",
                "kyuushu_kyuuhai",
            ),
        )
        families = {action_family(index) for index in range(VOCABULARY_SIZE)}
        self.assertEqual(families, set(ACTION_FAMILY_NAMES))
        self.assertEqual(action_family(0), "discard")
        self.assertEqual(action_family(RIICHI_INDEX), "riichi")
        self.assertEqual(action_family(PASS_INDEX), "pass")
        self.assertEqual(action_family(VOCABULARY_SIZE - 1), "kyuushu_kyuuhai")
        with self.assertRaises(Stage2ProtocolError):
            action_family(VOCABULARY_SIZE)


class DecisionRowTest(unittest.TestCase):
    def test_valid_row_exposes_forced_and_choice_semantics(self):
        forced = decision_row(200, 0, legal_indices=(PASS_INDEX,))
        self.assertEqual(forced.legal_action_count, 1)
        self.assertFalse(forced.is_choice_row)
        choice = decision_row(200, 1, legal_indices=(0, 2, RIICHI_INDEX))
        self.assertEqual(choice.legal_action_count, 3)
        self.assertTrue(choice.is_choice_row)
        self.assertEqual(choice.legal_action_indices, (0, 2, RIICHI_INDEX))

    def test_feature_dimension_must_be_exactly_8204(self):
        with self.assertRaises(Stage2RecordingError):
            decision_row(200, 0, values=(0.0,) * (FEATURE_DIMENSION - 1))

    def test_non_finite_feature_fails_closed(self):
        values = list(feature_values(200, 0))
        values[3] = float("inf")
        with self.assertRaises(Stage2RecordingError):
            decision_row(200, 0, values=tuple(values))

    def test_illegal_teacher_action_fails_closed(self):
        with self.assertRaises(Stage2RecordingError):
            decision_row(200, 0, legal_indices=(0, 2), teacher_index=RIICHI_INDEX)

    def test_teacher_family_must_match_the_vocabulary_block(self):
        row = decision_row(200, 0, legal_indices=(0, 2), teacher_index=0)
        with self.assertRaises(Stage2RecordingError):
            Stage2DecisionRow(
                seed=row.seed,
                split=row.split,
                step_ordinal=row.step_ordinal,
                decision_ordinal=row.decision_ordinal,
                round_ordinal=row.round_ordinal,
                round_wind=row.round_wind,
                hand_number=row.hand_number,
                honba=row.honba,
                actor_seat=row.actor_seat,
                feature_values=row.feature_values,
                legal_mask=row.legal_mask,
                teacher_action_index=row.teacher_action_index,
                teacher_action_family="riichi",
            )

    def test_empty_legal_mask_fails_closed(self):
        with self.assertRaises(ValueError):
            legal_mask(())


class DatasetArtifactTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_round_trip_binds_every_required_identity(self):
        dataset = write_synthetic_dataset(self.root / "dataset")
        reloaded = load_dataset(self.root / "dataset")
        self.assertEqual(reloaded.identity, dataset.identity)
        self.assertEqual(reloaded.row_count, dataset.row_count)

        manifest = reloaded.manifest
        self.assertEqual(
            manifest["feature"]["schema_fingerprint"],
            LOCKED_FEATURE_SCHEMA_FINGERPRINT,
        )
        self.assertEqual(manifest["feature"]["dimension"], FEATURE_DIMENSION)
        self.assertEqual(
            manifest["vocabulary"]["fingerprint"], LOCKED_VOCABULARY_FINGERPRINT
        )
        self.assertEqual(manifest["vocabulary"]["size"], VOCABULARY_SIZE)
        self.assertEqual(manifest["protocol"]["teacher_identity"], "yakuhai-call")
        self.assertEqual(manifest["protocol"]["game_mode"], "4p-red-half")
        self.assertEqual(manifest["protocol"]["split_unit"], "whole_hanchan")
        self.assertEqual(manifest["protocol"]["ordered_seeds"], list(ORDERED_SEEDS))
        self.assertEqual(manifest["provenance"], FIXTURE_PROVENANCE)
        self.assertEqual(len(manifest["games"]), 16)

    def test_every_row_satisfies_the_hard_dataset_invariants(self):
        dataset = write_synthetic_dataset(self.root / "dataset")
        self.assertEqual(dataset.count_non_finite_features(), 0)
        for index, row in enumerate(dataset.rows):
            self.assertEqual(len(dataset.feature_row(index)), FEATURE_DIMENSION)
            mask = dataset.legal_mask_row(index)
            self.assertEqual(len(mask), VOCABULARY_SIZE)
            self.assertTrue(mask[row.teacher_action_index])
            self.assertEqual(sum(mask), row.legal_action_count)

    def test_split_membership_never_crosses_games(self):
        dataset = write_synthetic_dataset(self.root / "dataset")
        seeds_by_split = {split: set() for split in Split}
        for row in dataset.rows:
            seeds_by_split[row.split].add(row.seed)
            self.assertIs(row.split, split_for_seed(row.seed))
        for split, seeds in seeds_by_split.items():
            self.assertEqual(seeds, set(SPLIT_SEEDS[split]))
        self.assertEqual(
            seeds_by_split[Split.TRAIN]
            & (seeds_by_split[Split.VALIDATION] | seeds_by_split[Split.TEST]),
            set(),
        )

    def test_existing_destination_is_never_overwritten(self):
        write_synthetic_dataset(self.root / "dataset")
        with self.assertRaises(FileExistsError):
            Stage2DatasetWriter(self.root / "dataset", provenance=FIXTURE_PROVENANCE)

    def test_incomplete_seed_population_fails_closed(self):
        writer = Stage2DatasetWriter(
            self.root / "partial", provenance=FIXTURE_PROVENANCE
        )
        writer.add_game(
            seed=200,
            split=Split.TRAIN,
            step_count=2,
            scores=(25000, 25000, 25000, 25000),
            ranks=(1, 2, 3, 4),
            rows=(decision_row(200, ordinal) for ordinal in range(2)),
        )
        with self.assertRaises(Stage2ArtifactError):
            writer.finalize()
        self.assertFalse((self.root / "partial").exists())

    def test_out_of_order_seeds_fail_closed(self):
        writer = Stage2DatasetWriter(
            self.root / "unordered", provenance=FIXTURE_PROVENANCE
        )
        self.addCleanup(writer.discard)
        writer.add_game(
            seed=201,
            split=Split.TRAIN,
            step_count=1,
            scores=(25000, 25000, 25000, 25000),
            ranks=(1, 2, 3, 4),
            rows=(decision_row(201, 0),),
        )
        with self.assertRaises(Stage2ArtifactError):
            writer.add_game(
                seed=200,
                split=Split.TRAIN,
                step_count=1,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(decision_row(200, 0),),
            )

    def test_row_from_a_different_game_fails_closed(self):
        writer = Stage2DatasetWriter(self.root / "mixed", provenance=FIXTURE_PROVENANCE)
        self.addCleanup(writer.discard)
        with self.assertRaises(Stage2ArtifactError):
            writer.add_game(
                seed=200,
                split=Split.TRAIN,
                step_count=1,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(decision_row(201, 0),),
            )

    def _corrupt(self, name: str, mutate) -> None:
        path = self.root / "dataset"
        write_synthetic_dataset(path)
        target = path / name
        mutate(target)
        with self.assertRaises(Stage2ArtifactError):
            load_dataset(path)

    def test_corrupt_feature_bytes_fail_closed(self):
        def mutate(target: Path) -> None:
            payload = bytearray(target.read_bytes())
            payload[0] ^= 0xFF
            target.write_bytes(bytes(payload))

        self._corrupt(FEATURES_FILENAME, mutate)

    def test_corrupt_legal_mask_bytes_fail_closed(self):
        def mutate(target: Path) -> None:
            payload = bytearray(target.read_bytes())
            payload[0] = 1 - payload[0]
            target.write_bytes(bytes(payload))

        self._corrupt(LEGAL_MASK_FILENAME, mutate)

    def test_corrupt_row_metadata_fails_closed(self):
        def mutate(target: Path) -> None:
            lines = target.read_text(encoding="utf-8").splitlines()
            document = json.loads(lines[0])
            document["legal_action_count"] += 1
            lines[0] = json.dumps(document, sort_keys=True, separators=(",", ":"))
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self._corrupt(ROWS_FILENAME, mutate)

    def test_tampered_manifest_identity_fails_closed(self):
        def mutate(target: Path) -> None:
            document = json.loads(target.read_text(encoding="utf-8"))
            document["totals"]["row_count"] += 1
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        self._corrupt(MANIFEST_FILENAME, mutate)

    def test_unsupported_schema_version_fails_closed(self):
        def mutate(target: Path) -> None:
            document = json.loads(target.read_text(encoding="utf-8"))
            document["dataset_schema_version"] = (
                "arena-learned-policy-stage2-dataset-v2"
            )
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        self._corrupt(MANIFEST_FILENAME, mutate)

    def test_unsupported_feature_fingerprint_fails_closed(self):
        def mutate(target: Path) -> None:
            document = json.loads(target.read_text(encoding="utf-8"))
            document["feature"]["schema_fingerprint"] = "0" * 64
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        self._corrupt(MANIFEST_FILENAME, mutate)

    def test_unsupported_vocabulary_fingerprint_fails_closed(self):
        def mutate(target: Path) -> None:
            document = json.loads(target.read_text(encoding="utf-8"))
            document["vocabulary"]["fingerprint"] = "0" * 64
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

        self._corrupt(MANIFEST_FILENAME, mutate)

    def test_extra_file_fails_closed(self):
        path = self.root / "dataset"
        write_synthetic_dataset(path)
        (path / "extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(Stage2ArtifactError):
            load_dataset(path)

    def test_missing_file_fails_closed(self):
        path = self.root / "dataset"
        write_synthetic_dataset(path)
        (path / LEGAL_MASK_FILENAME).unlink()
        with self.assertRaises(Stage2ArtifactError):
            load_dataset(path)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_coverage_separates_forced_and_choice_rows_per_split(self):
        dataset = write_synthetic_dataset(self.root / "dataset")
        coverage = build_coverage(dataset)
        self.assertEqual(coverage.dataset_identity, dataset.identity)
        self.assertEqual(
            tuple(split.name for split in coverage.splits),
            ("TRAIN", "VALIDATION", "TEST"),
        )
        self.assertEqual([split.hanchan_count for split in coverage.splits], [10, 3, 3])
        self.assertEqual(coverage.total.hanchan_count, 16)
        self.assertEqual(
            sum(split.total_rows for split in coverage.splits),
            coverage.total.total_rows,
        )
        for split in (*coverage.splits, coverage.total):
            self.assertEqual(split.forced_rows + split.choice_rows, split.total_rows)
            self.assertEqual(
                sum(count for _, count in split.family_row_counts),
                split.total_rows,
            )
            self.assertEqual(
                sum(rows for _, rows in split.legal_action_count_distribution),
                split.total_rows,
            )

    def test_absent_families_are_reported_and_never_invented(self):
        dataset = write_synthetic_dataset(self.root / "dataset")
        coverage = build_coverage(dataset)
        present = {name for name, count in coverage.total.family_row_counts if count}
        self.assertEqual(
            set(coverage.total.absent_families),
            set(ACTION_FAMILY_NAMES) - present,
        )
        self.assertEqual(
            set(name for name, _ in coverage.total.family_row_counts),
            set(ACTION_FAMILY_NAMES),
        )


class ConditionalUniformReferenceTest(unittest.TestCase):
    def test_reference_matches_the_closed_form(self):
        cross_entropy, agreement = conditional_uniform_reference((2, 4))
        self.assertAlmostEqual(
            cross_entropy, (0.6931471805599453 + 1.3862943611198906) / 2
        )
        self.assertAlmostEqual(agreement, (0.5 + 0.25) / 2)

    def test_invalid_counts_fail_closed(self):
        with self.assertRaises(ValueError):
            conditional_uniform_reference(())
        with self.assertRaises(ValueError):
            conditional_uniform_reference((1, 0))


class ContractIdentityGuardTest(unittest.TestCase):
    def test_unsupported_feature_semantics_fails_closed(self):
        from lisjong_arena.learned_policy_input import build_policy_input_feature
        from lisjong_arena.learned_policy_input.errors import (
            UnsupportedFeatureSemanticsError,
        )

        self.assertTrue(issubclass(UnsupportedFeatureSemanticsError, Exception))
        with self.assertRaises(UnsupportedFeatureSemanticsError):
            build_policy_input_feature(
                object(), version="arena-policy-input-feature-v2"
            )

    def test_contract_identity_error_is_a_stage2_error(self):
        self.assertTrue(issubclass(Stage2ContractIdentityError, Exception))


if __name__ == "__main__":
    unittest.main()
