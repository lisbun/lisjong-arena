"""Pure/synthetic Phase 7 protocol, metric, diagnostic, and artifact tests."""

import json
import random
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _phase4_raw_corpus_fixtures import fixture_corpus
from lisjong_engine.public_state import PublicRiichiStatus

from lisjong_arena.phase2_training_anchor.training_labels import (
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    aggregate_expected_count_rows,
    evaluate_expected_count_predictions,
    expected_count_metrics_value,
    measure_expected_count_rows,
)
from lisjong_arena.phase5_belief_dataset.model import (
    DatasetPartition,
    GameIdentity,
)
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.training import (
    build_phase6_example,
    expected_count_baseline_prediction,
    materialize_snapshot_example,
)
from lisjong_arena.phase7_snapshot_test.artifact import (
    RESULT_SCHEMA_VERSION,
    Phase7ResultArtifactError,
    load_result,
    save_result,
)
from lisjong_arena.phase7_snapshot_test.evaluation import (
    DatasetGateSpec,
    Phase5TestReference,
    Phase7Preflight,
    _assert_metrics_compatible,
    _subgroup_diagnostics,
    build_phase7_test_example,
    load_phase5_test_reference,
    prepare_preflight,
    validate_locked_dataset,
)
from lisjong_arena.phase7_snapshot_test.protocol import (
    BOOTSTRAP_REPLICATES,
    LOCKED_DATASET_IDENTITY,
    LOCKED_PHASE6_ARTIFACT_IDENTITY,
    LOCKED_PHASE6_MANIFEST_SHA256,
    LOCKED_PHASE6_WEIGHTS_SHA256,
    LOCKED_RAW_CORPUS_IDENTITY,
    MATERIALITY_EPSILON,
    PROTOCOL_ID,
    GateClassification,
    PairedGameCluster,
    _bootstrap_delta_values,
    classify_gate,
    locked_percentile_interval,
    paired_hanchan_bootstrap,
    physical_gate_passes,
    pooled_delta,
)


def _cluster(seed: int, anchors: int, baseline: float, learned: float):
    cells = anchors * 102
    return PairedGameCluster(
        GameIdentity("synthetic", seed),
        anchors,
        cells,
        baseline * cells,
        learned * cells,
    )


def _metrics(samples: int, mae: float) -> dict[str, object]:
    return {
        "samples": samples,
        "per_tile_mae": mae,
        "per_hand_l1": mae * 34,
        "concealed_size_inconsistency_mean": 0.0,
        "concealed_size_inconsistency_max": 0.0,
        "physical_conservation_violation_sample_rate": 0.0,
        "conservation_total_excess": 0.0,
        "conservation_mean_excess_per_sample": 0.0,
    }


def _result_value() -> dict[str, object]:
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "creation_software_revision": "a" * 40,
        "protocol_identity": PROTOCOL_ID,
        "learned_test_partition_evaluated": True,
        "provenance": {
            "phase6_model_artifact_logical_identity": (LOCKED_PHASE6_ARTIFACT_IDENTITY),
            "phase6_weights_sha256": LOCKED_PHASE6_WEIGHTS_SHA256,
            "phase6_manifest_sha256": LOCKED_PHASE6_MANIFEST_SHA256,
            "feature_semantics_id": "phase6-history-snapshot-v1",
            "raw_corpus_identity": LOCKED_RAW_CORPUS_IDENTITY,
            "dataset_identity": LOCKED_DATASET_IDENTITY,
            "test_games": [
                {"source_class": "first-party-bootstrap", "game_seed": seed}
                for seed in range(150, 160)
            ],
            "test_anchor_count": 4_726,
            "dataset_source_revisions": {"arena": "a" * 40},
        },
        "compatibility": {
            "phase5_validation_baseline": _metrics(4_558, 0.5),
            "phase6_validation_readback": {
                **_metrics(4_558, 0.49),
                "constraint_maximum_residual": 0.0,
                "constraint_non_convergence_count": 0,
            },
            "historical_test_baseline_reference_sha256": "1" * 64,
            "historical_test_baseline_reference": _metrics(4_726, 0.5),
            "reproduced_test_baseline": _metrics(4_726, 0.5),
        },
        "primary_metrics": {
            "baseline": _metrics(4_726, 0.5),
            "learned": _metrics(4_726, 0.49),
            "delta_mae": 0.01,
            "relative_improvement": 0.02,
            "materiality_epsilon": MATERIALITY_EPSILON,
        },
        "bootstrap": {
            "rng": "python-stdlib-random.Random",
            "seed": 0,
            "replicates": 20_000,
            "clusters_per_replicate": 10,
            "calls_per_replicate": 10,
            "sampling": "with-replacement",
            "pooling": "selected-anchor-pool-preserving-multiplicity",
            "lower_order_statistic_index": 499,
            "upper_order_statistic_index": 19_499,
            "ci_lower": 0.001,
            "ci_upper": 0.02,
        },
        "diagnostics": {
            "per_game": [{"game_seed": seed} for seed in range(10)],
            "game_macro_mean_delta_mae": 0.01,
            "median_per_game_delta_mae": 0.01,
            "positive_game_count": 10,
            "leave_one_hanchan_out": [
                {"omitted_game_seed": seed} for seed in range(10)
            ],
            "subgroups": {
                "game_seed": [],
                "opponent_relative_seat": [],
                "public_riichi_state": [],
                "true_tenpai_state": [],
            },
        },
        "physical_consistency": {
            "constraint_non_convergence_count": 0,
            "maximum_row_column_residual": 0.0,
            "concealed_size_inconsistency_max": 0.0,
            "physical_conservation_violation_sample_rate": 0.0,
            "conservation_total_excess": 0.0,
            "conservation_mean_excess_per_sample": 0.0,
            "blocking_gate_passed": True,
        },
        "classification": "CONTINUE",
    }


