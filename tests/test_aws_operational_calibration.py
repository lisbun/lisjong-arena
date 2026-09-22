from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import _aws_operational_calibration_fixtures as fixtures

from lisjong_arena import aws_operational_calibration as calibration
from lisjong_arena.durable_seed_checkpoint import publish_seed_checkpoint


def _gate(record, name):
    for gate in record["gates"]:
        if gate["name"] == name:
            return gate
    raise AssertionError(f"missing gate {name!r}")


def _admit(requirement=None, evidence_documents=None, now=fixtures.NOW):
    return calibration.build_phase_one_admission(
        fixtures.requirement() if requirement is None else requirement,
        [fixtures.evidence()] if evidence_documents is None else evidence_documents,
        now=now,
    )


class CalibrationEvidenceTest(unittest.TestCase):
    def test_valid_matching_calibration_admits_phase_one(self):
        record = _admit()
        self.assertEqual("GO", record["decision"])
        self.assertEqual([], record["blocking_reasons"])
        self.assertTrue(record["billable_resource_creation_authorized"])
        self.assertFalse(record["scientific_submission_authorized"])
        self.assertEqual(
            calibration.PHASE_ONE_GATE_NAMES,
            tuple(gate["name"] for gate in record["gates"]),
        )
        self.assertTrue(all(g["status"] == "PASS" for g in record["gates"]))
        self.assertEqual(
            fixtures.evidence()["calibration_identity"],
            record["calibration"]["identity"],
        )
        calibration.validate_admission_record(record)

    def test_p50_p90_are_nearest_rank_order_statistics(self):
        self.assertEqual(
            50.0, calibration.nearest_rank_percentile(range(10, 101, 10), 50)
        )
        self.assertEqual(
            90.0, calibration.nearest_rank_percentile(range(10, 101, 10), 90)
        )
        self.assertEqual(10.0, calibration.nearest_rank_percentile([10.0], 90))
        started = fixtures.CALIBRATION_START
        varied = [
            {
                "seed": 900_000 + index,
                "started_at": started.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "completed_at": (started + timedelta(seconds=10 * (index + 1)))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            for index in range(10)
        ]
        evidence = fixtures.evidence(
            tasks=varied,
            worker_count_requested=10,
            workers_active_observed=10,
            batch_scientific_wall_clock_seconds=110.0,
        )
        self.assertEqual(50.0, evidence["p50_seconds_per_unit"])
        self.assertEqual(90.0, evidence["p90_seconds_per_unit"])
        self.assertEqual(10, evidence["peak_observed_concurrency"])
        self.assertEqual(
            round(10 * 3600 / 110.0, 6), evidence["throughput_units_per_hour"]
        )
        self.assertIn(
            "not a guaranteed statistical bound", evidence["percentile_method"]
        )

    def test_evidence_round_trips_and_fails_closed_when_tampered(self):
        evidence = fixtures.evidence()
        self.assertEqual(evidence, calibration.validate_calibration_evidence(evidence))
        tampered = dict(evidence)
        tampered["p90_seconds_per_unit"] = 1.0
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.validate_calibration_evidence(tampered)
        renamed = dict(evidence)
        renamed["calibration_identity"] = "f" * 64
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.validate_calibration_evidence(renamed)

    def test_scientific_runtime_and_billable_runtime_stay_separate(self):
        evidence = fixtures.evidence()
        self.assertLess(
            evidence["batch_scientific_wall_clock_seconds"],
            evidence["ec2_billable_runtime_seconds"],
        )
        record = _admit()
        billable = record["cost_prediction"][
            "predicted_ec2_billable_runtime_range_seconds"
        ]
        runtime = record["runtime_prediction"]
        self.assertGreater(billable[0], runtime["predicted_lower_seconds"])
        self.assertGreater(billable[1], runtime["headroom_adjusted_upper_seconds"])
        self.assertEqual(
            "scientific workload start",
            record["clock_origins"]["predicted_scientific_runtime_seconds"],
        )
        self.assertEqual(
            "instance-side fail-safe arm epoch",
            record["clock_origins"]["hard_fail_safe_seconds"],
        )

    def test_billable_runtime_shorter_than_its_decomposition_fails_closed(self):
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            fixtures.evidence(
                ec2_billable_runtime_seconds=1000.0,
                setup_overhead_seconds=900.0,
                teardown_overhead_seconds=400.0,
            )


class CalibrationMatchingTest(unittest.TestCase):
    def test_insufficient_task_count_is_no_go(self):
        record = _admit(
            requirement=fixtures.requirement(
                calibration_policy={"minimum_tasks_per_worker": 2.0}
            )
        )
        self.assertEqual("NO-GO", record["decision"])
        gate = _gate(record, "matching-calibration")
        self.assertEqual("FAIL", gate["status"])
        self.assertIn("task-count-sufficient", gate["detail"])
        self.assertIn("at least 32 required", gate["detail"])
        self.assertFalse(record["billable_resource_creation_authorized"])

    def test_concurrency_not_exercised_is_no_go(self):
        started = fixtures.CALIBRATION_START
        serialized = [
            {
                "seed": 900_000 + index,
                "started_at": (started + timedelta(seconds=60 * index))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "completed_at": (started + timedelta(seconds=60 * (index + 1)))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            for index in range(20)
        ]
        evidence = fixtures.evidence(
            tasks=serialized,
            batch_scientific_wall_clock_seconds=1205.0,
            ec2_billable_runtime_seconds=3600.0,
        )
        self.assertEqual(1, evidence["peak_observed_concurrency"])
        record = _admit(evidence_documents=[evidence])
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "concurrency-exercised", _gate(record, "matching-calibration")["detail"]
        )

    def test_worker_mismatch_is_no_go_and_names_supported_worker_counts(self):
        evidence = fixtures.evidence(
            tasks=fixtures.tasks(count=16, workers=8),
            worker_count_requested=8,
            workers_active_observed=8,
            batch_scientific_wall_clock_seconds=125.0,
        )
        record = _admit(evidence_documents=[evidence])
        self.assertEqual("NO-GO", record["decision"])
        gate = _gate(record, "matching-calibration")
        self.assertIn("worker-count-exact", gate["detail"])
        self.assertIn("never extrapolated", gate["detail"])
        self.assertEqual([8], record["calibration"]["supported_worker_counts"])
        self.assertFalse(record["runtime_prediction"]["available"])
        self.assertIn("supported worker counts", record["runtime_prediction"]["reason"])

    def test_low_concurrency_small_batch_never_supports_a_large_worker_count(self):
        # A 4-hanchan, 2-worker observation is exactly the shape Issue #340
        # forbids extrapolating to a 16-worker production run.
        evidence = fixtures.evidence(
            tasks=fixtures.tasks(count=4, workers=2),
            worker_count_requested=2,
            workers_active_observed=2,
            batch_scientific_wall_clock_seconds=125.0,
        )
        record = _admit(evidence_documents=[evidence])
        self.assertEqual("NO-GO", record["decision"])
        self.assertEqual([], record["calibration"]["supported_worker_counts"])
        self.assertIsNone(record["calibration"]["identity"])

    def test_stale_calibration_is_no_go(self):
        record = _admit(
            now=fixtures.CALIBRATED_AT + timedelta(seconds=2_592_001),
        )
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn("freshness", _gate(record, "matching-calibration")["detail"])

    def test_calibration_from_the_future_is_no_go(self):
        record = _admit(now=fixtures.CALIBRATED_AT - timedelta(seconds=60))
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn("freshness", _gate(record, "matching-calibration")["detail"])

    def test_arena_revision_mismatch_is_no_go(self):
        record = _admit(evidence_documents=[fixtures.evidence(arena_revision="9" * 40)])
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "identity:arena_revision", _gate(record, "matching-calibration")["detail"]
        )

    def test_dependency_and_runtime_mismatch_is_no_go(self):
        for field, value in (
            ("lisjong_revision", "9" * 40),
            ("lisjong_engine_revision", "9" * 40),
            ("riichienv_version", "0.4.9"),
            ("instance_type", "c7i.8xlarge"),
            ("teacher_identity", "SomeOtherPolicy x4"),
            ("game_mode", "4p-red-east"),
            ("workload_identity", "other-workload"),
            ("instrumentation_identity", "other-instrumentation/v1"),
        ):
            with self.subTest(field=field):
                overrides = {field: value}
                if field == "instance_type":
                    overrides["vcpu"] = 32
                record = _admit(evidence_documents=[fixtures.evidence(**overrides)])
                self.assertEqual("NO-GO", record["decision"])
                self.assertIn(
                    f"identity:{field}",
                    _gate(record, "matching-calibration")["detail"],
                )

    def test_incomplete_calibration_is_no_go(self):
        for field in (
            "ec2_billable_runtime_seconds",
            "setup_overhead_seconds",
            "teardown_overhead_seconds",
        ):
            with self.subTest(field=field):
                evidence = fixtures.evidence(**{field: None})
                self.assertIn(field, evidence["unavailable_fields"])
                record = _admit(evidence_documents=[evidence])
                self.assertEqual("NO-GO", record["decision"])
                self.assertIn(
                    "completeness", _gate(record, "matching-calibration")["detail"]
                )

    def test_no_calibration_at_all_is_no_go(self):
        record = _admit(evidence_documents=[])
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "no calibration evidence was supplied",
            _gate(record, "matching-calibration")["detail"],
        )

    def test_weaker_durable_evidence_level_is_no_go(self):
        record = _admit(
            requirement=fixtures.requirement(
                target={"durable_evidence_level": "per-seed-durable-receipt"}
            )
        )
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "durable-evidence-level", _gate(record, "matching-calibration")["detail"]
        )

    def test_burstable_family_requires_an_explicit_sustained_basis(self):
        target = {"instance_type": "t3.small", "vcpu": 2, "worker_count": 2}
        without = fixtures.evidence(
            instance_type="t3.small",
            vcpu=2,
            worker_count_requested=2,
            workers_active_observed=2,
            tasks=fixtures.tasks(count=8, workers=2),
            batch_scientific_wall_clock_seconds=245.0,
        )
        record = _admit(
            requirement=fixtures.requirement(target=target),
            evidence_documents=[without],
        )
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "burstable-sustained-basis",
            _gate(record, "matching-calibration")["detail"],
        )
        with_basis = fixtures.evidence(
            instance_type="t3.small",
            vcpu=2,
            worker_count_requested=2,
            workers_active_observed=2,
            tasks=fixtures.tasks(count=8, workers=2),
            batch_scientific_wall_clock_seconds=245.0,
            burstable_sustained_basis="unlimited mode disabled; baseline-only "
            "throughput observed for the full window with surplus charges priced",
        )
        record = _admit(
            requirement=fixtures.requirement(target=target),
            evidence_documents=[with_basis],
        )
        self.assertEqual("GO", record["decision"])

    def test_calibration_seeds_must_be_a_dedicated_non_production_allocation(self):
        shared = fixtures.evidence(
            seed_allocation={
                "allocation_identity": fixtures.PRODUCTION_ALLOCATION_IDENTITY,
                "ledger_revision": fixtures.LEDGER_REVISION,
                "owner_repository": "lisbun/lisjong-arena",
                "seed_domain": "riichienv-4p-red-half-hanchan-v1",
                "seed_membership_identity": fixtures.PRODUCTION_MEMBERSHIP_IDENTITY,
            }
        )
        record = _admit(evidence_documents=[shared])
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "calibration-seed-isolation",
            _gate(record, "matching-calibration")["detail"],
        )
        scientific_population = fixtures.evidence(
            seed_allocation_population="offense-foundation"
        )
        record = _admit(evidence_documents=[scientific_population])
        self.assertEqual("NO-GO", record["decision"])
        self.assertIn(
            "calibration-seed-isolation",
            _gate(record, "matching-calibration")["detail"],
        )

    def test_unrelated_ledger_revision_movement_does_not_make_calibration_stale(self):
        # Seed ledger revision travels with the evidence for audit, but an
        # unrelated reservation must not invalidate a matching calibration.
        evidence = fixtures.evidence(seed_ledger_revision="9" * 64)
        record = _admit(evidence_documents=[evidence])
        self.assertEqual("GO", record["decision"])


