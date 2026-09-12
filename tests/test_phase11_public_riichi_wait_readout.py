"""Non-ML contract tests for Arena #172 and its #209 continuation."""

import ast
import copy
import hashlib
import json
import math
import random
import unittest
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policy_contract import Seat as LisjongSeat
from lisjong.policy_contract import Wind as LisjongWind
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.wind import Wind as EngineWind

import lisjong_arena.phase11_public_riichi_wait_readout.continuation as continuation
from lisjong_arena._execution_safety import ExecutionSafetyError
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_public_riichi_wait_readout.artifact import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    model_manifest_without_weights,
)
from lisjong_arena.phase11_public_riichi_wait_readout.continuation import (
    PHASE11_ARTIFACT_BINDING,
    SCIENTIFIC_EXECUTION_REVISION,
    ImmutableArtifactBinding,
)
from lisjong_arena.phase11_public_riichi_wait_readout.coverage import (
    build_coverage,
    load_coverage,
    save_coverage,
    validate_coverage,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    LatentExample,
    OpponentTarget,
    _targets_for_step,
)
from lisjong_arena.phase11_public_riichi_wait_readout.evaluation import (
    _add,
    _empty_aggregate,
    _reliability_rows,
    classify_interval,
    fit_train_prevalence,
    paired_comparison,
)
from lisjong_arena.phase11_public_riichi_wait_readout.lock import validate_lock
from lisjong_arena.phase11_public_riichi_wait_readout.protocol import (
    BOOTSTRAP_ORDER_INDICES,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    CLEAR_REGRESSION,
    CLEAR_SIGNAL,
    E160_SELECTED_EPOCH,
    E160_WEIGHTS_SHA256,
    FORMAL_TEST,
    INCONCLUSIVE,
    INSUFFICIENT_COVERAGE,
    MAX_EPOCHS,
    OUTCOMES,
    PATIENCE,
    PHASE167_EXECUTION_LOCK_IDENTITY,
    PHASE167_RESULT_IDENTITY,
    READOUT_OUTPUT_DIM,
    SELECTION_EXPOSURE,
    TILE_KIND_COUNT,
    Phase11Error,
    canonical_json_bytes,
    evaluation_value,
    identity,
    readout_value,
    representation_value,
    retained_value,
    training_value,
)
from lisjong_arena.phase11_public_riichi_wait_readout.result import (
    _aggregate_totals,
    _same_totals,
    _sequential_sum_error_bound,
    _validate_evaluation_evidence,
    assemble_result,
    budget_diagnostics,
    validate_result,
)
from lisjong_arena.phase11_public_riichi_wait_readout.training import EpochMetrics
from lisjong_arena.stage3_optimization_saturation.protocol import RULES


def _runtime():
    return {
        "python": "3.14.6",
        "torch": "2.13.0+cpu",
        "riichienv": "0.4.8",
        "platform": "Windows-11-test",
        "device": "cpu",
        "torch_threads": 1,
        "deterministic_algorithms": True,
        "free_threaded": False,
    }


def _lock():
    return {
        "schema": "phase11-public-riichi-wait-readout-v1/execution-lock",
        "role": "FROZEN_E160_PUBLIC_RIICHI_STRUCTURAL_WAIT_READOUT",
        "retained": retained_value(),
        "retained_runtime": _runtime(),
        "provenance": {
            "source_revisions": {
                "lisjong": "99a30c267a3c3e301e132c8799726eb10e012a95",
                "lisjong_engine": "8735e89e1aea000ab59368d0368d476787827741",
                "lisjong_arena": "3" * 40,
            },
            "fully_resolved": True,
            "effective_rules": RULES,
            "anchor_semantics_id": "turn-pre-action-frozen-anchor-v1",
            "evidence_cutoff_semantics_id": "anchor-time-round-evidence-prefix-v1",
            "label_semantics_id": "exact-concealed-count-red-structural-wait-v1",
        },
        "runtime": _runtime(),
        "representation": representation_value(),
        "readout": readout_value(),
        "training": training_value(),
        "evaluation": evaluation_value(),
        "artifact_audit": "Issue #172 retained audit fixture",
        "result_exposed": False,
        "execution_decision": "ONE-SHOT LOCAL EXECUTION AFTER MERGE",
    }


def _target(
    wind: str,
    *,
    established: bool = True,
    available: bool = True,
    positive: int = 0,
):
    mask = (
        None if not available else tuple(int(index == positive) for index in range(34))
    )
    return OpponentTarget(
        wind=wind,
        seat={"south": 1, "west": 2, "north": 3}[wind],
        established=established,
        mask=mask,
        unavailable_reason=None if available else "unstable_hand_size",
        riichi_junme=4 if established else None,
        open_closed="closed",
    )


def _record(partition, seed, targets):
    return LatentExample(
        partition=partition,
        source_class="first-party-bootstrap",
        game_seed=seed,
        round_index=0,
        anchor_identity=f"anchor-{partition.value}-{seed}",
        depth=1,
        opponent_winds=("south", "west", "north"),
        latent=None,
        targets=targets,
    )


def _records(validation_hanchan=8):
    train = (
        _record(
            DatasetPartition.TRAIN,
            360,
            (
                _target("south", positive=0),
                _target("west", positive=1),
                _target("north", available=False),
            ),
        ),
    )
    validation = tuple(
        _record(
            DatasetPartition.VALIDATION,
            seed,
            (
                _target("south", positive=seed % 34),
                _target("west", established=False),
                _target("north", available=False),
            ),
        )
        for seed in range(424, 424 + validation_hanchan)
    )
    return train + validation


VALIDATION_HANCHAN = 16
VALIDATION_ROWS = 2447


