"""#517 校正（CALIBRATION）のplan・run・verify・reportとP2 backend照合（合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘を返す``fake_play*``を使う。
Kは32の代わりに3へ差し替える（module定数をpatch。workerはKを参照しない）。合成値であり、
校正結果・棋力結果ではない。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_protocol_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import backend_check, calibration
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.lock import (
    EngineAabbLockError,
    require_population_seeds,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    FORMAL_SPLIT,
    SEED_DOMAIN,
)

SEED_C = 514_003
SEEDS = (fx.SEED_A, fx.SEED_B, SEED_C)
LIMITS = {
    "enforcement": "synthetic test value",
    "process_tree_rss_bytes": 4 * 1024**3,
    "wall_seconds_per_stage": 4 * 3600,
}


def calibration_allocation(
    *, seeds=SEEDS, split="CALIBRATION", owner=None, population=None
):
    return seed_registry.build_allocation_record(
        owner_issue=owner or calibration.OWNER_ISSUE,
        protocol="arena-heuristic-candidate-engine-aabb-half-v1",
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population=population or calibration.POPULATION,
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-22T00:00:00Z",
    )


def calibration_ledger(record=None, *extra):
    document = seed_registry.new_ledger()
    document["allocations"] = [record or calibration_allocation(), *extra]
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def binding_for(document, record):
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def plan_document(tmp, **overrides):
    record = overrides.pop("record", None) or calibration_allocation()
    document = overrides.pop("ledger", None) or calibration_ledger(record)
    arguments = {
        "ordered_seeds": SEEDS,
        "allocation_binding": binding_for(document, record),
        "timeout_seconds": fx.TIMEOUT_SECONDS,
        "resource_limits": LIMITS,
        "wheel_path": fx.WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / "diagnostics"),
            "output": str(tmp / "calibration"),
        },
        "environment": fx.ENVIRONMENT,
        "arena_revision": fx.ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return calibration.assemble_plan(**arguments)


def gate(plan, *, ledger):
    calibration.require_live_calibration_target(
        plan, ledger=ledger, environment=fx.ENVIRONMENT, head=fx.ARENA_REVISION
    )


class _Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(calibration, "CALIBRATION_K", len(SEEDS))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


class PlanTest(_Case):
    def test_plan_binds_pair_population_design_and_limits(self):
        plan = plan_document(self.tmp)
        self.assertEqual(plan["evaluation_kind"], "CALIBRATION")
        self.assertIs(plan["not_a_strength_result"], True)
        self.assertEqual(
            (plan["participants"]["A"]["policy_identity"], plan["participants"]["B"]["policy_identity"]),
            calibration.CALIBRATION_PAIR,
        )  # fmt: skip
        self.assertEqual(plan["incumbent_role"], "current-champion")
        self.assertEqual(plan["population"]["split"], "CALIBRATION")
        self.assertEqual(plan["population"]["ordered_seeds"], list(SEEDS))
        self.assertEqual(plan["design"]["delta_pt_per_seat_result"], 1.0)
        self.assertEqual(plan["design"]["bootstrap_seed"], 514)
        self.assertEqual(plan["resource_limits"], LIMITS)
        path = calibration.save_plan(plan, self.tmp / "plan.json")
        self.assertEqual(calibration.load_plan(path), plan)
        with self.assertRaises(FileExistsError):
            calibration.save_plan(plan, path)

    def test_plan_rejects_inputs_that_change_its_meaning(self):
        formal = calibration_allocation(split=FORMAL_SPLIT)
        other_owner = calibration_allocation(owner="lisbun/lisjong-arena#999")
        colliding = calibration_ledger(
            None,
            seed_registry.build_allocation_record(
                owner_issue="lisbun/lisjong-arena#1",
                protocol="other",
                seed_domain="riichienv-4p-red-half-hanchan-v1",
                purpose="test",
                population="other",
                split="FORMAL-EVAL",
                seeds=(SEED_C,),
                arena_revision="e" * 40,
                protocol_revision="test",
                provenance_reference="test",
                allocation_timestamp="2026-10-22T00:00:00Z",
            ),
        )
        external = calibration_allocation(seeds=(fx.SEED_A, fx.SEED_B, 472_005))
        cases = [
            ({"record": formal}, "split"),
            ({"record": other_owner}, "owner_issue"),
            ({"ledger": colliding, "record": calibration_allocation()}, "collide"),
            ({"record": external, "ordered_seeds": (fx.SEED_A, fx.SEED_B, 472_005)}, "outside the registry"),
            ({"ordered_seeds": SEEDS[:2]}, "exactly 3"),
            ({"timeout_seconds": 0}, "positive int"),
            ({"timeout_seconds": True}, "positive int"),
            ({"resource_limits": {**LIMITS, "wall_seconds_per_stage": 0}}, "positive int"),
            ({"resource_limits": {**LIMITS, "enforcement": " "}}, "enforcement"),
            ({"resource_limits": {"wall_seconds_per_stage": 1}}, "resource_limits"),
            ({"wheel_path": "relative.whl"}, "absolute"),
            ({"environment": fx.PYTHON_ENVIRONMENT}, "rust"),
            ({"arena_revision": "f" * 40}, "Arena revision"),
            ({"destinations": {"output": str(self.tmp / "x")}}, "destinations"),
        ]  # fmt: skip
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    plan_document(self.tmp, **overrides)

    def test_saved_plan_tampering_is_rejected(self):
        plan = plan_document(self.tmp)
        for key, value in (
            ("design", {**plan["design"], "delta_pt_per_seat_result": 2.0}),
            ("not_a_strength_result", False),
            ("incumbent_role", "fixed-c0"),
            ("plan_digest", "0" * 64),
        ):
            with self.subTest(key=key):
                with self.assertRaises(EngineAabbRecordError):
                    calibration.parse_plan({**plan, key: value})
        swapped = {
            **plan,
            "participants": {
                "A": plan["participants"]["B"],
                "B": plan["participants"]["A"],
            },
        }
        with self.assertRaisesRegex(EngineAabbRecordError, "fixed pair"):
            calibration.parse_plan(swapped)

    def test_formal_lock_refuses_a_calibration_allocation(self):
        record = calibration_allocation()
        document = calibration_ledger(record)
        binding = binding_for(document, record)
        # 正式lock（assemble_lock_document / require_live_target）が使う照合
        with self.assertRaisesRegex(EngineAabbLockError, "allocation"):
            require_population_seeds(document, binding, SEEDS, event=517)

    def test_live_gate_requires_the_allocation_to_stay_active(self):
        record = calibration_allocation()
        document = calibration_ledger(record)
        plan = plan_document(self.tmp, record=record, ledger=document)
        gate(plan, ledger=document)
        retired = seed_registry.transition_allocation(
            document, record["allocation_identity"], state="RETIRED"
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "not active"):
            gate(plan, ledger=retired)
        with self.assertRaisesRegex(EngineAabbRecordError, "HEAD"):
            calibration.require_live_calibration_target(
                plan, ledger=document, environment=fx.ENVIRONMENT, head="f" * 40
            )


class VariancePlanningTest(unittest.TestCase):
    def blocks(self, differences):
        return [
            {"difference_internal": value, "seed": index}
            for index, value in enumerate(differences)
        ]

    def test_planning_k_formula(self):
        # ((1.959964 + 0.841621) * 1 / 1)^2 = 7.8489 -> 8
        self.assertEqual(calibration.planning_k(1.0), 8)
        self.assertEqual(calibration.planning_k(0.0), 0)

    def test_sd_bootstrap_and_leave_one_out(self):
        values = [1200, -2400, 600, 0, 3600]
        result = calibration.variance_planning(self.blocks(values))
        d = [value / 120 for value in values]
        mean = sum(d) / len(d)
        sd = (sum((x - mean) ** 2 for x in d) / (len(d) - 1)) ** 0.5
        self.assertAlmostEqual(result["sd"], sd)
        self.assertEqual(
            result["planning"]["sigma_sample"]["k_plan"], calibration.planning_k(sd)
        )
        self.assertEqual(
            result["planning"]["sigma_sample"]["hanchans"],
            6 * calibration.planning_k(sd),
        )
        loo = result["leave_one_block_out"]["by_seed"]
        self.assertEqual([row["seed"] for row in loo], list(range(5)))
        self.assertAlmostEqual(
            sum(row["share_of_squared_deviation"] for row in loo), 1.0
        )
        self.assertGreaterEqual(
            result["bootstrap"]["sd_upper"], result["bootstrap"]["sd_median"]
        )
        self.assertEqual(result["bootstrap"]["replicates"], 10_000)
        # 同じ入力・同じ乱数seedなら同じ値（決定的）
        self.assertEqual(calibration.variance_planning(self.blocks(values)), result)
        self.assertNotIn("mean_d", result)  # fmt: skip

    def test_constant_blocks_and_too_few_blocks(self):
        result = calibration.variance_planning(self.blocks([0, 0, 0]))
        self.assertEqual(result["sd"], 0.0)
        self.assertEqual(result["planning"]["sigma_sample"]["k_plan"], 0)
        with self.assertRaises(EngineAabbRecordError):
            calibration.variance_planning(self.blocks([0, 0]))


class RunTest(_Case):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, calibration_ledger())
        self.plan = plan_document(self.tmp)
        self.plan_path = calibration.save_plan(self.plan, self.tmp / "plan.json")
        self.output = self.tmp / "calibration"

    def run_calibration(self, play=fx.fake_play, **kwargs):
        return calibration.run_calibration(
            self.plan_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=gate,
            **kwargs,
        )

    def assert_stopped(self):
        self.assertFalse(self.output.exists())
        stops = list((self.tmp / "diagnostics").glob("stop-*.json"))
        self.assertEqual(len(stops), 1)
        stop = json.loads(stops[0].read_text("utf-8"))
        self.assertTrue(stop["not_a_result"])
        self.assertEqual(stop["status"], "STOP / INCOMPLETE")
        self.assertEqual(stop["plan_digest"], self.plan["plan_digest"])
        return stop

    def test_complete_calibration_verify_and_report(self):
        summary = self.run_calibration()
        self.assertEqual(
            summary["status"], "CALIBRATION COMPLETE; NOT A STRENGTH RESULT"
        )
        self.assertEqual(summary["hanchan_count"], 18)
        self.assertEqual(summary["event_fact_status"], {"status": "VALID"})
        self.assertEqual(
            [(b["seed"], b["difference_internal"]) for b in summary["blocks"]],
            [(fx.SEED_A, -1000), (fx.SEED_B, 0), (SEED_C, 0)],
        )
        self.assertAlmostEqual(summary["blocks"][0]["d"], -1000 / 120)
        for forbidden in ("mean_d", "lower", "upper", "classification", "statistic"):
            self.assertNotIn(forbidden, summary)
        self.assertEqual(summary["variance"]["k"], 3)
        timings = json.loads((self.output / "timings.json").read_text("utf-8"))
        self.assertEqual(len(timings["hanchans"]), 18)
        self.assertEqual(timings["hanchans"][0]["worker_peak_rss_kib"], 1024)

        verified = calibration.verify_calibration(self.plan_path)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertIsNone(verified["report_digest"])
        paths = [item["path"] for item in verified["files"]]
        self.assertIn("raw/manifest.json", paths)
        self.assertIn("summary.json", paths)

        report = calibration.write_report(self.plan_path)
        self.assertEqual(
            report["timing"]["hanchan_wall_seconds"]["max"],
            max(r["wall_seconds"] for r in timings["hanchans"]),
        )
        self.assertEqual(
            report["j2_timeout"]["provisional_timeout_seconds"], fx.TIMEOUT_SECONDS
        )
        self.assertEqual(report["j2_timeout"]["factor_2_timeout_seconds"] % 60, 0)
        self.assertEqual(
            set(report["j1_schedule_bounds"]), set(summary["variance"]["planning"])
        )
        self.assertEqual(report["memory"]["worker_peak_rss_kib_max"], 1024)
        self.assertEqual(calibration.verify_calibration(self.plan_path)["report_digest"], report["report_digest"])  # fmt: skip

    def test_verify_in_a_separate_process(self):
        summary = self.run_calibration()
        code = (
            "import sys\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb import calibration\n"
            "with __import__('unittest.mock').mock.patch.object(calibration, 'CALIBRATION_K', 3):\n"
            "    print(calibration.verify_calibration(sys.argv[1])['summary_digest'])\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(self.plan_path)], text=True
        )
        self.assertEqual(output.strip(), summary["summary_digest"])

    def test_tampering_is_rejected(self):
        self.run_calibration()
        calibration.write_report(self.plan_path)
        summary_path = self.output / "summary.json"
        original = summary_path.read_text("utf-8")
        tampered = json.loads(original)
        tampered["variance"]["sd"] = 0.0
        summary_path.write_text(canonical_json_text(tampered), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "summary differs"):
            calibration.verify_calibration(self.plan_path)
        summary_path.write_text(original, "utf-8", newline="\n")

        timings_path = self.output / "timings.json"
        timings = json.loads(timings_path.read_text("utf-8"))
        timings["hanchans"][0]["wall_seconds"] = 9999.0
        timings_path.write_text(canonical_json_text(timings), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "report differs"):
            calibration.verify_calibration(self.plan_path)

        records = self.output / "raw" / "kyoku-records.jsonl"
        records.write_text(records.read_text("utf-8") + "\n", "utf-8", newline="\n")
        with self.assertRaises(EngineAabbRecordError):
            calibration.verify_calibration(self.plan_path)

    def rewrite_timings(self, change):
        path = self.output / "timings.json"
        timings = json.loads(path.read_text("utf-8"))
        change(timings)
        path.write_text(canonical_json_text(timings), "utf-8", newline="\n")

    def test_verify_rejects_invalid_timings_before_a_report_exists(self):
        self.run_calibration()
        original = (self.output / "timings.json").read_text("utf-8")
        first = json.loads(original)["hanchans"][0]

        def row(field, value):
            def change(timings):
                timings["hanchans"][0][field] = value

            return change

        def total(field, value):
            def change(timings):
                timings[field] = value

            return change

        cases = [
            (row("wall_seconds", -0.5), "finite non-negative"),
            (row("wall_seconds", True), "finite non-negative"),
            (row("wall_seconds", "1.0"), "finite non-negative"),
            (row("kyoku_count", 0), "positive int"),
            (row("kyoku_count", -3), "positive int"),
            (row("kyoku_count", True), "positive int"),
            (row("kyoku_count", 1.0 * first["kyoku_count"]), "positive int"),
            (
                row("kyoku_count", first["kyoku_count"] + 1),
                "differs from the raw record",
            ),
            (row("worker_peak_rss_kib", 0), "positive int or null"),
            (row("worker_peak_rss_kib", -1), "positive int or null"),
            (row("worker_peak_rss_kib", 1024.0), "positive int or null"),
            (row("parent_peak_rss_kib", True), "positive int or null"),
            (total("schedule_wall_seconds", -1), "finite non-negative"),
            (total("summary_seconds", False), "finite non-negative"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                self.rewrite_timings(change)
                self.assertFalse((self.output / "report.json").exists())
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    calibration.verify_calibration(self.plan_path)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    calibration.write_report(self.plan_path)
                self.rewrite_timings(lambda t: t.update(json.loads(original)))
        calibration.verify_calibration(self.plan_path)

    def test_missing_rss_is_not_counted_as_zero(self):
        self.run_calibration()

        def missing(timings):
            for item in timings["hanchans"]:
                item["parent_peak_rss_kib"] = None
            timings["hanchans"][0]["worker_peak_rss_kib"] = None

        self.rewrite_timings(missing)
        calibration.verify_calibration(self.plan_path)
        report = calibration.write_report(self.plan_path)
        memory = report["memory"]
        self.assertIsNone(memory["parent_peak_rss_kib_max"])
        self.assertEqual(memory["parent_peak_rss_missing_rows"], 18)
        self.assertIsNone(memory["sum_of_peaks_kib_max"])
        self.assertEqual(memory["sum_of_peaks_rows"], 0)
        self.assertEqual(memory["worker_peak_rss_kib_max"], 1024)
        self.assertEqual(memory["worker_peak_rss_missing_rows"], 1)
        self.assertEqual(
            calibration.verify_calibration(self.plan_path)["report_digest"],
            report["report_digest"],
        )
        with self.assertRaises(FileExistsError):
            calibration.write_report(self.plan_path)

    def test_failures_stop_without_output(self):
        cases = [
            (fx.fake_play_policy_error, "synthetic Policy failure"),
            (fx.fake_play_missing_evidence, ""),
            (fx.fake_play_other_environment, "environment"),
            (fx.fake_play_count_mismatch, ""),
        ]
        for play, message in cases:
            with self.subTest(play=play.__name__):
                shutil.rmtree(self.tmp / "diagnostics", ignore_errors=True)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    self.run_calibration(play)
                self.assert_stopped()

    def test_wall_limit_stops_the_calibration(self):
        ticks = iter(range(0, 10**9, 1000))
        with self.assertRaisesRegex(RunStopped, "wall limit"):
            self.run_calibration(clock=lambda: next(ticks) * 20.0)
        stop = self.assert_stopped()
        self.assertEqual(stop["completed_hanchans"], 1)

    def test_rss_limit_stops_the_calibration(self):
        plan = plan_document(
            self.tmp, resource_limits={**LIMITS, "process_tree_rss_bytes": 1}
        )
        self.plan_path.unlink()
        calibration.save_plan(plan, self.plan_path)
        self.plan = plan
        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            self.run_calibration()
        self.assert_stopped()

    def test_completed_calibration_is_never_rerun(self):
        self.run_calibration()
        with self.assertRaisesRegex(EngineAabbRecordError, "write-once"):
            self.run_calibration()


class BackendCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_check(self, backend, play, name=None):
        environment = fx.ENVIRONMENT if backend == "rust" else fx.PYTHON_ENVIRONMENT
        return backend_check.run_backend_check(
            self.tmp / (name or f"{backend}.json"),
            backend=backend,
            wheel_path=fx.WHEEL_PATH,
            timeout_seconds=fx.TIMEOUT_SECONDS,
            play=play,
            environment=environment,
            arena_revision=fx.ARENA_REVISION,
        )

    def test_matching_backends(self):
        rust = self.run_check("rust", fx.fake_play_rust_actions)
        self.assertEqual(len(rust["hanchans"]), 12)
        self.assertEqual([r["seed"] for r in rust["hanchans"][::6]], [472_005, 472_006])
        self.assertEqual(
            rust["participants"]["A"]["policy_identity"],
            calibration.CALIBRATION_PAIR[0],
        )
        self.run_check("python", fx.fake_play_python_actions)
        comparison = backend_check.compare_backend_checks(
            self.tmp / "python.json", self.tmp / "rust.json"
        )
        self.assertEqual(comparison["status"], "MATCH")
        self.assertEqual(comparison["mismatches"], [])
        self.assertEqual(comparison["hanchan_count"], 12)
        self.assertEqual(comparison["selected_action_count"], 12 * 166)  # fmt: skip

    def test_a_different_action_is_a_mismatch(self):
        self.run_check("rust", fx.fake_play_rust_actions)
        self.run_check("python", fx.fake_play_python_drift)
        comparison = backend_check.compare_backend_checks(
            self.tmp / "python.json", self.tmp / "rust.json"
        )
        self.assertEqual(comparison["status"], "MISMATCH")
        self.assertEqual(
            comparison["mismatches"],
            [{"field": "selected_actions", "placement": 3, "seed": 472_006}],
        )

    def test_saved_comparison_does_not_probe_or_execute_a_backend(self):
        """D3: Python evidence remains readable without executing Python Policy."""
        self.run_check("rust", fx.fake_play_rust_actions)
        self.run_check("python", fx.fake_play_python_actions)
        with (
            mock.patch.object(
                backend_check, "run_schedule", side_effect=AssertionError
            ),
            mock.patch.object(
                backend_check, "environment_document", side_effect=AssertionError
            ),
            mock.patch(
                "lisjong_arena.shanten_backend_verification.backend.require_shanten_backend",
                side_effect=AssertionError,
            ),
        ):
            comparison = backend_check.compare_backend_checks(
                self.tmp / "python.json", self.tmp / "rust.json"
            )
        self.assertEqual(comparison["status"], backend_check.MATCH)

    def test_saved_comparison_rejects_unknown_schema(self):
        self.run_check("rust", fx.fake_play_rust_actions)
        document = self.run_check("python", fx.fake_play_python_actions)
        path = self.tmp / "python.json"
        document["schema"] = "arena-heuristic-candidate-engine-aabb-backend-check-v999"
        document["document_digest"] = backend_check.document_identity(
            {key: value for key, value in document.items() if key != "document_digest"}
        )
        path.write_text(canonical_json_text(document), encoding="utf-8")
        with self.assertRaisesRegex(
            EngineAabbRecordError, "unsupported backend check document"
        ):
            backend_check.compare_backend_checks(path, self.tmp / "rust.json")

    def test_inputs_that_cannot_be_compared(self):
        self.run_check("rust", fx.fake_play_rust_actions)
        self.run_check("rust", fx.fake_play_rust_actions, name="rust-2.json")
        with self.assertRaisesRegex(EngineAabbRecordError, "python document"):
            backend_check.compare_backend_checks(
                self.tmp / "rust-2.json", self.tmp / "rust.json"
            )
        with self.assertRaisesRegex(EngineAabbRecordError, "backend is not python"):
            backend_check.run_backend_check(
                self.tmp / "x.json",
                backend="python",
                wheel_path="",
                timeout_seconds=60,
                play=fx.fake_play_python_actions,
                environment=fx.ENVIRONMENT,
                arena_revision=fx.ARENA_REVISION,
            )
        path = self.tmp / "rust.json"
        document = json.loads(path.read_text("utf-8"))
        document["hanchans"][0]["final_points"][0] += 1
        path.write_text(canonical_json_text(document), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "digest"):
            backend_check.load_backend_check(path)  # fmt: skip

    def test_missing_selected_actions_stop_the_check(self):
        with self.assertRaisesRegex(RunStopped, "selected actions"):
            self.run_check("rust", fx.fake_play_rust_missing_actions)
        self.assertFalse((self.tmp / "rust.json").exists())

    def test_canonical_engine_value(self):
        import enum
        from dataclasses import dataclass

        class Color(enum.Enum):
            RED = 1

        @dataclass(frozen=True)
        class Point:
            x: int
            tags: frozenset

        value = backend_check.canonical_engine_value(
            {"b": Point(1, frozenset({"z", "a"})), "a": (Color.RED, None)}
        )
        self.assertEqual(
            value,
            [
                ["a", ["Color.RED", None]],
                ["b", {"__type": "Point", "tags": ["a", "z"], "x": 1}],
            ],
        )
        with self.assertRaises(EngineAabbRecordError):
            backend_check.canonical_engine_value(object())


if __name__ == "__main__":
    unittest.main()