class RuntimeAndCostBoundaryTest(unittest.TestCase):
    def test_runtime_prediction_never_divides_a_unit_duration_by_workers(self):
        runtime = calibration.predict_runtime(
            fixtures.evidence(),
            total_units=20,
            worker_count=16,
            headroom_factor=1.5,
        )
        self.assertEqual(125.0, runtime["unit_scaled_lower_seconds"])
        self.assertEqual(2, runtime["waves"])
        self.assertEqual(120.0, runtime["wave_tail_upper_seconds"])
        self.assertEqual(125.0, runtime["predicted_upper_seconds"])
        self.assertEqual(187.5, runtime["headroom_adjusted_upper_seconds"])
        self.assertIn(fixtures.evidence()["calibration_identity"], runtime["basis"])

    def test_runtime_prediction_refuses_a_different_worker_count(self):
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.predict_runtime(
                fixtures.evidence(),
                total_units=20,
                worker_count=32,
                headroom_factor=1.5,
            )

    def test_runtime_headroom_boundary(self):
        for budget_seconds, expected in ((187.5, "PASS"), (187.49, "FAIL")):
            with self.subTest(budget_seconds=budget_seconds):
                record = _admit(
                    requirement=fixtures.requirement(
                        budget={"execution_budget_seconds": budget_seconds}
                    )
                )
                self.assertEqual(expected, _gate(record, "runtime-headroom")["status"])

    def test_normal_deadline_boundary(self):
        # setup 1200 + headroom-adjusted 187.5 + post-processing 1200
        for deadline, expected in ((2587.5, "PASS"), (2587.49, "FAIL")):
            with self.subTest(deadline=deadline):
                record = _admit(
                    requirement=fixtures.requirement(
                        budget={
                            "normal_deadline_seconds": deadline,
                            "hard_fail_safe_seconds": deadline
                            + fixtures.TEARDOWN_SECONDS,
                        }
                    )
                )
                self.assertEqual(expected, _gate(record, "normal-deadline")["status"])

    def test_hard_fail_safe_boundary(self):
        normal = fixtures.NORMAL_DEADLINE_SECONDS
        teardown = fixtures.TEARDOWN_SECONDS
        for hard, expected in (
            (normal + teardown, "PASS"),
            (normal + teardown - 0.01, "FAIL"),
        ):
            with self.subTest(hard=hard):
                record = _admit(
                    requirement=fixtures.requirement(
                        budget={"hard_fail_safe_seconds": hard}
                    )
                )
                self.assertEqual(expected, _gate(record, "hard-fail-safe")["status"])

    def test_missing_pricing_is_no_go(self):
        pricing = dict(fixtures.requirement()["pricing"])
        pricing["instance_hourly_rate_usd"] = None
        record = _admit(requirement=fixtures.requirement(pricing=pricing))
        self.assertEqual("NO-GO", record["decision"])
        self.assertEqual("FAIL", _gate(record, "cost-prediction")["status"])
        self.assertEqual("FAIL", _gate(record, "cost-budget")["status"])
        self.assertIn(
            "instance hourly rate unavailable",
            record["cost_prediction"]["reason"],
        )

    def test_unknown_material_charge_is_never_treated_as_zero(self):
        charges = fixtures.charges()
        charges.append(
            {
                "label": "cross-AZ data transfer",
                "material": True,
                "max_usd": None,
                "reason": None,
                "usd": None,
            }
        )
        record = _admit(requirement=fixtures.requirement(charges=charges))
        self.assertEqual("NO-GO", record["decision"])
        self.assertFalse(record["cost_prediction"]["available"])
        self.assertIn("never treated as zero", record["cost_prediction"]["reason"])
        self.assertEqual(
            ["cross-AZ data transfer"],
            record["cost_prediction"]["unbounded_material_charges"],
        )
        self.assertEqual("FAIL", _gate(record, "cost-budget")["status"])

    def test_immaterial_charge_must_record_why(self):
        charges = fixtures.charges()
        charges.append(
            {
                "label": "unexamined charge",
                "material": False,
                "max_usd": None,
                "reason": None,
                "usd": None,
            }
        )
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.validate_admission_requirement(
                fixtures.requirement(charges=charges)
            )

    def test_cost_budget_exceeded_is_no_go(self):
        record = _admit(
            requirement=fixtures.requirement(budget={"cost_budget_usd": 0.5})
        )
        self.assertEqual("NO-GO", record["decision"])
        self.assertEqual("PASS", _gate(record, "cost-prediction")["status"])
        self.assertEqual("FAIL", _gate(record, "cost-budget")["status"])

    def test_bounded_charges_raise_only_the_upper_cost_bound(self):
        cost = _admit()["cost_prediction"]
        lower, upper = cost["predicted_total_cost_range_usd"]
        ec2_lower, ec2_upper = cost["predicted_ec2_cost_range_usd"]
        self.assertEqual(round(ec2_lower + 0.96, 6), lower)
        self.assertEqual(round(ec2_upper + 0.96 + 0.07, 6), upper)
        self.assertFalse(cost["is_finalized_aws_invoice"])