def _order_example():
    """Accumulate one fixed cell population into the six #172 groupings.

    Every grouping receives the identical per-cell ``float`` contributions
    through ``evaluation._add``; only the accumulation order differs, exactly
    as in the executed run. Baseline probabilities stay far below 0.1, so the
    baseline reliability grouping collapses into one bin that sums all 83,198
    cells sequentially, which is the grouping that failed the old fixed
    ``abs_tol=1e-9`` comparison.
    """
    generator = random.Random(209)
    games = [_empty_aggregate() for _ in range(VALIDATION_HANCHAN)]
    tiles = [_empty_aggregate() for _ in range(TILE_KIND_COUNT)]
    reliability = {
        "baseline": [_empty_aggregate() for _ in range(10)],
        "readout": [_empty_aggregate() for _ in range(10)],
    }
    subgroups = {
        "riichi_junme": defaultdict(_empty_aggregate),
        "seat": defaultdict(_empty_aggregate),
        "open_closed": defaultdict(_empty_aggregate),
    }
    per_tile_positives = [0] * TILE_KIND_COUNT
    for row_index in range(VALIDATION_ROWS):
        game = row_index % VALIDATION_HANCHAN
        junme = str(1 + row_index % 9)
        seat = str(1 + row_index % 3)
        open_closed = "closed" if row_index % 4 else "open"
        for tile_index in range(TILE_KIND_COUNT):
            y = int(generator.random() < 0.09)
            baseline_p = (generator.randint(150, 600) + 0.5) / 9800.0
            logit = generator.uniform(-6.0, 2.0)
            readout_p = 1.0 / (1.0 + math.exp(-logit))
            per_tile_positives[tile_index] += y
            for aggregate in (
                games[game],
                tiles[tile_index],
                reliability["baseline"][min(int(baseline_p * 10), 9)],
                reliability["readout"][min(int(readout_p * 10), 9)],
                subgroups["riichi_junme"][junme],
                subgroups["seat"][seat],
                subgroups["open_closed"][open_closed],
            ):
                _add(aggregate, y, baseline_p, readout_p, logit)
    identities = [
        {"source_class": "first-party-bootstrap", "game_seed": 424 + index}
        for index in range(VALIDATION_HANCHAN)
    ]
    evidence = {
        "finite_probabilities": True,
        "probability_range_valid": True,
        "per_hanchan": [
            {**identity_row, **row}
            for identity_row, row in zip(identities, games, strict=True)
        ],
        "per_tile": [{"tile_index": index, **row} for index, row in enumerate(tiles)],
        "reliability": {
            name: _reliability_rows(rows) for name, rows in reliability.items()
        },
        "subgroups": {
            name: [{"group": key, **row} for key, row in sorted(groups.items())]
            for name, groups in subgroups.items()
        },
    }
    coverage = {
        "eligible_hanchan": VALIDATION_HANCHAN,
        "eligible_hanchan_identities": identities,
        "eligible_rows": VALIDATION_ROWS,
        "per_tile_positives": per_tile_positives,
    }
    return evidence, coverage


