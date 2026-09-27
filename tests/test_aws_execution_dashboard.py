from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path

from lisjong_arena import aws_execution_dashboard as dashboard
from lisjong_arena import aws_execution_observability as core

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def documents(completed=2):
    pricing = core.pricing_provenance(
        source="synthetic rate",
        checked_at=NOW,
        region="ap-northeast-1",
        instance_hourly_rate_usd=0.1,
    )
    plan = core.build_execution_plan(
        run_id="run-1",
        unit_kind="hanchan",
        total_units=10,
        instance_type="t3.small",
        vcpu=2,
        memory_mib=2048,
        worker_count=3,
        fail_safe_seconds=3600,
        pricing=pricing,
        predicted_scientific_runtime_seconds=(300, 500),
        scientific_estimate_basis="synthetic",
        predicted_ec2_billable_runtime_seconds=(600, 900),
        billable_estimate_basis="synthetic",
        retained_ebs_estimate_usd=None,
        unknown_variable_charges=("IPv4: unknown",),
    )
    progress = core.ProgressTracker("run-1", "hanchan", 10, 3, NOW).snapshot(
        completed, now=NOW + timedelta(seconds=completed * 30)
    )
    calibration = core.build_calibration(
        run_id="run-1",
        unit_kind="hanchan",
        completed_units=10,
        scientific_runtime_seconds=300,
        ec2_billable_runtime_seconds=950,
        predicted_scientific_runtime_seconds=(300, 500),
        predicted_ec2_billable_runtime_seconds=(600, 900),
        instance_type="t3.small",
        vcpu=2,
        worker_count=3,
        pricing=pricing,
    )
    return plan, progress, calibration