class PhaseGateTest(unittest.TestCase):
    def test_phase_one_no_go_never_authorizes_billable_creation(self):
        for requirement_gate in (
            "protocol_lock",
            "seed_registry",
            "durable_evidence",
            "artifact_destination",
            "reattach",
        ):
            with self.subTest(gate=requirement_gate):
                gates = {
                    name: dict(value)
                    for name, value in fixtures.requirement()["gates"].items()
                }
                gates[requirement_gate] = {
                    "status": "FAIL",
                    "detail": "synthetic operator-declared failure",
                }
                record = _admit(requirement=fixtures.requirement(gates=gates))
                self.assertEqual("NO-GO", record["decision"])
                self.assertFalse(record["billable_resource_creation_authorized"])
                self.assertFalse(record["scientific_submission_authorized"])
                self.assertTrue(record["blocking_reasons"])

    def test_phase_two_go_authorizes_submission(self):
        phase_one = _admit()
        phase_two = calibration.build_phase_two_admission(
            phase_one, fixtures.observation(phase_one), now=fixtures.NOW
        )
        self.assertEqual("GO", phase_two["decision"])
        self.assertTrue(phase_two["scientific_submission_authorized"])
        self.assertEqual(
            phase_one["admission_identity"],
            phase_two["phase_one_admission_identity"],
        )
        self.assertEqual(
            calibration.PHASE_TWO_GATE_NAMES,
            tuple(gate["name"] for gate in phase_two["gates"]),
        )

    def test_phase_two_no_go_never_authorizes_scientific_submission(self):
        phase_one = _admit()
        cases = {
            "actual-environment-identity": {"arena_revision": "9" * 40},
            "hard-fail-safe-armed": {"fail_safe_armed": False},
            "retained-destination-writable": {
                "retained_destination": {"write_probe": "FAIL"}
            },
            "recovery-identity-persisted": {"recovery_identity": {"status": "FAIL"}},
            "remaining-budget-sufficient": {
                "observed_at_epoch": fixtures.FAIL_SAFE_ARM_EPOCH
                + int(fixtures.HARD_FAIL_SAFE_SECONDS)
                - 60
            },
        }
        for gate_name, override in cases.items():
            with self.subTest(gate=gate_name):
                phase_two = calibration.build_phase_two_admission(
                    phase_one,
                    fixtures.observation(phase_one, **override),
                    now=fixtures.NOW,
                )
                self.assertEqual("NO-GO", phase_two["decision"])
                self.assertFalse(phase_two["scientific_submission_authorized"])
                self.assertEqual("FAIL", _gate(phase_two, gate_name)["status"])
                self.assertIn(
                    "submit no scientific seed",
                    " ".join(phase_two["limitations"]),
                )

    def test_phase_two_never_passes_for_a_nonexistent_instance(self):
        phase_one = _admit()
        phase_two = calibration.build_phase_two_admission(
            phase_one,
            fixtures.observation(
                phase_one,
                instance_id="",
                retained_destination={"verified_on_instance_id": ""},
                recovery_identity={"verified_on_instance_id": ""},
            ),
            now=fixtures.NOW,
        )
        self.assertEqual("NO-GO", phase_two["decision"])
        for gate_name in (
            "actual-environment-identity",
            "hard-fail-safe-armed",
            "retained-destination-writable",
            "recovery-identity-persisted",
        ):
            self.assertEqual("FAIL", _gate(phase_two, gate_name)["status"])

    def test_phase_two_rejects_a_fail_safe_deadline_that_is_not_the_admitted_one(self):
        phase_one = _admit()
        phase_two = calibration.build_phase_two_admission(
            phase_one,
            fixtures.observation(
                phase_one,
                fail_safe_deadline_epoch=fixtures.FAIL_SAFE_ARM_EPOCH + 3600,
            ),
            now=fixtures.NOW,
        )
        self.assertEqual("FAIL", _gate(phase_two, "hard-fail-safe-armed")["status"])

    def test_phase_two_requires_the_matching_phase_one_record(self):
        phase_one = _admit()
        other = _admit(requirement=fixtures.requirement(run_id="other-run"))
        phase_two = calibration.build_phase_two_admission(
            phase_one, fixtures.observation(other), now=fixtures.NOW
        )
        self.assertEqual("NO-GO", phase_two["decision"])
        self.assertEqual(
            "FAIL", _gate(phase_two, "actual-environment-identity")["status"]
        )

    def test_phase_two_cannot_follow_a_phase_one_no_go(self):
        no_go = _admit(evidence_documents=[])
        self.assertEqual("NO-GO", no_go["decision"])
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.build_phase_two_admission(
                no_go,
                fixtures.observation(no_go),
                now=fixtures.NOW,
            )

    def test_admission_record_decision_and_gates_cannot_disagree(self):
        record = _admit()
        forged = dict(record)
        forged["gates"] = [
            dict(gate, status="FAIL") if gate["name"] == "cost-budget" else gate
            for gate in record["gates"]
        ]
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.validate_admission_record(forged)