class AggregateNumericStabilityTest(unittest.TestCase):
    """Issue #209: equivalent aggregates differ only by summation order."""

    @classmethod
    def setUpClass(cls):
        cls.evidence, cls.coverage = _order_example()

    def _grouping_rows(self, name):
        if name == "per_tile":
            return self.evidence["per_tile"]
        if name in self.evidence["reliability"]:
            return self.evidence["reliability"][name]
        return self.evidence["subgroups"][name]

    def _envelope(self, name, field):
        expected = _aggregate_totals(self.evidence["per_hanchan"])
        actual = _aggregate_totals(self._grouping_rows(name))
        return actual[field][1] + expected[field][1]

    def test_the_old_fixed_absolute_tolerance_rejects_equivalent_evidence(self):
        reference = sum(
            row["baseline_logloss_sum"] for row in self.evidence["per_hanchan"]
        )
        regrouped = sum(
            row["baseline_logloss_sum"]
            for row in self.evidence["reliability"]["baseline"]
        )
        self.assertEqual(
            self.evidence["reliability"]["baseline"][0]["cells"],
            VALIDATION_ROWS * TILE_KIND_COUNT,
        )
        self.assertGreater(abs(reference - regrouped), 1e-9)
        self.assertFalse(math.isclose(reference, regrouped, rel_tol=0, abs_tol=1e-9))

    def test_derived_bound_accepts_the_same_evidence_in_a_different_order(self):
        self.assertIsInstance(
            _validate_evaluation_evidence(self.evidence, self.coverage), dict
        )

    def test_the_accepted_window_stays_a_few_parts_in_a_trillion(self):
        totals = _aggregate_totals(self.evidence["per_hanchan"])
        for field in ("baseline_logloss_sum", "readout_logloss_sum"):
            total, envelope = totals[field]
            self.assertLess(envelope / total, 1e-10)

    def test_error_bound_is_derived_from_term_count_and_magnitude_only(self):
        self.assertEqual(_sequential_sum_error_bound(1.0, 0), 0.0)
        self.assertEqual(_sequential_sum_error_bound(1.0, 1), 0.0)
        self.assertEqual(_sequential_sum_error_bound(0.0, 10_000), 0.0)
        for terms in (2, 1_000, 83_198):
            self.assertAlmostEqual(
                _sequential_sum_error_bound(4.0, terms),
                4.0 * _sequential_sum_error_bound(1.0, terms),
                places=18,
            )
        self.assertLess(
            _sequential_sum_error_bound(1.0, 1_000),
            _sequential_sum_error_bound(1.0, 83_198),
        )
        with self.assertRaises(Phase11Error):
            _sequential_sum_error_bound(1.0, 2**53)

    def test_no_fixed_epsilon_remains_in_aggregate_validation(self):
        source = Path(
            "src/lisjong_arena/phase11_public_riichi_wait_readout/result.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("abs_tol", source)
        self.assertNotIn("rel_tol", source)
        self.assertNotIn("1e-9", source)
        self.assertNotIn("1e-6", source)

    def test_corruption_beyond_the_derived_bound_is_rejected(self):
        for name in (
            "per_tile",
            "baseline",
            "readout",
            "riichi_junme",
            "seat",
            "open_closed",
        ):
            for field in ("baseline_logloss_sum", "readout_brier_sum"):
                with self.subTest(grouping=name, field=field):
                    window = self._envelope(name, field)
                    self.assertGreater(window, 0.0)
                    evidence = copy.deepcopy(self.evidence)
                    if name == "per_tile":
                        row = evidence["per_tile"][7]
                    elif name in evidence["reliability"]:
                        row = next(
                            item
                            for item in evidence["reliability"][name]
                            if item["cells"]
                        )
                    else:
                        row = evidence["subgroups"][name][0]
                    row[field] += 2.0 * window
                    with self.assertRaises(Phase11Error):
                        _validate_evaluation_evidence(evidence, self.coverage)

    def test_integer_evidence_stays_an_exact_comparison(self):
        evidence = copy.deepcopy(self.evidence)
        row = next(item for item in evidence["reliability"]["readout"] if item["cells"])
        row["positives"] += 1
        with self.assertRaises(Phase11Error):
            _validate_evaluation_evidence(evidence, self.coverage)
        evidence = copy.deepcopy(self.evidence)
        evidence["subgroups"]["seat"][0]["cells"] += 1
        with self.assertRaises(Phase11Error):
            _validate_evaluation_evidence(evidence, self.coverage)
        totals = _aggregate_totals(self.evidence["per_hanchan"])
        for field in ("cells", "positives"):
            drifted = dict(totals)
            drifted[field] = totals[field] + 1
            with self.assertRaises(Phase11Error):
                _same_totals(drifted, totals, "exact integer")

    def test_a_moved_cell_is_still_rejected(self):
        evidence = copy.deepcopy(self.evidence)
        donor = evidence["subgroups"]["open_closed"][0]
        receiver = evidence["subgroups"]["open_closed"][1]
        moved = donor["baseline_logloss_sum"] / donor["cells"]
        donor["baseline_logloss_sum"] -= moved
        with self.assertRaises(Phase11Error):
            _validate_evaluation_evidence(evidence, self.coverage)
        receiver["baseline_logloss_sum"] += moved
        _validate_evaluation_evidence(evidence, self.coverage)


class ProtocolTest(unittest.TestCase):
    def test_exact_retained_identities_and_fixed_contract(self):
        retained = retained_value()
        self.assertEqual(
            retained["phase167_execution_lock_identity"],
            PHASE167_EXECUTION_LOCK_IDENTITY,
        )
        self.assertEqual(retained["phase167_result_identity"], PHASE167_RESULT_IDENTITY)
        self.assertEqual(retained["e160_weights_sha256"], E160_WEIGHTS_SHA256)
        self.assertEqual(retained["e160_selected_epoch"], E160_SELECTED_EPOCH)
        self.assertEqual((MAX_EPOCHS, PATIENCE), (160, 6))
        self.assertEqual((BOOTSTRAP_REPLICATES, BOOTSTRAP_SEED), (10_000, 148))
        self.assertEqual(BOOTSTRAP_ORDER_INDICES, (249, 9750))
        self.assertEqual(SELECTION_EXPOSURE, 4)
        self.assertIs(FORMAL_TEST, False)

    def test_readout_and_representation_are_exact(self):
        self.assertEqual(readout_value()["architecture"], [128, 64, 102])
        self.assertEqual(READOUT_OUTPUT_DIM, 3 * 34)
        representation = representation_value()
        self.assertEqual(representation["parameter_count"], 459_080)
        self.assertIn("next_latent", representation["latent_alignment"])
        self.assertEqual(
            representation["frozen_components"], ["recurrent", "expected-count head"]
        )
        self.assertEqual(
            training_value()["objective"],
            "unweighted BCEWithLogits over eligible target-tile cells",
        )

    def test_outcomes_are_exhaustive_and_lock_has_no_rescue(self):
        self.assertEqual(
            set(OUTCOMES),
            {
                "STOP / INVALID",
                "INSUFFICIENT RIICHI COVERAGE",
                "CLEAR READOUT SIGNAL",
                "CLEAR READOUT REGRESSION",
                "INCONCLUSIVE",
            },
        )
        evaluation = evaluation_value()
        self.assertIs(evaluation["no_rescue"], True)
        self.assertIs(evaluation["no_e320"], True)

    def test_json_boolean_is_not_accepted_as_an_integer_or_conversely(self):
        value = _lock()
        validate_lock(value)
        value["result_exposed"] = 0
        with self.assertRaises(Phase11Error):
            validate_lock(value)

    def test_readout_budget_bound_is_diagnostic_only(self):
        self.assertEqual(budget_diagnostics(160), ["READOUT BUDGET BOUND"])
        self.assertEqual(budget_diagnostics(159), [])


class EligibilityAndCoverageTest(unittest.TestCase):
    def test_established_only_and_unavailable_is_masked_not_zero_filled(self):
        records = _records()
        coverage = build_coverage(records, "a" * 64)
        train = coverage["partitions"]["train"]
        validation = coverage["partitions"]["validation"]
        self.assertEqual(
            (
                train["established_rows"],
                train["eligible_rows"],
                train["unavailable_rows"],
            ),
            (3, 2, 1),
        )
        self.assertEqual(validation["eligible_rows"], 8)
        self.assertEqual(validation["unavailable_rows"], 8)
        self.assertEqual(validation["positive_wait_cells"], 8)
        self.assertIs(records[0].targets[2].mask, None)

    def test_available_all_zero_is_audited_and_invalid(self):
        target = OpponentTarget("south", 1, True, (0,) * 34, None, 5, "closed")
        records = (
            _record(
                DatasetPartition.TRAIN, 360, (target, _target("west"), _target("north"))
            ),
            _record(
                DatasetPartition.VALIDATION,
                424,
                (_target("south"), _target("west"), _target("north")),
            ),
        )
        lock = _lock()
        coverage = build_coverage(records, identity(lock))
        self.assertIs(coverage["semantic_valid"], False)
        self.assertEqual(coverage["partitions"]["train"]["available_all_zero_rows"], 1)
        self.assertEqual(len(coverage["partitions"]["train"]["all_zero_audit"]), 1)
        identified = {**coverage, "coverage_identity": identity(coverage)}
        result = assemble_result(
            lock,
            identified,
            baseline=None,
            model_manifest=None,
            evaluation_evidence=None,
        )
        self.assertEqual(result["outcome"], "STOP / INVALID")

    def test_tampered_label_semantics_is_rejected(self):
        coverage = build_coverage(_records(), "a" * 64)
        coverage["label_semantics"] = "ron-legal-wait"
        with self.assertRaises(Phase11Error):
            validate_coverage(coverage, "a" * 64)

    def test_baseline_is_exact_jeffreys_and_rejects_validation_leakage(self):
        train = tuple(
            row for row in _records() if row.partition is DatasetPartition.TRAIN
        )
        baseline = fit_train_prevalence(train)
        self.assertEqual(baseline["eligible_rows"], 2)
        self.assertEqual(baseline["probabilities"][0], 1.5 / 3.0)
        self.assertEqual(baseline["probabilities"][2], 0.5 / 3.0)
        with self.assertRaises(Phase11Error):
            fit_train_prevalence(_records())

    def test_opponent_rows_map_by_wind_and_pending_none_are_excluded(self):
        meld_counts = (0,) * len(tuple(PublicMeldType))
        public = (
            SimpleNamespace(
                wind=EngineWind.SOUTH,
                riichi_status=PublicRiichiStatus.PENDING,
                riichi_declaration_discard_order=1,
                meld_kind_counts=meld_counts,
            ),
            SimpleNamespace(
                wind=EngineWind.WEST,
                riichi_status=PublicRiichiStatus.NONE,
                riichi_declaration_discard_order=0,
                meld_kind_counts=meld_counts,
            ),
            SimpleNamespace(
                wind=EngineWind.NORTH,
                riichi_status=PublicRiichiStatus.ESTABLISHED,
                riichi_declaration_discard_order=3,
                meld_kind_counts=meld_counts,
            ),
        )
        identities = {
            LisjongWind.SOUTH: SimpleNamespace(
                wind=LisjongWind.SOUTH, seat=LisjongSeat.SEAT_1
            ),
            LisjongWind.WEST: SimpleNamespace(
                wind=LisjongWind.WEST, seat=LisjongSeat.SEAT_2
            ),
            LisjongWind.NORTH: SimpleNamespace(
                wind=LisjongWind.NORTH, seat=LisjongSeat.SEAT_3
            ),
        }
        wait_rows = tuple(
            SimpleNamespace(
                identity=identities[wind],
                mask=(1,) + (0,) * 33,
                unavailable_reason=None,
            )
            for wind in (LisjongWind.NORTH, LisjongWind.SOUTH, LisjongWind.WEST)
        )
        expected_rows = tuple(
            SimpleNamespace(identity=identities[wind])
            for wind in (LisjongWind.WEST, LisjongWind.NORTH, LisjongWind.SOUTH)
        )
        discards = tuple(
            SimpleNamespace(
                seat=seat,
                discards=(SimpleNamespace(order=3, is_riichi_declaration=True),)
                if seat is EngineSeat.NORTH
                else (),
            )
            for seat in EngineSeat
        )
        step = SimpleNamespace(
            opponent_winds=(LisjongWind.SOUTH, LisjongWind.WEST, LisjongWind.NORTH),
            sample=SimpleNamespace(
                anchor=SimpleNamespace(
                    observation=SimpleNamespace(
                        discards=discards, dealer_seat=EngineSeat.EAST
                    )
                ),
                labels=SimpleNamespace(
                    structural_waits=wait_rows, expected_counts=expected_rows
                ),
            ),
        )
        with patch(
            "lisjong_arena.phase11_public_riichi_wait_readout.data.build_phase6_snapshot_feature",
            return_value=SimpleNamespace(opponents=public),
        ):
            targets = _targets_for_step(step)
        self.assertEqual(tuple(row.wind for row in targets), ("south", "west", "north"))
        self.assertEqual(
            tuple(row.established for row in targets), (False, False, True)
        )
        self.assertEqual(tuple(row.eligible for row in targets), (False, False, True))


class EvaluationAndResultTest(unittest.TestCase):
    def _evidence(self, delta):
        rows = []
        for seed in range(424, 432):
            cells = 34
            rows.append(
                {
                    "source_class": "first-party-bootstrap",
                    "game_seed": seed,
                    "cells": cells,
                    "positives": 1,
                    "baseline_logloss_sum": 0.7 * cells,
                    "readout_logloss_sum": (0.7 - delta) * cells,
                    "baseline_brier_sum": 0.2 * cells,
                    "readout_brier_sum": 0.19 * cells,
                }
            )
        return {"per_hanchan": rows}

    def test_signal_regression_and_inconclusive_branches(self):
        self.assertEqual(classify_interval(0.01, 0.02), CLEAR_SIGNAL)
        self.assertEqual(classify_interval(-0.02, -0.01), CLEAR_REGRESSION)
        self.assertEqual(classify_interval(-0.01, 0.01), INCONCLUSIVE)
        for delta, expected in (
            (0.01, CLEAR_SIGNAL),
            (-0.01, CLEAR_REGRESSION),
            (0.0, INCONCLUSIVE),
        ):
            self.assertEqual(
                paired_comparison(self._evidence(delta))["classification"], expected
            )

    def test_coverage_gate_and_strict_result_rederivation(self):
        lock = _lock()
        coverage_value = build_coverage(_records(validation_hanchan=7), identity(lock))
        coverage = {**coverage_value, "coverage_identity": identity(coverage_value)}
        result = assemble_result(
            lock,
            coverage,
            baseline=coverage["train_prevalence_baseline"],
            model_manifest=None,
            evaluation_evidence=None,
        )
        self.assertEqual(result["outcome"], INSUFFICIENT_COVERAGE)
        validate_result(result, lock)
        tampered = copy.deepcopy(result)
        tampered["outcome"] = INCONCLUSIVE
        with self.assertRaises(Phase11Error):
            validate_result(tampered, lock)


CONTINUATION_SOURCE = Path(
    "src/lisjong_arena/phase11_public_riichi_wait_readout/continuation.py"
)


def _continuation_lock():
    lock = _lock()
    lock["provenance"]["source_revisions"]["lisjong_arena"] = (
        SCIENTIFIC_EXECUTION_REVISION
    )
    return lock


def _fixture_manifest(lock, coverage_identity, weights):
    history = tuple(
        EpochMetrics(
            epoch=epoch,
            train_binary_log_loss=0.6 + epoch / 100,
            validation_binary_log_loss=0.5 + (epoch - 1) / 100,
        )
        for epoch in range(1, 8)
    )
    result = SimpleNamespace(
        selected_epoch=1,
        history=history,
        train_binary_log_loss=0.55,
        validation_binary_log_loss=0.5,
        optimizer_parameter_names=("0.weight", "0.bias", "2.weight", "2.bias"),
        frozen_digest_before="b" * 64,
        frozen_digest_after="b" * 64,
        training_wall_seconds=1.0,
    )
    manifest = model_manifest_without_weights(
        lock=lock,
        coverage_identity=coverage_identity,
        result=result,
        training_cpu_seconds=0.5,
    )
    manifest["weights_bytes"] = len(weights)
    manifest["weights_sha256"] = hashlib.sha256(weights).hexdigest()
    return manifest


def _fixture_artifact_root(directory):
    """Write a #172-shaped artifact root and the binding that pins it."""
    lock = _continuation_lock()
    root = Path(directory) / "issue-172"
    model = root / "readout-model"
    model.mkdir(parents=True)
    (root / "execution-lock.json").write_bytes(canonical_json_bytes(lock))
    coverage_value = build_coverage(_records(validation_hanchan=8), identity(lock))
    save_coverage(root / "coverage.json", coverage_value, identity(lock))
    weights = b"fixture readout weights"
    manifest = _fixture_manifest(lock, identity(coverage_value), weights)
    (model / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
    (model / WEIGHTS_FILENAME).write_bytes(weights)
    binding = ImmutableArtifactBinding(
        execution_lock_identity=identity(lock),
        coverage_file_sha256=hashlib.sha256(
            (root / "coverage.json").read_bytes()
        ).hexdigest(),
        coverage_identity=identity(coverage_value),
        model_manifest_sha256=hashlib.sha256(
            (model / MANIFEST_FILENAME).read_bytes()
        ).hexdigest(),
        readout_weights_sha256=manifest["weights_sha256"],
        selected_epoch=manifest["selected_epoch"],
        epochs_run=manifest["epochs_run"],
        frozen_e160_digest=manifest["frozen_digest_before"],
    )
    return root, binding, lock


def _fixture_result(root, lock):
    coverage = load_coverage(root / "coverage.json", identity(lock))
    baseline = coverage["train_prevalence_baseline"]
    validation = coverage["partitions"]["validation"]
    games = [_empty_aggregate() for _ in validation["eligible_hanchan_identities"]]
    tiles = [_empty_aggregate() for _ in range(TILE_KIND_COUNT)]
    reliability = {
        "baseline": [_empty_aggregate() for _ in range(10)],
        "readout": [_empty_aggregate() for _ in range(10)],
    }
    subgroups = {
        "riichi_junme": defaultdict(_empty_aggregate),
        "seat": defaultdict(_empty_aggregate),
        "open_closed": defaultdict(_empty_aggregate),
    }
    for row_index, (identity_row, game) in enumerate(
        zip(validation["eligible_hanchan_identities"], games, strict=True)
    ):
        positive_tile = identity_row["game_seed"] % TILE_KIND_COUNT
        for tile_index, baseline_p in enumerate(baseline["probabilities"]):
            y = int(tile_index == positive_tile)
            readout_p = baseline_p
            logit = math.log(readout_p / (1.0 - readout_p))
            for aggregate in (
                game,
                tiles[tile_index],
                reliability["baseline"][min(int(baseline_p * 10), 9)],
                reliability["readout"][min(int(readout_p * 10), 9)],
                subgroups["riichi_junme"][str(1 + row_index % 9)],
                subgroups["seat"][str(1 + row_index % 3)],
                subgroups["open_closed"]["closed" if row_index % 2 else "open"],
            ):
                _add(aggregate, y, baseline_p, readout_p, logit)
    evidence = {
        "finite_probabilities": True,
        "probability_range_valid": True,
        "per_hanchan": [
            {**identity_row, **row}
            for identity_row, row in zip(
                validation["eligible_hanchan_identities"], games, strict=True
            )
        ],
        "per_tile": [{"tile_index": index, **row} for index, row in enumerate(tiles)],
        "reliability": {
            name: _reliability_rows(rows) for name, rows in reliability.items()
        },
        "subgroups": {
            name: [{"group": key, **row} for key, row in sorted(groups.items())]
            for name, groups in subgroups.items()
        },
    }
    manifest = json.loads((root / "readout-model" / MANIFEST_FILENAME).read_bytes())
    return assemble_result(
        lock,
        coverage,
        baseline=baseline,
        model_manifest=manifest,
        evaluation_evidence=evidence,
    )


class ContinuationBindingTest(unittest.TestCase):
    """Issue #209: the continuation is pinned to already-produced artifacts."""

    def test_binding_records_the_executed_issue_172_artifacts(self):
        binding = PHASE11_ARTIFACT_BINDING
        self.assertEqual(
            binding.execution_lock_identity,
            "5e08169c96568d692b69070245a8e2a6da61b8b1b5d4a9d8069b5dbe0130ce74",
        )
        self.assertEqual(
            binding.coverage_file_sha256,
            "12d2c4f2c0b64d22701fc47754b2a5076b41fa18bd940084fc51155b0f4e1dfa",
        )
        self.assertEqual(
            binding.coverage_identity,
            "6d1cdf7696b9e3d5b1e0bbfc7a13f5b3958c8e839ce6e02420f864be0c32a310",
        )
        self.assertEqual(
            binding.model_manifest_sha256,
            "618f6d366a738c9cf99da8a5a022d8aae2ade4ba824abad3248a9a862f3dcb09",
        )
        self.assertEqual(
            binding.readout_weights_sha256,
            "91e8a4db8b8d7a664e22142070e2f581d922be8e373a5bb1d6be1c7c33852948",
        )
        self.assertEqual(binding.selected_epoch, 121)
        self.assertEqual(binding.epochs_run, 127)
        self.assertEqual(
            binding.frozen_e160_digest,
            "581f4d20138291ea7c6b22508105b2ac2ed40cc3b3668e680376f3b9adf0885e",
        )
        self.assertEqual(
            SCIENTIFIC_EXECUTION_REVISION,
            "93963d85f6201c714cb4fcf39d59e9e09c85766d",
        )
        self.assertLess(binding.selected_epoch, MAX_EPOCHS)
        self.assertEqual(binding.epochs_run - binding.selected_epoch, PATIENCE)

    def test_bound_artifacts_are_only_read(self):
        with TemporaryDirectory() as temporary:
            root, binding, lock = _fixture_artifact_root(temporary)
            files = sorted(path for path in root.rglob("*") if path.is_file())
            before = [path.read_bytes() for path in files]
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                bound = continuation.bind_immutable_artifacts(root)
            self.assertEqual(bound.lock, lock)
            self.assertEqual(
                bound.coverage["coverage_identity"], binding.coverage_identity
            )
            self.assertEqual(bound.manifest["selected_epoch"], 1)
            self.assertEqual(
                [path.read_bytes() for path in files],
                before,
                "the continuation must never rewrite the retained artifacts",
            )

    def test_every_bound_file_is_byte_checked(self):
        for name in (
            "execution-lock.json",
            "coverage.json",
            "readout-model/manifest.json",
            "readout-model/weights.pt",
        ):
            with self.subTest(artifact=name):
                with TemporaryDirectory() as temporary:
                    root, binding, _lock_value = _fixture_artifact_root(temporary)
                    target = root / name
                    target.write_bytes(target.read_bytes() + b" ")
                    with patch.object(
                        continuation, "PHASE11_ARTIFACT_BINDING", binding
                    ):
                        with self.assertRaises(Phase11Error):
                            continuation.bind_immutable_artifacts(root)

    def test_missing_artifact_root_and_files_fail_closed(self):
        with TemporaryDirectory() as temporary:
            root, binding, _lock_value = _fixture_artifact_root(temporary)
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                with self.assertRaises(Phase11Error):
                    continuation.bind_immutable_artifacts(root / "absent")
                (root / "readout-model" / WEIGHTS_FILENAME).unlink()
                with self.assertRaises(Phase11Error):
                    continuation.bind_immutable_artifacts(root)

    def test_a_lock_from_another_arena_revision_is_not_the_172_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "issue-172"
            model = root / "readout-model"
            model.mkdir(parents=True)
            lock = _lock()
            (root / "execution-lock.json").write_bytes(canonical_json_bytes(lock))
            coverage_value = build_coverage(
                _records(validation_hanchan=8), identity(lock)
            )
            save_coverage(root / "coverage.json", coverage_value, identity(lock))
            weights = b"fixture readout weights"
            manifest = _fixture_manifest(lock, identity(coverage_value), weights)
            (model / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
            (model / WEIGHTS_FILENAME).write_bytes(weights)
            binding = ImmutableArtifactBinding(
                execution_lock_identity=identity(lock),
                coverage_file_sha256=hashlib.sha256(
                    (root / "coverage.json").read_bytes()
                ).hexdigest(),
                coverage_identity=identity(coverage_value),
                model_manifest_sha256=hashlib.sha256(
                    (model / MANIFEST_FILENAME).read_bytes()
                ).hexdigest(),
                readout_weights_sha256=manifest["weights_sha256"],
                selected_epoch=manifest["selected_epoch"],
                epochs_run=manifest["epochs_run"],
                frozen_e160_digest=manifest["frozen_digest_before"],
            )
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                with self.assertRaisesRegex(Phase11Error, "scientific execution"):
                    continuation.bind_immutable_artifacts(root)


class ContinuationRevisionTest(unittest.TestCase):
    def test_the_scientific_revision_is_never_the_continuation_revision(self):
        with self.assertRaisesRegex(Phase11Error, "repair commit"):
            continuation.require_locked_continuation_revision(
                SCIENTIFIC_EXECUTION_REVISION
            )

    def test_locked_revision_and_head_must_match_exactly(self):
        with patch.object(
            continuation, "require_clean_arena_head", return_value="c" * 40
        ) as clean:
            with self.assertRaises(Phase11Error):
                continuation.require_locked_continuation_revision("d" * 40)
        clean.assert_called_once()

    def test_a_dirty_head_stops_the_continuation(self):
        with patch.object(
            continuation,
            "require_clean_arena_head",
            side_effect=ExecutionSafetyError("Arena worktree must be clean"),
        ):
            with self.assertRaisesRegex(ExecutionSafetyError, "must be clean"):
                continuation.require_locked_continuation_revision("d" * 40)

    def test_an_unmerged_revision_stops_the_continuation(self):
        with (
            patch.object(
                continuation, "require_clean_arena_head", return_value="d" * 40
            ),
            patch.object(
                continuation,
                "require_merged_arena_revision",
                side_effect=ExecutionSafetyError("not contained in main"),
            ),
        ):
            with self.assertRaisesRegex(ExecutionSafetyError, "not contained"):
                continuation.require_locked_continuation_revision("d" * 40)

    def test_a_short_or_uppercase_revision_is_rejected(self):
        for value in ("D" * 40, "d" * 39, "", None):
            with self.subTest(revision=value):
                with self.assertRaises(Phase11Error):
                    continuation.require_locked_continuation_revision(value)

    def test_prepare_locks_the_current_clean_merged_revision(self):
        with (
            patch.object(
                continuation, "require_clean_arena_head", return_value="d" * 40
            ),
            patch.object(
                continuation, "require_merged_arena_revision", return_value="d" * 40
            ) as merged,
        ):
            self.assertEqual(continuation.current_continuation_revision(), "d" * 40)
        merged.assert_called_once()

    def test_later_main_revision_is_not_adopted_after_lock(self):
        with (
            patch.object(
                continuation, "require_clean_arena_head", return_value="e" * 40
            ),
            patch.object(continuation, "require_merged_arena_revision") as merged,
        ):
            with self.assertRaisesRegex(Phase11Error, "locked technical"):
                continuation.require_locked_continuation_revision("d" * 40)
        merged.assert_not_called()


class ContinuationLockTest(unittest.TestCase):
    def _lock_value(self, temporary):
        root, binding, lock = _fixture_artifact_root(temporary)
        with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
            bound = continuation.bind_immutable_artifacts(root)
            value = continuation.continuation_lock(
                bound=bound,
                continuation_revision="d" * 40,
                dependencies={
                    "runtime": _runtime(),
                    "provenance": {
                        "source_revisions": {
                            **lock["provenance"]["source_revisions"],
                            "lisjong_arena": "d" * 40,
                        }
                    },
                },
                result_identity="e" * 64,
                prepared_result_sha256="f" * 64,
                prepared_result_bytes=123,
                continuation_audit="Issue #209 continuation recorded 2026-09-12",
            )
        return root, binding, value

    def test_lock_is_write_once_and_round_trips(self):
        with TemporaryDirectory() as temporary:
            root, binding, value = self._lock_value(temporary)
            destination = Path(temporary) / "continuation-lock.json"
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                continuation.save_continuation_lock(destination, value)
                loaded = continuation.load_continuation_lock(destination)
                self.assertEqual(loaded["result_identity"], "e" * 64)
                self.assertEqual(
                    loaded["scientific_execution_revision"],
                    SCIENTIFIC_EXECUTION_REVISION,
                )
                self.assertEqual(loaded["technical_continuation_revision"], "d" * 40)
                self.assertFalse(loaded["retrained"])
                with self.assertRaises(FileExistsError):
                    continuation.save_continuation_lock(destination, value)
            self.assertTrue(root.is_dir())

    def test_lock_rejects_confused_revisions_and_training_claims(self):
        with TemporaryDirectory() as temporary:
            _root, binding, value = self._lock_value(temporary)
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                for field in ("retrained", "resumed", "checkpoint_reselected"):
                    tampered = copy.deepcopy(value)
                    tampered[field] = True
                    with self.assertRaises(Phase11Error):
                        continuation.validate_continuation_lock(tampered)
                tampered = copy.deepcopy(value)
                tampered["technical_continuation_revision"] = (
                    SCIENTIFIC_EXECUTION_REVISION
                )
                with self.assertRaises(Phase11Error):
                    continuation.validate_continuation_lock(tampered)
                tampered = copy.deepcopy(value)
                tampered["locked_source_revisions"]["lisjong"] = "f" * 40
                with self.assertRaises(Phase11Error):
                    continuation.validate_continuation_lock(tampered)
                tampered = copy.deepcopy(value)
                tampered["continuation_audit"] = "   "
                with self.assertRaises(Phase11Error):
                    continuation.validate_continuation_lock(tampered)
                tampered = copy.deepcopy(value)
                del tampered["result_identity"]
                with self.assertRaises(Phase11Error):
                    continuation.validate_continuation_lock(tampered)

    def test_a_tampered_lock_file_is_rejected(self):
        with TemporaryDirectory() as temporary:
            _root, binding, value = self._lock_value(temporary)
            destination = Path(temporary) / "continuation-lock.json"
            with patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding):
                continuation.save_continuation_lock(destination, value)
                payload = json.loads(destination.read_bytes())
                payload["retrained"] = True
                destination.write_bytes(canonical_json_bytes(payload))
                with self.assertRaises(Phase11Error):
                    continuation.load_continuation_lock(destination)


class ContinuationRecoveryTest(unittest.TestCase):
    def _prepare(self, temporary):
        root, binding, scientific_lock = _fixture_artifact_root(temporary)
        result = _fixture_result(root, scientific_lock)
        dependencies = {
            "runtime": _runtime(),
            "provenance": {
                "source_revisions": {
                    **scientific_lock["provenance"]["source_revisions"],
                    "lisjong_arena": "d" * 40,
                }
            },
        }
        with (
            patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding),
            patch.object(
                continuation, "current_continuation_revision", return_value="d" * 40
            ),
            patch.object(
                continuation,
                "require_locked_dependencies",
                return_value=dependencies,
            ),
            patch.object(
                continuation, "_prospective_result", return_value=result
            ) as evaluate,
        ):
            prepared = continuation.prepare_continuation(
                artifact_root=root,
                corpus_root="corpus",
                phase157_root="phase157",
                phase167_root="phase167",
                continuation_audit="Issue #209 continuation recorded 2026-09-12",
            )
        evaluate.assert_called_once()
        return root, binding, dependencies, prepared

    def test_interruption_after_lock_can_recover_with_result_write_only(self):
        with TemporaryDirectory() as temporary:
            root, binding, dependencies, prepared = self._prepare(temporary)
            self.assertFalse((root / "result.json").exists())
            self.assertTrue(
                (root / "continuation" / "continuation-lock.json").is_file()
            )
            with (
                patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding),
                patch.object(
                    continuation,
                    "require_locked_continuation_revision",
                    return_value="d" * 40,
                ),
                patch.object(
                    continuation,
                    "require_locked_dependencies",
                    return_value=dependencies,
                ),
                patch.object(
                    continuation,
                    "_prospective_result",
                    side_effect=AssertionError("evaluation must not rerun"),
                ),
                patch.object(
                    continuation,
                    "_publish_new_file",
                    side_effect=OSError("simulated interruption before result publish"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    continuation.expose_result(artifact_root=root)
            self.assertFalse((root / "result.json").exists())
            with (
                patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding),
                patch.object(
                    continuation,
                    "require_locked_continuation_revision",
                    return_value="d" * 40,
                ),
                patch.object(
                    continuation,
                    "require_locked_dependencies",
                    return_value=dependencies,
                ),
                patch.object(
                    continuation,
                    "_prospective_result",
                    side_effect=AssertionError("evaluation must not rerun"),
                ) as evaluate,
            ):
                exposed = continuation.expose_result(artifact_root=root)
            evaluate.assert_not_called()
            self.assertEqual(exposed["result_identity"], prepared["result_identity"])
            self.assertTrue((root / "result.json").is_file())

    def test_second_result_exposure_is_rejected_before_any_work(self):
        with TemporaryDirectory() as temporary:
            root, binding, dependencies, _prepared = self._prepare(temporary)
            with (
                patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding),
                patch.object(
                    continuation,
                    "require_locked_continuation_revision",
                    return_value="d" * 40,
                ),
                patch.object(
                    continuation,
                    "require_locked_dependencies",
                    return_value=dependencies,
                ),
            ):
                continuation.expose_result(artifact_root=root)
            with patch.object(continuation, "load_continuation_lock") as load:
                with self.assertRaisesRegex(ExecutionSafetyError, "write-once"):
                    continuation.expose_result(artifact_root=root)
            load.assert_not_called()

    def test_tampered_lock_stops_before_artifact_loading(self):
        with TemporaryDirectory() as temporary:
            root, _binding, _dependencies, _prepared = self._prepare(temporary)
            lock_path = root / "continuation" / "continuation-lock.json"
            lock_path.write_bytes(lock_path.read_bytes() + b" ")
            with (
                patch.object(continuation, "bind_immutable_artifacts") as bind,
                patch.object(continuation, "_prospective_result") as evaluate,
            ):
                with self.assertRaises(Phase11Error):
                    continuation.expose_result(artifact_root=root)
            bind.assert_not_called()
            evaluate.assert_not_called()

    def test_precommitted_result_identity_mismatch_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root, binding, dependencies, _prepared = self._prepare(temporary)
            lock_path = root / "continuation" / "continuation-lock.json"
            payload = json.loads(lock_path.read_bytes())
            payload["result_identity"] = "a" * 64
            without_identity = {
                name: item
                for name, item in payload.items()
                if name != "continuation_lock_identity"
            }
            payload["continuation_lock_identity"] = identity(without_identity)
            lock_path.write_bytes(canonical_json_bytes(payload))
            with (
                patch.object(continuation, "PHASE11_ARTIFACT_BINDING", binding),
                patch.object(
                    continuation,
                    "require_locked_continuation_revision",
                    return_value="d" * 40,
                ),
                patch.object(
                    continuation,
                    "require_locked_dependencies",
                    return_value=dependencies,
                ),
                patch.object(continuation, "_publish_new_file") as publish,
            ):
                with self.assertRaisesRegex(Phase11Error, "precommitted"):
                    continuation.expose_result(artifact_root=root)
            publish.assert_not_called()


class ContinuationSurfaceTest(unittest.TestCase):
    def test_the_continuation_module_starts_without_runpy_reimport(self):
        import subprocess
        import sys

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lisjong_arena.phase11_public_riichi_wait_readout.continuation",
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("RuntimeWarning", completed.stderr)

    def test_the_continuation_never_reaches_the_training_implementation(self):
        source = CONTINUATION_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("training", imported)
        self.assertNotIn(".training", imported)
        for forbidden in (
            "train_readout",
            "create_readout_optimizer",
            "ReadoutTrainingConfig(",
            "torch.optim",
        ):
            self.assertNotIn(forbidden, source)

    def test_the_continuation_cli_offers_no_training_or_budget_knob(self):
        parser = continuation._parser()
        command = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command.choices),
            {"prepare", "expose"},
        )
        prepare_options = {
            option
            for action in command.choices["prepare"]._actions
            for option in action.option_strings
        }
        self.assertEqual(
            prepare_options,
            {
                "-h",
                "--help",
                "--corpus-root",
                "--phase157-root",
                "--phase167-root",
                "--artifact-root",
                "--continuation-audit",
            },
        )
        expose_options = {
            option
            for action in command.choices["expose"]._actions
            for option in action.option_strings
        }
        self.assertEqual(expose_options, {"-h", "--help", "--artifact-root"})
        help_text = parser.format_help().lower()
        for forbidden in ("epoch", "resume", "rescue", "hpo", "320", "seed", "train "):
            self.assertNotIn(forbidden, help_text)

    def test_an_existing_result_or_continuation_stops_prepare_before_any_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for existing in (root / "result.json", root / "continuation"):
                with self.subTest(existing=existing.name):
                    if existing.suffix:
                        existing.write_text("{}", encoding="utf-8")
                    else:
                        existing.mkdir()
                    with patch.object(
                        continuation, "current_continuation_revision"
                    ) as revision:
                        with self.assertRaisesRegex(ExecutionSafetyError, "write-once"):
                            continuation.prepare_continuation(
                                artifact_root=root,
                                corpus_root="corpus",
                                phase157_root="phase157",
                                phase167_root="phase167",
                                continuation_audit="Issue #209 continuation",
                            )
                    revision.assert_not_called()
                    if existing.is_dir():
                        existing.rmdir()
                    else:
                        existing.unlink()


