from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from lisjong_arena.aws_execution_observability import (
    AwsExecutionObservabilityError,
    ProgressTracker,
    build_calibration,
    build_execution_plan,
    calculate_compute_cost,
    main,
    pricing_provenance,
    validate_progress_document,
    validate_progress_transition,
    write_progress,
)


class AwsExecutionProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started = datetime(2026, 9, 21, 1, 2, 3, tzinfo=UTC)
        self.tracker = ProgressTracker(
            run_id="run-1",
            unit_kind="hanchan",
            total_units=96,
            worker_count=2,
            started_at=self.started,
        )

    def test_progress_starts_at_zero_with_warming_up_eta(self) -> None:
        progress = self.tracker.snapshot(0, now=self.started)
        self.assertEqual(progress["completed_units"], 0)
        self.assertEqual(progress["eta_status"], "warming-up")
        self.assertIsNone(progress["eta_seconds"])
        self.assertIsNone(progress["estimated_finish_at"])

    def test_eta_becomes_available_after_two_samples(self) -> None:
        first = self.tracker.snapshot(1, now=self.started + timedelta(seconds=60))
        self.assertEqual(first["eta_status"], "warming-up")
        second = self.tracker.snapshot(2, now=self.started + timedelta(seconds=120))
        self.assertEqual(second["eta_status"], "available")
        self.assertEqual(second["throughput_per_hour"], 60.0)
        self.assertEqual(second["eta_seconds"], 5640)

    def test_completed_above_total_is_rejected(self) -> None:
        with self.assertRaises(AwsExecutionObservabilityError):
            self.tracker.snapshot(97, now=self.started + timedelta(seconds=1))

    def test_decreasing_completed_is_rejected(self) -> None:
        self.tracker.snapshot(2, now=self.started + timedelta(seconds=120))
        with self.assertRaises(AwsExecutionObservabilityError):
            self.tracker.snapshot(1, now=self.started + timedelta(seconds=180))

    def test_total_is_immutable_across_documents(self) -> None:
        before = self.tracker.snapshot(0, now=self.started)
        after = dict(before)
        after["total_units"] = 95
        with self.assertRaises(AwsExecutionObservabilityError):
            validate_progress_transition(before, after)

    def test_scientific_fields_are_rejected(self) -> None:
        progress = self.tracker.snapshot(0, now=self.started)
        progress["scores"] = [25000] * 4
        with self.assertRaises(AwsExecutionObservabilityError):
            validate_progress_document(progress)

    def test_timezone_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(AwsExecutionObservabilityError):
            ProgressTracker(
                run_id="run-1",
                unit_kind="hanchan",
                total_units=1,
                worker_count=1,
                started_at=datetime(2026, 9, 21),
            )

    def test_progress_is_atomically_replaced(self) -> None:
        progress = self.tracker.snapshot(0, now=self.started)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "operational" / "progress.json"
            with patch(
                "lisjong_arena.aws_execution_observability.atomic_replace"
            ) as replace:
                write_progress(path, progress)
            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[0], path)
            self.assertEqual(
                json.loads(replace.call_args.args[1]),
                progress,
            )


class AwsExecutionPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pricing = pricing_provenance(
            source="injected test rate",
            checked_at=datetime(2026, 9, 21, tzinfo=UTC),
            region="ap-northeast-1",
            instance_hourly_rate_usd=0.1,
        )

    def test_cost_uses_runtime_and_injected_rate(self) -> None:
        self.assertEqual(calculate_compute_cost(5400, 0.1), 0.15)

    def test_pricing_provenance_is_retained_in_plan(self) -> None:
        plan = build_execution_plan(
            run_id="run-1",
            unit_kind="hanchan",
            total_units=96,
            instance_type="t3.small",
            vcpu=2,
            memory_mib=2048,
            worker_count=2,
            fail_safe_seconds=8 * 3600,
            pricing=self.pricing,
            predicted_runtime_seconds=(3600, 5400),
            estimate_basis="matching prior run",
            retained_ebs_estimate_usd=None,
            unknown_variable_charges=("data transfer: unknown / not hard-bounded",),
        )
        self.assertEqual(plan["pricing"], self.pricing)
        self.assertEqual(plan["predicted_ec2_cost"]["range_usd"], [0.1, 0.15])

    def test_no_matching_evidence_does_not_invent_runtime(self) -> None:
        plan = build_execution_plan(
            run_id="run-1",
            unit_kind="hanchan",
            total_units=96,
            instance_type="t3.small",
            vcpu=2,
            memory_mib=2048,
            worker_count=2,
            fail_safe_seconds=8 * 3600,
            pricing=self.pricing,
            predicted_runtime_seconds=None,
            estimate_basis=None,
            retained_ebs_estimate_usd=None,
        )
        self.assertEqual(plan["runtime_estimate"]["confidence"], "LOW")
        self.assertIsNone(plan["runtime_estimate"]["range_seconds"])
        self.assertEqual(
            plan["runtime_estimate"]["reason"], "no matching historical evidence"
        )

    def test_unavailable_rate_retains_pricing_check_provenance(self) -> None:
        unavailable = pricing_provenance(
            source="AWS Pricing API unavailable",
            checked_at=datetime(2026, 9, 21, tzinfo=UTC),
            region="ap-northeast-1",
            instance_hourly_rate_usd=None,
        )
        plan = build_execution_plan(
            run_id="run-1",
            unit_kind="hanchan",
            total_units=96,
            instance_type="t3.small",
            vcpu=2,
            memory_mib=2048,
            worker_count=2,
            fail_safe_seconds=8 * 3600,
            pricing=unavailable,
            predicted_runtime_seconds=None,
            estimate_basis=None,
            retained_ebs_estimate_usd=None,
        )
        self.assertEqual(plan["pricing"], unavailable)
        self.assertIsNone(plan["predicted_ec2_cost"])
        self.assertIsNone(plan["estimated_fail_safe_cost_exposure"])

    def test_unknown_charges_are_explicit(self) -> None:
        plan = build_execution_plan(
            run_id="run-1",
            unit_kind="hanchan",
            total_units=96,
            instance_type="t3.small",
            vcpu=2,
            memory_mib=2048,
            worker_count=3,
            fail_safe_seconds=8 * 3600,
            pricing=self.pricing,
            predicted_runtime_seconds=(3600, 5400),
            estimate_basis="matching prior run",
            retained_ebs_estimate_usd=None,
            unknown_variable_charges=(
                "T-family surplus credits: unknown / not hard-bounded",
                "data transfer: unknown / not hard-bounded",
            ),
        )
        self.assertEqual(len(plan["unknown_variable_charges"]), 2)
        self.assertEqual(plan["warnings"], ["worker_count exceeds vCPU count"])
        self.assertNotIn("maximum AWS bill", json.dumps(plan))

    def test_calibration_records_prediction_errors_and_estimated_cost(self) -> None:
        calibration = build_calibration(
            run_id="run-1",
            unit_kind="hanchan",
            completed_units=96,
            scientific_runtime_seconds=6000,
            ec2_billable_runtime_seconds=7200,
            predicted_runtime_seconds=(3600, 5400),
            instance_type="t3.small",
            vcpu=2,
            worker_count=2,
            pricing=self.pricing,
        )
        self.assertEqual(calibration["runtime_prediction_error_seconds"], 600.0)
        self.assertEqual(calibration["cost_prediction_error_usd"], 0.05)
        self.assertEqual(calibration["scientific_runtime_seconds"], 6000.0)
        self.assertEqual(calibration["ec2_billable_runtime_seconds"], 7200.0)
        self.assertEqual(calibration["actual_throughput_per_hour"], 57.6)
        self.assertEqual(calibration["estimated_realized_cost"]["ec2_compute_usd"], 0.2)
        self.assertEqual(
            calibration["estimated_realized_cost"]["kind"],
            "estimated realized cost",
        )
        self.assertFalse(
            calibration["estimated_realized_cost"]["is_finalized_aws_invoice"]
        )

    def test_calibration_rejects_billable_runtime_shorter_than_science(self) -> None:
        with self.assertRaisesRegex(
            AwsExecutionObservabilityError,
            "billable runtime cannot be shorter",
        ):
            build_calibration(
                run_id="run-1",
                unit_kind="hanchan",
                completed_units=96,
                scientific_runtime_seconds=6000,
                ec2_billable_runtime_seconds=5999,
                predicted_runtime_seconds=(3600, 5400),
                instance_type="t3.small",
                vcpu=2,
                worker_count=2,
                pricing=self.pricing,
            )

    def test_plan_cli_writes_the_pure_core_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "plan",
                        "--output-path",
                        str(path),
                        "--run-id",
                        "run-1",
                        "--unit-kind",
                        "hanchan",
                        "--total-units",
                        "96",
                        "--instance-type",
                        "t3.small",
                        "--vcpu",
                        "2",
                        "--memory-mib",
                        "2048",
                        "--worker-count",
                        "2",
                        "--fail-safe-seconds",
                        "28800",
                        "--pricing-source",
                        "injected test rate",
                        "--pricing-checked-at",
                        "2026-09-21T00:00:00Z",
                        "--pricing-region",
                        "ap-northeast-1",
                        "--instance-hourly-rate-usd",
                        "0.1",
                        "--predicted-runtime-min-seconds",
                        "3600",
                        "--predicted-runtime-max-seconds",
                        "5400",
                        "--estimate-basis",
                        "matching prior run",
                    ]
                )
            self.assertEqual(exit_code, 0)
            plan = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                plan["runtime_estimate"]["range_seconds"], [3600.0, 5400.0]
            )
            self.assertEqual(plan["predicted_ec2_cost"]["range_usd"], [0.1, 0.15])


if __name__ == "__main__":
    unittest.main()