class InformationFlowTest(unittest.TestCase):
    def test_no_scientific_fields_in_calibration_or_admission(self):
        phase_one = _admit()
        phase_two = calibration.build_phase_two_admission(
            phase_one, fixtures.observation(phase_one), now=fixtures.NOW
        )
        for document, context in (
            (fixtures.evidence(), "calibration evidence"),
            (fixtures.requirement(), "admission requirement"),
            (phase_one, "phase 1 admission"),
            (phase_two, "phase 2 admission"),
            (calibration.historical_incomplete_observation(), "historical observation"),
        ):
            with self.subTest(context=context):
                calibration.assert_no_scientific_fields(document, context=context)
                serialized = json.dumps(document, sort_keys=True)
                for forbidden in ('"score"', '"rank"', '"f1"', '"f2"', '"support"'):
                    self.assertNotIn(forbidden, serialized)

    def test_a_scientific_field_anywhere_fails_closed(self):
        evidence = fixtures.evidence()
        leaked = dict(evidence)
        leaked["limitations"] = list(evidence["limitations"])
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.assert_no_scientific_fields(
                {"gates": [{"detail": {"p2_outcome": "OFFENSE SUPPORT QUALIFIED"}}]},
                context="admission record",
            )
        with self.assertRaises(calibration.AwsOperationalCalibrationError) as caught:
            calibration.build_calibration_evidence_from_document(
                {"score": 1.0, "tasks": leaked["tasks"]}
            )
        self.assertIn("scientific fields are forbidden", str(caught.exception))