class Issue209ProtocolInvariantTest(unittest.TestCase):
    """The #209 repair must not move any #172 scientific constant."""

    def test_locked_scientific_constants_are_unchanged(self):
        self.assertEqual(readout_value()["architecture"], [128, 64, 102])
        self.assertEqual(readout_value()["activation"], "ReLU")
        self.assertEqual(MAX_EPOCHS, 160)
        self.assertEqual(PATIENCE, 6)
        config = training_value()["config"]
        self.assertEqual(config["seed"], 0)
        self.assertEqual(config["dataloader_seed"], 0)
        self.assertEqual(config["learning_rate"], 1e-3)
        self.assertEqual(config["weight_decay"], 0.0)
        self.assertEqual(training_value()["optimizer"], "Adam")
        self.assertEqual(
            training_value()["checkpoint_selection"],
            "lowest VALIDATION binary log loss; earliest 1e-12 tie",
        )
        evaluation = evaluation_value()
        self.assertEqual(
            evaluation["baseline"]["formula"],
            "(positive(tile) + 0.5) / (eligible_rows + 1.0)",
        )
        self.assertEqual(evaluation["baseline"]["smoothing"], "Jeffreys")
        self.assertEqual(evaluation["coverage_minimum_validation_hanchan"], 8)
        self.assertEqual(evaluation["bootstrap"]["replicates"], 10_000)
        self.assertEqual(evaluation["bootstrap"]["seed"], 148)
        self.assertEqual(
            evaluation["bootstrap"]["order_statistic_indices"], [249, 9750]
        )
        self.assertEqual(
            evaluation["classification"],
            {
                "lower > 0": CLEAR_SIGNAL,
                "upper < 0": CLEAR_REGRESSION,
                "otherwise": INCONCLUSIVE,
            },
        )
        self.assertIs(evaluation["formal_test"], False)
        self.assertIs(FORMAL_TEST, False)
        self.assertEqual(evaluation["selection_exposure"], 4)
        retained = retained_value()
        self.assertEqual(retained["train_seeds"], list(range(360, 424)))
        self.assertEqual(retained["validation_seeds"], list(range(424, 440)))
        self.assertEqual(len(retained["train_seeds"]), 64)
        self.assertEqual(len(retained["validation_seeds"]), 16)

    def test_the_locked_dependency_revisions_are_the_172_ones(self):
        lock = _continuation_lock()
        self.assertEqual(
            lock["provenance"]["source_revisions"]["lisjong"],
            "99a30c267a3c3e301e132c8799726eb10e012a95",
        )
        self.assertEqual(
            lock["provenance"]["source_revisions"]["lisjong_engine"],
            "8735e89e1aea000ab59368d0368d476787827741",
        )
        self.assertEqual(lock["runtime"]["riichienv"], "0.4.8")
        self.assertEqual(lock["runtime"]["torch"], "2.13.0+cpu")
        self.assertTrue(lock["runtime"]["python"].startswith("3.14."))
        validate_lock(lock)


class CliContractTest(unittest.TestCase):
    def test_normal_import_and_plan_are_torch_free(self):
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from lisjong_arena.phase11_public_riichi_wait_readout.__main__ import _plan\n"
            "_plan()\n"
            "raise SystemExit(1 if 'torch' in sys.modules else 0)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(completed.returncode, 0)

    def test_cli_has_no_resume_hpo_rescue_or_budget_override(self):
        from lisjong_arena.phase11_public_riichi_wait_readout.__main__ import _parser

        parser = _parser()
        command = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command.choices),
            {"plan", "verify", "lock", "preflight", "train", "evaluate"},
        )
        for subparser in command.choices.values():
            options = {
                option
                for action in subparser._actions
                for option in action.option_strings
            }
            for forbidden in (
                "--resume",
                "--max-epochs",
                "--epochs",
                "--hpo",
                "--rescue",
                "--learning-rate",
                "--seed",
            ):
                self.assertNotIn(forbidden, options)
        help_text = parser.format_help().lower()
        self.assertNotIn("320", help_text)


if __name__ == "__main__":
    unittest.main()
