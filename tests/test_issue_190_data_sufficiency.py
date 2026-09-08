"""Protocol, TEST non-exposure, metrics, and classification for Issue #190."""

import hashlib
import math
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_data_sufficiency import source as source_module
from lisjong_arena.learned_policy_data_sufficiency.artifact import training_identity
from lisjong_arena.learned_policy_data_sufficiency.errors import (
    DataSufficiencyError,
    DataSufficiencyEvidenceBlocked,
)
from lisjong_arena.learned_policy_data_sufficiency.experiment import build_result
from lisjong_arena.learned_policy_data_sufficiency.metrics import (
    classify_comparison,
    outcome_for_failure,
    paired_comparison,
    summarize_validation_rows,
)
from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    EXCLUDED_COMPONENTS,
    OUTCOMES,
    SCALE_SEEDS,
    SOURCE_DATASET_IDENTITY,
    TEST_SEEDS_METADATA_ONLY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    DataSufficiencyOutcome,
    plan_document,
)
from lisjong_arena.learned_policy_data_sufficiency.source import (
    load_source_dataset,
)
from lisjong_arena.learned_policy_offline_q import artifact as offlineq_artifact
from lisjong_arena.learned_policy_offline_q.bc_training import (
    locked_model_block,
    locked_training_block,
)
from lisjong_arena.learned_policy_offline_q.protocol import Split


def _validation_rows(scale_offset: float, agreement: bool = True):
    return [
        {
            "source_row_index": 1000 + seed,
            "seed": seed,
            "round_ordinal": 0,
            "actor_seat": 0,
            "decision_ordinal": 0,
            "masked_ce": 1.0 + scale_offset + (seed - VALIDATION_SEEDS[0]) * 0.01,
            "teacher_exact_match": agreement,
        }
        for seed in VALIDATION_SEEDS
    ]


def _fixture_prefix_binding(dataset):
    row_count = sum(
        game["row_count"]
        for game in dataset.manifest["games"]
        if game["seed"] in (*TRAIN_SEEDS, *VALIDATION_SEEDS)
    )
    lines = (
        (dataset.path / offlineq_artifact.ROWS_FILENAME)
        .read_bytes()
        .splitlines(keepends=True)
    )
    rows = b"".join(lines[:row_count])
    features = (dataset.path / offlineq_artifact.FEATURES_FILENAME).read_bytes()[
        : row_count * 8204 * 4
    ]
    masks = (dataset.path / offlineq_artifact.LEGAL_MASK_FILENAME).read_bytes()[
        : row_count * 802
    ]
    return {
        "row_count": row_count,
        "rows_sha256": hashlib.sha256(rows).hexdigest(),
        "features_sha256": hashlib.sha256(features).hexdigest(),
        "legal_mask_sha256": hashlib.sha256(masks).hexdigest(),
    }


def _scale_cell(scale: str, offset: float, agreement: bool = True):
    rows = _validation_rows(offset, agreement)
    return {
        "scale": scale,
        "train_seeds": list(SCALE_SEEDS[scale]),
        "train_hanchan_count": len(SCALE_SEEDS[scale]),
        "train_row_count": len(SCALE_SEEDS[scale]),
        "train_eligible_decision_count": len(SCALE_SEEDS[scale]),
        "training_identity": training_identity(scale, SOURCE_DATASET_IDENTITY),
        "checkpoint_identity": hashlib.sha256(f"{scale}-manifest".encode()).hexdigest(),
        "checkpoint_weights_sha256": hashlib.sha256(
            f"{scale}-weights".encode()
        ).hexdigest(),
        "selected_epoch": 1,
        "selected_validation_choice_masked_ce": summarize_validation_rows(rows)[
            "aggregate_masked_ce"
        ],
        "training_wall_clock_seconds": 1.0,
        "training_cpu_seconds": 0.5,
        "validation_rows": rows,
        "validation": summarize_validation_rows(rows),
    }