class HistoricalObservationTest(unittest.TestCase):
    def test_historical_326_observation_preserves_missing_units(self):
        record = calibration.historical_incomplete_observation()
        self.assertEqual(
            calibration.INCOMPLETE_OBSERVATION_SCHEMA_VERSION, record["schema_version"]
        )
        self.assertEqual("20260921T061727Z-184f3b4c", record["run_id"])
        self.assertEqual(96, record["target_units"])
        self.assertEqual(28800.0, record["hard_fail_safe_seconds"])
        self.assertFalse(record["admissible_as_calibration"])
        for missing in (
            "completed_units",
            "task_durations_seconds",
            "p50_seconds_per_unit",
            "p90_seconds_per_unit",
            "throughput_units_per_hour",
            "batch_scientific_wall_clock_seconds",
            "ec2_billable_runtime_seconds",
        ):
            with self.subTest(field=missing):
                self.assertIsNone(record[missing])
                self.assertIn(missing, record["unavailable_fields"])
        self.assertIn(
            "96 hanchan completed within 8 hours", record["forbidden_derivations"]
        )
        self.assertIn("12 hanchan per hour", record["forbidden_derivations"])
        self.assertTrue(record["provenance_reference"].startswith("https://"))

    def test_historical_326_observation_can_never_be_a_matching_calibration(self):
        record = calibration.historical_incomplete_observation()
        with self.assertRaises(calibration.AwsOperationalCalibrationError):
            calibration.validate_calibration_evidence(record)
        admission = _admit(evidence_documents=[])
        self.assertEqual("NO-GO", admission["decision"])
        self.assertIsNone(admission["calibration"]["identity"])