class DashboardTest(unittest.TestCase):
    def test_plan_and_optional_values(self):
        plan, _, _ = documents()
        for field in (
            "pricing",
            "scientific_runtime_estimate",
            "ec2_billable_runtime_estimate",
            "predicted_ec2_cost",
            "estimated_fail_safe_cost_exposure",
            "warnings",
        ):
            plan.pop(field)
        html = dashboard.render_dashboard(plan, generated_at=NOW)
        self.assertIn("unavailable", html)
        self.assertIn("run-1", html)
        self.assertIn("Fail-safe deadline", html)
        self.assertIn("not a finalized AWS invoice", html)
        self.assertNotIn("None", html)

    def test_eta_states_and_snapshot_timestamps(self):
        for completed, status in (
            (0, "warming-up"),
            (2, "available"),
            (10, "complete"),
        ):
            with self.subTest(status=status):
                plan, progress, _ = documents(completed)
                html = dashboard.render_dashboard(
                    plan, progress, generated_at=NOW + timedelta(hours=1)
                )
                self.assertIn(f"<dd>{status}</dd>", html)
                self.assertIn(progress["updated_at"], html)
                self.assertIn("2026-09-28T01:00:00+00:00", html)
                self.assertIn(f"{completed} / 10", html)

    def test_calibration_separates_workload_from_billable_runtime(self):
        html = dashboard.render_dashboard(*documents(10))
        for text in (
            "Workload (scientific) actual seconds</dt><dd>300",
            "EC2 billable actual seconds</dt><dd>950",
            "0.026389",
            "worker_count exceeds vCPU count",
            "IPv4: unknown",
        ):
            self.assertIn(text, html)
        # An older progress snapshot is explicitly labelled, not mistaken for final counts.
        html = dashboard.render_dashboard(*documents(2))
        self.assertIn("2 / 10", html)
        self.assertIn("Completed units (calibration)</dt><dd>10", html)

    def test_identity_mismatch_is_rejected(self):
        for index, key, bad in (
            (1, "run_id", "other"),
            (1, "unit_kind", "games"),
            (1, "total_units", 11),
            (1, "worker_count", 4),
            (2, "run_id", "other"),
            (2, "unit_kind", "games"),
            (2, "worker_count", 4),
            (2, "worker_count", True),
            (2, "instance_type", "c7i.xlarge"),
            (2, "vcpu", 4),
            (2, "completed_units", 11),
            (2, "completed_units", 1),
            (2, "total_units", 11),
        ):
            with self.subTest(index=index, key=key):
                values = list(documents())
                values[index][key] = bad
                with self.assertRaises(core.AwsExecutionObservabilityError):
                    dashboard.render_dashboard(*values)

    def test_invalid_schema_and_fields(self):
        for index in range(3):
            values = list(documents())
            values[index]["schema_version"] = "future"
            with self.assertRaises(core.AwsExecutionObservabilityError):
                dashboard.render_dashboard(*values)
        for field, value in (("eta_status", "estimated"), ("scores", [25000] * 4)):
            plan, progress, _ = documents()
            progress[field] = value
            with self.assertRaises(core.AwsExecutionObservabilityError):
                dashboard.render_dashboard(plan, progress)
        plan, _, calibration = documents()
        for mutation in (
            {"instance": []},
            {"worker_count": True},
            {"warnings": "warning"},
            {"pricing": {"checked_at": "2026-09-28"}},
            {"predicted_ec2_cost": {"range_usd": [1, 0]}},
            {"estimated_fail_safe_cost_exposure": {"ec2_compute_usd": float("nan")}},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(core.AwsExecutionObservabilityError):
                    dashboard.render_dashboard(plan | mutation)
        calibration["ec2_billable_runtime_seconds"] = 1
        with self.assertRaises(core.AwsExecutionObservabilityError):
            dashboard.render_dashboard(plan, calibration=calibration)

    def test_escape_and_allowlist_include_no_hidden_raw_document(self):
        plan, progress, calibration = documents()
        attack = '<script src="https://invalid.test/x">&</script>'
        plan["warnings"] = [attack]
        plan["pricing"]["source"] = attack
        for value in (plan, calibration):
            value.update(
                scores="PRIVATE_SCORE",
                credential="PRIVATE_SECRET",
                local_path="PRIVATE_PATH",
                future={"x": "PRIVATE_FUTURE"},
            )
        plan["instance"]["account_id"] = "PRIVATE_ACCOUNT"
        html = dashboard.render_dashboard(plan, progress, calibration)
        self.assertIn("&lt;script", html)
        self.assertNotIn(attack, html)
        self.assertNotIn("PRIVATE_", html)

        class Resources(HTMLParser):
            def handle_starttag(self, tag, attrs):
                assert tag not in ("script", "link", "img", "iframe")
                assert not ({key for key, _ in attrs} & {"src", "href"})

        Resources().feed(html)
        self.assertNotIn("@import", html)
        self.assertNotIn("url(", html)
        self.assertIn('name="viewport"', html)
        self.assertIn("overflow-wrap:anywhere", html)

    def test_cli_writes_after_validation_only_and_protects_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [
                root / name
                for name in ("plan.json", "progress.json", "calibration.json")
            ]
            for path, value in zip(paths, documents()):
                path.write_text(json.dumps(value), encoding="utf-8")
            out = root / "dashboard.html"
            for count in (1, 2, 3):
                args = ["--plan", str(paths[0]), "--out", str(out)]
                if count >= 2:
                    args.extend(["--progress", str(paths[1])])
                if count == 3:
                    args.extend(["--calibration", str(paths[2])])
                self.assertEqual(dashboard.main(args), 0)
                self.assertIn("<!doctype html>", out.read_text())
            original = out.read_bytes()
            paths[1].write_text("{}")
            with redirect_stderr(StringIO()):
                self.assertEqual(dashboard.main(args), 1)
            self.assertEqual(out.read_bytes(), original)
            out.unlink()
            with redirect_stderr(StringIO()):
                self.assertEqual(dashboard.main(args), 1)
            self.assertFalse(out.exists())
            original_plan = paths[0].read_bytes()
            with redirect_stderr(StringIO()):
                self.assertEqual(
                    dashboard.main(["--plan", str(paths[0]), "--out", str(paths[0])]), 1
                )
            self.assertEqual(paths[0].read_bytes(), original_plan)

    def test_progress_export_validates_identity_and_preserves_document(self):
        _, progress, _ = documents()
        with tempfile.TemporaryDirectory() as temp:
            source, out = Path(temp) / "source.json", Path(temp) / "progress.json"
            args = [
                "export-progress",
                "--input-path",
                str(source),
                "--output-path",
                str(out),
                "--run-id",
                "run-1",
            ]
            source.write_text(json.dumps(progress), encoding="utf-8")
            self.assertEqual(core.main(args), 0)
            self.assertEqual(json.loads(out.read_text()), progress)
            previous = out.read_bytes()
            for change in (
                {"scores": [1]},
                {"run_id": "other"},
                {"schema_version": "future"},
                {"completed_units": 1},
            ):
                source.write_text(json.dumps(progress | change), encoding="utf-8")
                with self.assertRaises(core.AwsExecutionObservabilityError):
                    core.main(args)
                self.assertEqual(out.read_bytes(), previous)


class StatusExportTest(unittest.TestCase):
    def test_status_export_with_fake_aws(self):
        from tests.test_aws_wait_shape_326_scripts import AwsWaitShape326ScriptTest

        _, progress, _ = documents()
        instance = {
            "InstanceId": "i-test",
            "State": {"Name": "running"},
            "InstanceType": "t3.small",
            "LaunchTime": NOW.isoformat(),
            "Tags": [
                {"Key": "lisjong-progress-path", "Value": "/mnt/run/progress.json"}
            ],
            "BlockDeviceMappings": [],
        }
        rules = [
            (("describe-instances",), {"Reservations": [{"Instances": [instance]}]}),
            (("describe-volumes",), {"Volumes": []}),
            (
                ("describe-instance-types",),
                {
                    "InstanceTypes": [
                        {
                            "VCpuInfo": {"DefaultVCpus": 2},
                            "MemoryInfo": {"SizeInMiB": 2048},
                        }
                    ]
                },
            ),
            (
                ("describe-instance-information",),
                {
                    "InstanceInformationList": [
                        {"InstanceId": "i-test", "PingStatus": "Online"}
                    ]
                },
            ),
            (("send-command",), {"Command": {"CommandId": "cmd-probe"}}),
            (
                ("get-command-invocation",),
                {"Status": "Success", "StandardOutputContent": json.dumps(progress)},
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dir with spaces" / "progress.json"
            args = ("-ProgressOutPath", str(output), "-Python", sys.executable)
            result = AwsWaitShape326ScriptTest._run_status_with_fake_aws(
                self, rules, args
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Progress: 2/10 hanchan", result.stdout)
            self.assertEqual(json.loads(output.read_text()), progress)
            original = output.read_bytes()
            for invalid in (
                progress | {"run_id": "wrong"},
                progress | {"scores": [1]},
                None,
            ):
                changed = copy.deepcopy(rules)
                changed[-1][1]["StandardOutputContent"] = (
                    "" if invalid is None else json.dumps(invalid)
                )
                result = AwsWaitShape326ScriptTest._run_status_with_fake_aws(
                    self, changed, args
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
