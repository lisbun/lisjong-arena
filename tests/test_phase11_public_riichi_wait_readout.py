"""Non-ML contract tests for Arena #172."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policy_contract import Seat as LisjongSeat
from lisjong.policy_contract import Wind as LisjongWind
from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.wind import Wind as EngineWind

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_public_riichi_wait_readout.coverage import (
    build_coverage,
    validate_coverage,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    LatentExample,
    OpponentTarget,
    _targets_for_step,
)
from lisjong_arena.phase11_public_riichi_wait_readout.evaluation import (
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
    Phase11Error,
    evaluation_value,
    identity,
    readout_value,
    representation_value,
    retained_value,
    training_value,
)
from lisjong_arena.phase11_public_riichi_wait_readout.result import (
    assemble_result,
    budget_diagnostics,
    validate_result,
)
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