class ProtocolTest(unittest.TestCase):
    def test_exact_source_and_nested_seed_only_scales_are_locked(self):
        self.assertEqual(
            SOURCE_DATASET_IDENTITY,
            "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4",
        )
        self.assertEqual(TRAIN_SEEDS, tuple(range(245, 265)))
        self.assertEqual(VALIDATION_SEEDS, tuple(range(265, 271)))
        self.assertEqual(TEST_SEEDS_METADATA_ONLY, tuple(range(271, 277)))
        previous = ()
        for scale, count in (("S5", 5), ("S10", 10), ("S15", 15), ("S20", 20)):
            self.assertEqual(SCALE_SEEDS[scale], TRAIN_SEEDS[:count])
            self.assertEqual(SCALE_SEEDS[scale][: len(previous)], previous)
            previous = SCALE_SEEDS[scale]

    def test_exact_140_bc_family_and_all_non_goals_are_explicit(self):
        plan = plan_document()
        self.assertEqual(plan["feature"]["dimension"], 8204)
        self.assertEqual(plan["model"], locked_model_block())
        self.assertEqual(plan["model"]["hidden_width"], 128)
        self.assertEqual(plan["model"]["output_dimension"], 802)
        self.assertEqual(plan["training"], locked_training_block())
        self.assertEqual(
            plan["training"]["loss"], "masked_cross_entropy_over_legal_actions"
        )
        self.assertEqual(plan["changed_axis"], "train_hanchan_count")
        self.assertEqual(
            set(EXCLUDED_COMPONENTS),
            {
                "p1_8241_features",
                "p2_tile_structured_architecture",
                "q_bootstrap",
                "cql",
                "reward_objective",
                "auxiliary_head",
                "hand_belief",
                "test_rows",
            },
        )
        self.assertFalse(plan["source_dataset"]["test_rows_read"])

    def test_outcome_set_is_exhaustive(self):
        self.assertEqual(
            set(OUTCOMES),
            {
                "CLEAR DATA SCALE SIGNAL",
                "NO CLEAR DATA SCALE SIGNAL",
                "DATA SUFFICIENCY EVIDENCE BLOCKED",
                "STOP / INVALID",
            },
        )


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self.root / "dataset", rows_per_game=2)

    def _load_fixture(self):
        with (
            mock.patch.object(
                source_module, "SOURCE_DATASET_IDENTITY", self.dataset.identity
            ),
            mock.patch.object(
                source_module,
                "SOURCE_PREFIX_BINDING",
                _fixture_prefix_binding(self.dataset),
            ),
        ):
            return load_source_dataset(self.dataset.path)

    def test_production_loader_rejects_a_substitute_dataset_identity(self):
        with self.assertRaises(DataSufficiencyEvidenceBlocked):
            load_source_dataset(self.dataset.path)

    def test_only_train_and_validation_rows_are_materialized(self):
        source = self._load_fixture()
        self.assertEqual(
            tuple(dict.fromkeys(row.seed for row in source.rows)),
            (*TRAIN_SEEDS, *VALIDATION_SEEDS),
        )
        self.assertTrue(all(row.split is not Split.TEST for row in source.rows))

    def test_test_rows_are_not_read_even_for_artifact_verification(self):
        relevant_count = sum(
            game["row_count"]
            for game in self.dataset.manifest["games"]
            if game["seed"] in (*TRAIN_SEEDS, *VALIDATION_SEEDS)
        )
        rows_path = self.dataset.path / offlineq_artifact.ROWS_FILENAME
        lines = rows_path.read_bytes().splitlines(keepends=True)
        # Same byte length and file size, but invalid JSON at the first TEST row.
        lines[relevant_count] = b"!" + lines[relevant_count][1:]
        rows_path.write_bytes(b"".join(lines))
        source = self._load_fixture()
        self.assertEqual(len(source.rows), relevant_count)

    def test_train_validation_prefix_digest_mismatch_is_blocked(self):
        expected_prefix = _fixture_prefix_binding(self.dataset)
        features_path = self.dataset.path / offlineq_artifact.FEATURES_FILENAME
        payload = bytearray(features_path.read_bytes())
        payload[0] ^= 0x01
        features_path.write_bytes(payload)
        with (
            mock.patch.object(
                source_module, "SOURCE_DATASET_IDENTITY", self.dataset.identity
            ),
            mock.patch.object(
                source_module,
                "SOURCE_PREFIX_BINDING",
                expected_prefix,
            ),
        ):
            with self.assertRaises(DataSufficiencyEvidenceBlocked):
                load_source_dataset(self.dataset.path)