@unittest.skipIf(
    os.name == "nt",
    "the #339 durable checkpoint primitive fsyncs the destination directory, "
    "which Windows cannot open; CI runs this on Linux",
)
class DurableReceiptSourceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _publish(self, seeds):
        start = datetime(2026, 9, 20, tzinfo=UTC)
        for index, seed in enumerate(seeds):
            offset = timedelta(seconds=(index // 16) * 60)
            publish_seed_checkpoint(
                self.root,
                run_id="calibration-run",
                seed=seed,
                protocol_identity="a" * 64,
                payload={"seed": seed},
                started_at=(start + offset)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                completed_at=(start + offset + timedelta(seconds=60))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            )

    def test_tasks_are_derived_from_durable_receipts(self):
        seeds = list(range(900_000, 900_020))
        self._publish(seeds)
        tasks = calibration.tasks_from_durable_receipts(
            self.root,
            run_id="calibration-run",
            protocol_identity="a" * 64,
            expected_seeds=seeds,
        )
        self.assertEqual(20, len(tasks))
        evidence = fixtures.evidence(
            tasks=tasks, durable_evidence_level="per-seed-durable-receipt"
        )
        self.assertEqual(16, evidence["peak_observed_concurrency"])
        self.assertEqual(60.0, evidence["p90_seconds_per_unit"])

    def test_an_incomplete_receipt_set_fails_closed(self):
        seeds = list(range(900_000, 900_020))
        self._publish(seeds[:-1])
        with self.assertRaises(Exception):
            calibration.tasks_from_durable_receipts(
                self.root,
                run_id="calibration-run",
                protocol_identity="a" * 64,
                expected_seeds=seeds,
            )


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def test_cli_go_and_no_go_exit_codes(self):
        evidence_input = {
            "calibration_run_id": "calibration-cli",
            "calibrated_at": "2026-09-20T00:10:00Z",
            "seed_allocation": {
                "allocation_identity": fixtures.CALIBRATION_ALLOCATION_IDENTITY,
                "ledger_revision": fixtures.LEDGER_REVISION,
                "owner_repository": "lisbun/lisjong-arena",
                "seed_domain": "riichienv-4p-red-half-hanchan-v1",
                "seed_membership_identity": fixtures.CALIBRATION_MEMBERSHIP_IDENTITY,
            },
            "seed_allocation_population": calibration.CALIBRATION_POPULATION,
            "seed_ledger_revision": fixtures.LEDGER_REVISION,
            "arena_revision": fixtures.ARENA_REVISION,
            "lisjong_revision": fixtures.LISJONG_REVISION,
            "lisjong_engine_revision": fixtures.LISJONG_ENGINE_REVISION,
            "riichienv_version": fixtures.RIICHIENV_VERSION,
            "workload_identity": fixtures.WORKLOAD_IDENTITY,
            "teacher_identity": fixtures.TEACHER_IDENTITY,
            "game_mode": fixtures.GAME_MODE,
            "instance_type": fixtures.INSTANCE_TYPE,
            "vcpu": fixtures.VCPU,
            "worker_count_requested": fixtures.WORKERS,
            "workers_active_observed": fixtures.WORKERS,
            "tasks": fixtures.tasks(),
            "batch_scientific_wall_clock_seconds": fixtures.WALL_CLOCK_SECONDS,
            "ec2_billable_runtime_seconds": 2400.0,
            "setup_overhead_seconds": 900.0,
            "teardown_overhead_seconds": 400.0,
            "durable_evidence_level": fixtures.DURABLE_LEVEL,
            "instrumentation_identity": fixtures.INSTRUMENTATION_IDENTITY,
            "instrumentation_path": "issue-332/phase-A/operational/progress.json",
        }
        evidence_path = str(self.root / "calibration.json")
        self.assertEqual(
            0,
            calibration.main(
                [
                    "evidence",
                    "--input",
                    self._write("calibration-input.json", evidence_input),
                    "--output",
                    evidence_path,
                ]
            ),
        )
        self.assertEqual(
            0, calibration.main(["validate-evidence", "--calibration", evidence_path])
        )
        phase_one_path = str(self.root / "admission-phase-1.json")
        self.assertEqual(
            0,
            calibration.main(
                [
                    "admit-phase-1",
                    "--requirement",
                    self._write("requirement.json", fixtures.requirement()),
                    "--calibration",
                    evidence_path,
                    "--output",
                    phase_one_path,
                    "--now",
                    "2026-09-21T00:00:00Z",
                ]
            ),
        )
        phase_one = calibration.read_admission_record(phase_one_path)
        self.assertEqual("GO", phase_one["decision"])
        self.assertEqual(
            0,
            calibration.main(
                [
                    "admit-phase-2",
                    "--admission",
                    phase_one_path,
                    "--observation",
                    self._write("observation.json", fixtures.observation(phase_one)),
                    "--output",
                    str(self.root / "admission-phase-2.json"),
                    "--now",
                    "2026-09-21T00:00:00Z",
                ]
            ),
        )
        self.assertEqual(
            3,
            calibration.main(
                [
                    "admit-phase-1",
                    "--requirement",
                    self._write("requirement-2.json", fixtures.requirement()),
                    "--output",
                    str(self.root / "admission-no-go.json"),
                    "--now",
                    "2026-09-21T00:00:00Z",
                ]
            ),
        )
        no_go = calibration.read_admission_record(
            str(self.root / "admission-no-go.json")
        )
        self.assertEqual("NO-GO", no_go["decision"])
        self.assertFalse(no_go["billable_resource_creation_authorized"])
        self.assertEqual(
            2,
            calibration.main(
                [
                    "admit-phase-1",
                    "--requirement",
                    self._write("broken.json", {"schema_version": "nope"}),
                    "--output",
                    str(self.root / "unused.json"),
                ]
            ),
        )
        self.assertFalse((self.root / "unused.json").exists())

    def test_cli_emits_the_historical_observation(self):
        output = str(self.root / "historical.json")
        self.assertEqual(
            0, calibration.main(["historical-observation", "--output", output])
        )
        document = json.loads(Path(output).read_text(encoding="utf-8"))
        self.assertIsNone(document["completed_units"])
        self.assertEqual(
            2,
            calibration.main(["historical-observation", "--output", output]),
        )


class SanityTest(unittest.TestCase):
    def test_fixture_shape_matches_the_332_phase_a_production_shape(self):
        target = fixtures.requirement()["target"]
        self.assertEqual("c7i.4xlarge", target["instance_type"])
        self.assertEqual(16, target["worker_count"])
        self.assertEqual(20, target["total_units"])
        self.assertEqual("0.4.10", target["riichienv_version"])
        self.assertEqual("4p-red-half", target["game_mode"])
        self.assertEqual(
            math.ceil(target["total_units"] / target["worker_count"]),
            calibration.predict_runtime(
                fixtures.evidence(),
                total_units=target["total_units"],
                worker_count=target["worker_count"],
                headroom_factor=1.5,
            )["waves"],
        )


if __name__ == "__main__":
    unittest.main()