class Phase7SnapshotPureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.raw = save_raw_corpus(fixture_corpus(), root / "raw")
        self.dataset = build_phase5_belief_dataset(
            self.raw, FirstPartySplitPolicy.ACCEPTANCE
        )
        self.samples = resolve_training_samples(self.dataset, self.raw)
        self.predictions = tuple(
            expected_count_baseline_prediction(example, sample)
            for example, sample in zip(self.dataset.examples, self.samples, strict=True)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_phase6_seal_and_phase7_test_only_guard_preserve_values(self):
        example = self.dataset.examples[0]
        sample = self.samples[0]
        self.assertIs(example.partition, DatasetPartition.TEST)
        with self.assertRaisesRegex(ValueError, "rejects TEST"):
            build_phase6_example(example, sample)
        phase7 = build_phase7_test_example(example, sample)
        self.assertEqual(phase7, materialize_snapshot_example(example, sample))
        for partition in (DatasetPartition.TRAIN, DatasetPartition.VALIDATION):
            guarded = replace(example, partition=partition)
            self.assertEqual(
                build_phase6_example(guarded, sample),
                materialize_snapshot_example(guarded, sample),
            )
        with self.assertRaisesRegex(ValueError, "requires TEST"):
            build_phase7_test_example(
                replace(example, partition=DatasetPartition.VALIDATION), sample
            )

    def test_phase7_import_is_torch_free_and_cli_requires_explicit_mode(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import lisjong_arena.phase7_snapshot_test; "
                "assert 'torch' not in sys.modules",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        from lisjong_arena.phase7_snapshot_test.__main__ import _parser

        preflight = _parser().parse_args(
            [
                "preflight",
                "--raw",
                "raw",
                "--dataset",
                "dataset",
                "--model-artifact",
                "model",
                "--phase5-report",
                "report",
            ]
        )
        self.assertEqual(preflight.command, "preflight")
        self.assertFalse(hasattr(preflight, "result"))

    def test_row_measurements_reproduce_anchor_aggregate_exactly(self):
        report = evaluate_expected_count_predictions(
            self.dataset.dataset_identity,
            self.dataset.examples,
            self.samples,
            self.predictions,
        )
        rows = tuple(
            row
            for example, sample, prediction in zip(
                self.dataset.examples,
                self.samples,
                self.predictions,
                strict=True,
            )
            for row in measure_expected_count_rows(example, sample, prediction)
        )
        row_metrics = aggregate_expected_count_rows(rows)
        aggregate = report.partition_metrics[0].metrics
        self.assertEqual(row_metrics.row_count, aggregate.opponent_hand_count)
        self.assertEqual(row_metrics.cell_count, aggregate.cell_count)
        self.assertAlmostEqual(
            row_metrics.absolute_error_sum, aggregate.absolute_error_sum, places=12
        )
        self.assertAlmostEqual(row_metrics.mae, aggregate.per_tile_mae, places=15)

    def test_subgroup_assignments_are_the_four_registered_families(self):
        metrics = (
            evaluate_expected_count_predictions(
                self.dataset.dataset_identity,
                self.dataset.examples,
                self.samples,
                self.predictions,
            )
            .partition_metrics[0]
            .metrics
        )
        first_sample = self.samples[0]
        first_wait = first_sample.labels.structural_waits[0]
        unavailable_wait = replace(
            first_wait,
            mask=None,
            unavailable_reason=StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE,
        )
        changed_labels = replace(
            first_sample.labels,
            structural_waits=(unavailable_wait,)
            + first_sample.labels.structural_waits[1:],
        )
        diagnostic_samples = (
            replace(first_sample, labels=changed_labels),
        ) + self.samples[1:]
        preflight = Phase7Preflight(
            dataset=self.dataset,
            samples=self.samples,
            artifact=None,
            manifest_sha256="0" * 64,
            test_games=tuple(value.game for value in self.dataset.games),
            test_examples=self.dataset.examples,
            test_samples=diagnostic_samples,
            baseline_test_predictions=self.predictions,
            phase5_validation_metrics=metrics,
            learned_validation_metrics={},
            historical_test_reference=Phase5TestReference(
                "1" * 64, expected_count_metrics_value(metrics)
            ),
            reproduced_test_baseline_metrics=metrics,
        )
        diagnostics = _subgroup_diagnostics(preflight, self.predictions)
        self.assertEqual(
            set(diagnostics),
            {
                "game_seed",
                "opponent_relative_seat",
                "public_riichi_state",
                "true_tenpai_state",
            },
        )
        self.assertEqual(
            {value["group"] for value in diagnostics["opponent_relative_seat"]},
            {"1", "2", "3"},
        )
        self.assertEqual(
            {value["group"] for value in diagnostics["public_riichi_state"]},
            {value.value for value in PublicRiichiStatus},
        )
        self.assertTrue(
            any(not value["available"] for value in diagnostics["public_riichi_state"])
        )
        tenpai_groups = {value["group"] for value in diagnostics["true_tenpai_state"]}
        self.assertTrue(
            tenpai_groups
            <= {
                "tenpai",
                "non_tenpai",
                "unavailable:unstable_hand_size",
            }
        )
        self.assertIn("unavailable:unstable_hand_size", tenpai_groups)
        self.assertTrue(
            next(
                value["available"]
                for value in diagnostics["true_tenpai_state"]
                if value["group"] == "unavailable:unstable_hand_size"
            )
        )
        self.assertEqual(
            sum(value["row_count"] for value in diagnostics["true_tenpai_state"]),
            len(self.dataset.examples) * 3,
        )

    def test_dataset_gate_checks_canonical_test_order_and_count(self):
        spec = DatasetGateSpec(
            self.dataset.raw_corpus_identity,
            self.dataset.dataset_identity,
            self.dataset.games[0].game.source_class,
            tuple(value.game.game_seed for value in self.dataset.games),
            len(self.dataset.examples),
        )
        games, examples = validate_locked_dataset(
            self.dataset, self.raw.corpus_identity, spec=spec
        )
        self.assertEqual(games, tuple(value.game for value in self.dataset.games))
        self.assertEqual(examples, self.dataset.examples)
        with self.assertRaisesRegex(RuntimeError, "anchor count"):
            validate_locked_dataset(
                self.dataset,
                self.raw.corpus_identity,
                spec=replace(spec, test_anchor_count=len(examples) + 1),
            )

    def test_artifact_mismatch_stops_preflight_before_any_test_materialization(self):
        with (
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.load_raw_corpus",
                return_value=SimpleNamespace(corpus_identity="raw"),
            ),
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.load_belief_dataset",
                return_value=SimpleNamespace(dataset=object()),
            ),
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.validate_locked_dataset",
                return_value=((), ()),
            ),
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.verify_frozen_artifact",
                side_effect=RuntimeError("artifact mismatch"),
            ),
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.resolve_training_samples"
            ) as resolve,
            patch(
                "lisjong_arena.phase7_snapshot_test.evaluation.build_phase7_test_example"
            ) as materialize_test,
        ):
            with self.assertRaisesRegex(RuntimeError, "artifact mismatch"):
                prepare_preflight(
                    raw_path="raw",
                    dataset_path="dataset",
                    artifact_path="artifact",
                    phase5_report_path="report",
                )
        resolve.assert_not_called()
        materialize_test.assert_not_called()

    def test_exact_phase5_reference_is_required_not_rounded_literal(self):
        metrics = expected_count_metrics_value(
            evaluate_expected_count_predictions(
                self.dataset.dataset_identity,
                self.dataset.examples,
                self.samples,
                self.predictions,
            )
            .partition_metrics[0]
            .metrics
        )
        value = {
            "raw_corpus_identity": self.dataset.raw_corpus_identity,
            "dataset_identity": self.dataset.dataset_identity,
            "games": 60,
            "samples_per_partition": {"test": len(self.dataset.examples)},
            "baseline": {
                "dataset_identity": self.dataset.dataset_identity,
                "partitions": {
                    "test": {
                        "samples": metrics["samples"],
                        "expected_count": {
                            name: metric
                            for name, metric in metrics.items()
                            if name != "samples"
                        },
                    }
                },
            },
        }
        path = Path(self.temporary.name) / "phase5-report.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        spec = DatasetGateSpec(
            self.dataset.raw_corpus_identity,
            self.dataset.dataset_identity,
            self.dataset.games[0].game.source_class,
            tuple(value.game.game_seed for value in self.dataset.games),
            len(self.dataset.examples),
        )
        reference = load_phase5_test_reference(path, spec=spec)
        self.assertEqual(reference.metrics, metrics)
        value["baseline"]["partitions"]["test"]["expected_count"]["per_tile_mae"] = (
            round(metrics["per_tile_mae"], 8)
        )
        path.write_text(json.dumps(value), encoding="utf-8")
        rounded = load_phase5_test_reference(path, spec=spec)
        self.assertNotEqual(rounded.metrics["per_tile_mae"], metrics["per_tile_mae"])
        with self.assertRaisesRegex(RuntimeError, "historical.*drift"):
            _assert_metrics_compatible(
                metrics, rounded.metrics, "historical Phase 5 TEST baseline"
            )
        with self.assertRaisesRegex(RuntimeError, "validation.*drift"):
            _assert_metrics_compatible(
                metrics,
                {**metrics, "per_hand_l1": metrics["per_hand_l1"] + 1e-9},
                "frozen learned validation",
            )

    def test_bootstrap_rng_multiplicity_pooling_order_statistics_and_repeatability(
        self,
    ):
        clusters = (
            _cluster(1, 1, 0.9, 0.1),
            _cluster(2, 9, 0.2, 0.1),
        )
        values = _bootstrap_delta_values(
            clusters, seed=0, replicates=2, clusters_per_replicate=3
        )
        rng = random.Random(0)
        expected = tuple(
            pooled_delta(tuple(clusters[rng.randrange(2)] for _ in range(3)))
            for _ in range(2)
        )
        self.assertEqual(values, expected)
        self.assertNotEqual(
            pooled_delta((clusters[0], clusters[1], clusters[1])),
            sum(value.delta_mae for value in clusters) / 2,
        )
        ordered = tuple(float(value) for value in reversed(range(BOOTSTRAP_REPLICATES)))
        interval = locked_percentile_interval(ordered)
        self.assertEqual(interval.lower, 499.0)
        self.assertEqual(interval.upper, 19_499.0)
        formal = tuple(_cluster(seed, seed + 1, 0.5, 0.49) for seed in range(10))
        self.assertEqual(
            paired_hanchan_bootstrap(formal), paired_hanchan_bootstrap(formal)
        )

    def test_classification_boundaries_are_exhaustive(self):
        cases = (
            (True, MATERIALITY_EPSILON, 1e-9, 0.1, GateClassification.CONTINUE),
            (
                True,
                MATERIALITY_EPSILON,
                0.0,
                0.1,
                GateClassification.REFORMULATE,
            ),
            (True, 0.001, -0.1, 0.1, GateClassification.REFORMULATE),
            (
                True,
                -MATERIALITY_EPSILON,
                -0.1,
                -1e-9,
                GateClassification.STOP_REWORK,
            ),
            (
                True,
                -MATERIALITY_EPSILON,
                -0.1,
                0.0,
                GateClassification.REFORMULATE,
            ),
            (False, 1.0, 0.5, 1.5, GateClassification.STOP_REWORK),
        )
        for valid, delta, lower, upper, expected in cases:
            with self.subTest(expected=expected, delta=delta):
                self.assertIs(
                    classify_gate(
                        validity_ok=valid,
                        delta_mae=delta,
                        ci_lower=lower,
                        ci_upper=upper,
                    ),
                    expected,
                )

    def test_physical_gate_uses_semantic_violation_rate_not_raw_excess(self):
        self.assertTrue(
            physical_gate_passes(
                constraint_non_convergence_count=0,
                maximum_residual=1e-6,
                concealed_size_inconsistency_max=1e-6,
                conservation_violation_sample_rate=0,
            )
        )
        self.assertFalse(
            physical_gate_passes(
                constraint_non_convergence_count=0,
                maximum_residual=1e-6 + 1e-12,
                concealed_size_inconsistency_max=0,
                conservation_violation_sample_rate=0,
            )
        )

    def test_result_artifact_is_atomic_immutable_and_does_not_touch_phase6(self):
        root = Path(self.temporary.name)
        phase6_manifest = root / "phase6-manifest.json"
        phase6_manifest.write_bytes(b"immutable-phase6")
        before = phase6_manifest.read_bytes()
        destination = root / "phase7-result"
        value = _result_value()
        save_result(destination, value)
        self.assertEqual(load_result(destination), value)
        self.assertEqual(phase6_manifest.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            save_result(destination, value)
        with self.assertRaisesRegex(
            Phase7ResultArtifactError, "classification is internally inconsistent"
        ):
            save_result(
                root / "inconsistent-result",
                {**value, "classification": "REFORMULATE"},
            )


if __name__ == "__main__":
    unittest.main()