class MetricsAndResultTest(unittest.TestCase):
    source = SimpleNamespace(
        identity=SOURCE_DATASET_IDENTITY,
        provenance={
            "execution_environment": "riichienv",
            "lisjong_arena_version": "0.1.0",
            "lisjong_arena_revision": "0" * 40,
            "lisjong_version": "0.1.0",
            "lisjong_revision": ("a0666d24e66179a45fd6e231a3cbd489b492d162"),
            "lisjong_engine_version": "0.1.0",
            "lisjong_engine_revision": "1" * 40,
            "riichienv_version": "0.4.8",
            "python_version": "3.14.0",
        },
    )

    def test_aggregate_and_per_hanchan_metrics_derive_from_raw_rows(self):
        rows = _validation_rows(0.0)
        rows.append(
            {
                **rows[0],
                "source_row_index": rows[0]["source_row_index"] + 10_000,
                "masked_ce": 3.0,
                "teacher_exact_match": False,
            }
        )
        summary = summarize_validation_rows(rows)
        first = summary["per_hanchan"][0]
        self.assertEqual(first["eligible_decision_count"], 2)
        self.assertEqual(first["masked_ce_sum"], 4.0)
        self.assertEqual(first["masked_ce"], 2.0)
        self.assertNotEqual(
            summary["aggregate_masked_ce"],
            sum(entry["masked_ce"] for entry in summary["per_hanchan"]) / 6,
        )

    def test_paired_sign_and_normal_interval_are_exact(self):
        scales = {
            scale: _scale_cell(scale, 0.0 if scale != "S20" else -0.2)
            for scale in SCALE_SEEDS
        }
        comparison = paired_comparison(scales)
        self.assertTrue(
            all(
                math.isclose(pair["difference_s20_minus_s15"], -0.2)
                for pair in comparison["pairs"]
            )
        )
        self.assertLess(comparison["interval_upper"], 0.0)
        self.assertEqual(
            classify_comparison(comparison),
            DataSufficiencyOutcome.CLEAR_DATA_SCALE_SIGNAL,
        )

    def test_interval_touching_zero_is_no_clear_signal(self):
        self.assertEqual(
            classify_comparison(
                {"interval_lower": -1.0, "interval_upper": 0.0, "hanchan_count": 6}
            ),
            DataSufficiencyOutcome.NO_CLEAR_DATA_SCALE_SIGNAL,
        )

    def test_non_finite_metrics_are_rejected(self):
        rows = _validation_rows(0.0)
        rows[0]["masked_ce"] = float("nan")
        with self.assertRaises(DataSufficiencyError):
            summarize_validation_rows(rows)

    def test_teacher_agreement_cannot_change_classification(self):
        good = {
            scale: _scale_cell(scale, -0.2 if scale == "S20" else 0.0, True)
            for scale in SCALE_SEEDS
        }
        bad_agreement = {
            scale: _scale_cell(scale, -0.2 if scale == "S20" else 0.0, False)
            for scale in SCALE_SEEDS
        }
        first = build_result(
            source=self.source,
            backend="fixture",
            key="offlineq-190-data-sufficiency-preflight/run",
            scales=good,
        )
        second = build_result(
            source=self.source,
            backend="fixture",
            key="offlineq-190-data-sufficiency-preflight/run",
            scales=bad_agreement,
        )
        self.assertEqual(first["classification"], second["classification"])

    def test_recorded_classification_is_uniquely_rederived(self):
        scales = {scale: _scale_cell(scale, 0.0) for scale in SCALE_SEEDS}
        result = build_result(
            source=self.source,
            backend="fixture",
            key="offlineq-190-data-sufficiency-preflight/run",
            scales=scales,
        )
        tampered = deepcopy(result)
        tampered["classification"] = "CLEAR DATA SCALE SIGNAL"
        from lisjong_arena.learned_policy_data_sufficiency.artifact import (
            validate_result,
        )

        with self.assertRaises(DataSufficiencyError):
            validate_result(tampered)

    def test_validation_decision_population_must_match_across_all_scales(self):
        scales = {scale: _scale_cell(scale, 0.0) for scale in SCALE_SEEDS}
        scales["S10"]["validation_rows"][0]["decision_ordinal"] = 99
        scales["S10"]["validation"] = summarize_validation_rows(
            scales["S10"]["validation_rows"]
        )
        with self.assertRaises(DataSufficiencyError):
            build_result(
                source=self.source,
                backend="fixture",
                key="offlineq-190-data-sufficiency-preflight/run",
                scales=scales,
            )

    def test_blocked_and_invalid_failures_are_distinct(self):
        blocked = DataSufficiencyEvidenceBlocked("missing")
        invalid = DataSufficiencyError("malformed")
        self.assertEqual(
            outcome_for_failure(blocked), DataSufficiencyOutcome.EVIDENCE_BLOCKED
        )
        self.assertEqual(
            outcome_for_failure(invalid), DataSufficiencyOutcome.STOP_INVALID
        )


if __name__ == "__main__":
    unittest.main()
