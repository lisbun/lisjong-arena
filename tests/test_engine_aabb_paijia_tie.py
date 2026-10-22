"""#520 同点数牌paijia候補の開発比較（DEVELOPMENT）のplan・段階1・段階2（合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘と合成したPolicy観測を
返す ``fake_*`` を使う。段階2のKは32の代わりに3へ差し替える（workerはKを参照しない）。
判断の分類（worker側）はlisjong #330の実際のanalysis・action型で確かめる。
合成値であり、強さの結果ではない。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_paijia_tie_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    calibration,
    paijia_tie_activation,
    paijia_tie_plan,
    paijia_tie_screen,
    paijia_tie_worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    HanchanTask,
    RunStopped,
    play_task,
)
from lisjong_arena.heuristic_candidate_engine_aabb.lock import (
    EngineAabbLockError,
    require_population_seeds,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import FORMAL_SPLIT


class _Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(
            paijia_tie_plan.STAGE_K, {"screen": len(fx.SCREEN_SEEDS)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(_Case):
    def test_activation_plan_binds_pair_population_criteria_and_limits(self):
        plan = fx.plan_document(self.tmp)
        self.assertEqual(plan["evaluation_kind"], "DEVELOPMENT")
        self.assertIs(plan["not_a_formal_result"], True)
        self.assertEqual(plan["owner_issue"], "lisbun/lisjong-arena#520")
        self.assertEqual(
            (plan["participants"]["A"]["policy_identity"], plan["participants"]["B"]["policy_identity"]),
            ("placement-aware-speed-call-suited-paijia-tie-v1", "placement-aware-speed-call"),
        )  # fmt: skip
        for role in ("A", "B"):
            participant = plan["participants"][role]
            self.assertEqual(participant["behavior_configuration"], {})
            self.assertIsNone(participant["artifact"])
            self.assertEqual(participant["lisjong_revision"], fx.LISJONG_REVISION)
        self.assertEqual(
            plan["participants"]["A"]["factory_binding"],
            "lisjong_arena.policy_catalog:"
            "create_placement_aware_speed_call_suited_paijia_tie",
        )
        population = plan["population"]
        self.assertEqual(population["split"], "DEVELOPMENT")
        self.assertEqual(population["ordered_seeds"], list(fx.ACTIVATION_SEEDS))
        self.assertEqual(
            population["population"],
            "heuristic-candidate-engine-aabb-paijia-tie-activation-520",
        )
        criteria = plan["criteria"]
        self.assertEqual(criteria["minimum_changes"], 32)
        self.assertEqual(criteria["minimum_seeds_with_change"], 4)
        self.assertEqual(criteria["seat_hanchan_denominator"], 32)
        self.assertEqual(criteria["screen_limits"], fx.SCREEN_LIMITS)
        self.assertEqual(plan["execution"]["workers"], 1)
        self.assertEqual(plan["execution"]["backend"], "rust")
        path = paijia_tie_plan.save_plan(plan, self.tmp / "plan.json")
        self.assertEqual(paijia_tie_plan.load_plan(path), plan)
        with self.assertRaises(FileExistsError):
            paijia_tie_plan.save_plan(plan, path)
        with self.assertRaisesRegex(EngineAabbRecordError, "not screen"):
            paijia_tie_plan.load_plan(path, stage="screen")

    def test_screen_plan_fixes_progression_conditions(self):
        plan = fx.plan_document(self.tmp, stage="screen")
        conditions = plan["criteria"]["conditions"]
        self.assertEqual(conditions["mean_d_pt_above"], 0.0)
        self.assertEqual(conditions["base_points_mean_difference_pt_at_least"], 0.0)
        self.assertEqual(conditions["deal_in_rate_mean_difference_at_most"], 0.01)
        self.assertEqual(
            plan["criteria"]["labels"],
            {
                "all_conditions": "PROMISING-DEVELOPMENT",
                "otherwise": "NOT-PROMISING-DEVELOPMENT",
            },
        )
        self.assertEqual(
            plan["prerequisite"]["activation_plan_digest"], "1" * 64
        )  # fmt: skip

    def test_rehearsal_uses_fixed_development_seeds_without_allocation(self):
        for stage in ("activation", "screen"):
            with self.subTest(stage=stage):
                plan = fx.plan_document(self.tmp, stage=stage, run_kind="rehearsal")
                self.assertEqual(
                    plan["population"]["ordered_seeds"], [472_005, 472_006]
                )
                self.assertNotIn("allocation_binding", plan["population"])
                self.assertIsNone(plan["prerequisite"])
        with self.assertRaisesRegex(EngineAabbRecordError, "development seeds"):
            fx.plan_document(
                self.tmp, run_kind="rehearsal", ordered_seeds=(472_005, 472_007)
            )

    def test_plan_rejects_inputs_that_change_its_meaning(self):
        formal = fx.allocation("activation", fx.ACTIVATION_SEEDS, split=FORMAL_SPLIT)
        other_owner = fx.allocation(
            "activation", fx.ACTIVATION_SEEDS, owner="lisbun/lisjong-arena#999"
        )
        screen_population = fx.allocation(
            "activation",
            fx.ACTIVATION_SEEDS,
            population=paijia_tie_plan.POPULATIONS["screen"],
        )
        overlapping = seed_registry.build_allocation_record(
            owner_issue="lisbun/lisjong-arena#1",
            protocol="other",
            seed_domain="riichienv-4p-red-half-hanchan-v1",
            purpose="test",
            population="other",
            split="FORMAL-EVAL",
            seeds=(fx.ACTIVATION_SEEDS[0],),
            arena_revision="e" * 40,
            protocol_revision="test",
            provenance_reference="test",
            allocation_timestamp="2026-10-22T00:00:00Z",
        )
        external = fx.allocation("activation", (*fx.ACTIVATION_SEEDS[:7], 472_005))
        python = {**fx.ENVIRONMENT, "shanten_backend": {"backend": "python", "lisjong_revision": fx.LISJONG_REVISION}}  # fmt: skip
        other_native = {
            **fx.ENVIRONMENT,
            "native": {**fx.ENVIRONMENT["native"], "source_revision": "f" * 40},
        }
        cases = [
            ({"record": formal, "ledger": fx.ledger(formal)}, "split"),
            ({"record": other_owner, "ledger": fx.ledger(other_owner)}, "owner_issue"),
            ({"record": screen_population, "ledger": fx.ledger(screen_population)}, "population"),
            ({"ledger": fx.ledger(fx.ACTIVATION_RECORD, overlapping)}, "collide"),
            ({"record": external, "ledger": fx.ledger(external), "ordered_seeds": (*fx.ACTIVATION_SEEDS[:7], 472_005)}, "outside the registry"),
            ({"ordered_seeds": fx.ACTIVATION_SEEDS[:7]}, "exactly 8"),
            ({"timeout_seconds": 0}, "positive int"),
            ({"timeout_seconds": True}, "positive int"),
            ({"resource_limits": {**fx.LIMITS, "wall_seconds": 0}}, "positive int"),
            ({"resource_limits": {**fx.LIMITS, "enforcement": " "}}, "enforcement"),
            ({"screen_limits": None}, "resource_limits"),
            ({"prerequisite": {"activation_plan_digest": "1" * 64}}, "only an allocated screen"),
            ({"wheel_path": "relative.whl"}, "absolute"),
            ({"environment": python}, "rust"),
            ({"environment": other_native}, "SOURCE_REVISION"),
            ({"arena_revision": "f" * 40}, "Arena revision"),
            ({"destinations": {"output": str(self.tmp / "x")}}, "destinations"),
            ({"stage": "formal"}, "stage"),
        ]  # fmt: skip
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    fx.plan_document(self.tmp, **overrides)

    def test_screen_plan_requires_the_stage_1_binding_and_its_limits(self):
        cases = [
            ({"prerequisite": None}, "stage-1 result"),
            (
                {
                    "prerequisite": {
                        "activation_plan_digest": "1" * 64,
                        "activation_summary_digest": "2" * 64,
                        "screen_limits": fx.LIMITS,
                    }
                },
                "limits differ",
            ),
            ({"screen_limits": fx.SCREEN_LIMITS}, "activation plan"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    fx.plan_document(self.tmp, stage="screen", **overrides)

    def test_saved_plan_tampering_is_rejected(self):
        plan = fx.plan_document(self.tmp)
        criteria = {**plan["criteria"], "minimum_changes": 16}
        for key, value in (
            ("criteria", criteria),
            ("not_a_formal_result", False),
            ("evaluation_kind", "FORMAL-EVAL"),
            ("run_kind", "rehearsal"),
            ("plan_digest", "0" * 64),
        ):
            with self.subTest(key=key):
                with self.assertRaises(EngineAabbRecordError):
                    paijia_tie_plan.parse_plan({**plan, key: value})
        swapped = {
            **plan,
            "participants": {
                "A": plan["participants"]["B"],
                "B": plan["participants"]["A"],
            },
        }
        with self.assertRaisesRegex(EngineAabbRecordError, "fixed pair"):
            paijia_tie_plan.parse_plan(swapped)

    def test_formal_lock_and_calibration_refuse_the_520_allocation(self):
        document = fx.ledger()
        binding = fx.binding(document, fx.SCREEN_RECORD)
        with self.assertRaisesRegex(EngineAabbLockError, "allocation"):
            require_population_seeds(document, binding, fx.SCREEN_SEEDS, event=520)
        with self.assertRaisesRegex(EngineAabbRecordError, "allocation"):
            calibration.require_calibration_seeds(document, binding, fx.SCREEN_SEEDS)

    def test_live_gate_requires_head_environment_and_an_active_allocation(self):
        document = fx.ledger()
        plan = fx.plan_document(self.tmp, ledger=document)
        fx.gate(plan, ledger=document)
        retired = seed_registry.transition_allocation(
            document, fx.ACTIVATION_RECORD["allocation_identity"], state="RETIRED"
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "not active"):
            fx.gate(plan, ledger=retired)
        with self.assertRaisesRegex(EngineAabbRecordError, "HEAD"):
            paijia_tie_plan.require_live_target(
                plan, ledger=document, environment=fx.ENVIRONMENT, head="f" * 40
            )
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal")
        fx.gate(rehearsal, ledger=None)


# ---------------------------------------------------------------------------
# 段階1
# ---------------------------------------------------------------------------


class ActivationRunTest(_Case):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.plan_path = self.save(fx.plan_document(self.tmp))

    def save(self, plan, name="activation-plan.json"):
        self.plan = plan
        self.output = Path(plan["artifact_destinations"]["output"])
        self.diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
        return paijia_tie_plan.save_plan(plan, self.tmp / name)

    def run_activation(self, play=fx.fake_activation_proceed, **kwargs):
        return paijia_tie_activation.run_activation(
            self.plan_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=fx.gate,
            **kwargs,
        )

    def test_threshold_is_met_exactly_and_verifies(self):
        summary = self.run_activation()
        self.assertEqual(summary["gate"]["decision"], "PROCEED")
        activation = summary["activation"]
        self.assertEqual(activation["changes"], 32)
        self.assertEqual(activation["tile_type_changes"], 16)
        self.assertEqual(activation["red_only_changes"], 16)
        self.assertEqual(activation["seat_hanchans"], 32)
        self.assertEqual(activation["changes_per_seat_hanchan"], 1.0)
        self.assertEqual(activation["seeds_with_change"], 8)
        self.assertEqual(activation["decisions"], 8 * 4 * 3)
        self.assertEqual(activation["gate_passed_decisions"], 32)
        self.assertEqual(activation["by_stage"]["NO_FINAL_TIE"], 32)
        self.assertEqual(sum(activation["invalid"].values()), 0)
        self.assertEqual(summary["decision_seconds"]["A_shadow"]["count"], 96)
        self.assertEqual(summary["evaluation_kind"], "DEVELOPMENT")
        self.assertIs(summary["not_a_strength_result"], True)
        for forbidden in ("mean_d", "statistic", "classification", "blocks"):
            self.assertNotIn(forbidden, summary)
        projection = summary["screen_projection"]
        self.assertEqual(projection["screen_hanchans"], 192)
        self.assertEqual(projection["worst_case_screen_seconds_at_timeout"], 192 * 60)
        self.assertTrue(projection["within_budget"])
        hanchans = json.loads((self.output / "hanchans.json").read_text("utf-8"))
        self.assertEqual(len(hanchans["hanchans"]), 8)
        self.assertEqual(len(hanchans["hanchans"][0]["final_points"]), 4)
        timings = json.loads((self.output / "timings.json").read_text("utf-8"))
        for name in ("realtime_elapsed_seconds", "monotonic_elapsed_seconds"):
            self.assertGreaterEqual(timings[name], 0)

        verified = paijia_tie_activation.verify_activation(self.plan_path)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertEqual(verified["decision"], "PROCEED")
        self.assertEqual(
            sorted(item["path"] for item in verified["files"]),
            ["hanchans.json", "shadow.json", "summary.json", "timings.json"],
        )
        self.assertIn("realtime_minus_monotonic_seconds", verified["clocks"])
        prerequisite = paijia_tie_activation.screen_prerequisite(self.plan_path)
        self.assertEqual(
            prerequisite["activation_summary_digest"], summary["summary_digest"]
        )
        self.assertEqual(prerequisite["screen_limits"], fx.SCREEN_LIMITS)

    def test_verify_in_a_separate_process(self):
        summary = self.run_activation()
        code = (
            "import sys\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb import paijia_tie_activation as m\n"
            "print(m.verify_activation(sys.argv[1])['summary_digest'])\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(self.plan_path)], text=True
        )
        self.assertEqual(output.strip(), summary["summary_digest"])

    def test_gate_decisions_in_fixed_order(self):
        cases = [
            (fx.fake_activation_low, "STOP-LOW-ACTIVATION", 31, 8),
            (fx.fake_activation_concentrated, "STOP-LOW-ACTIVATION", 12, 3),
            (fx.fake_activation_many_in_few_seeds, "STOP-LOW-ACTIVATION", 36, 3),
            (fx.fake_activation_invalid, "STOP-INVALID", 32, 8),
        ]
        for index, (play, decision, changes, seeds) in enumerate(cases):
            with self.subTest(play=play.__name__):
                plan = fx.plan_document(
                    self.tmp,
                    destinations={
                        "diagnostics": str(self.tmp / f"d{index}"),
                        "output": str(self.tmp / f"o{index}"),
                    },
                )
                self.plan_path = self.save(plan, f"plan-{index}.json")
                summary = self.run_activation(play)
                self.assertEqual(summary["gate"]["decision"], decision)
                self.assertEqual(summary["activation"]["changes"], changes)
                self.assertEqual(summary["activation"]["seeds_with_change"], seeds)
                if decision != "PROCEED":
                    with self.assertRaisesRegex(EngineAabbRecordError, decision):
                        paijia_tie_activation.screen_prerequisite(self.plan_path)
        low = json.loads((self.tmp / "o0" / "summary.json").read_text("utf-8"))
        self.assertEqual(low["activation"]["tsumogiri_only_differences"], 1)
        invalid = json.loads((self.tmp / "o3" / "summary.json").read_text("utf-8"))
        self.assertEqual(invalid["activation"]["invalid"]["out-of-scope-difference"], 1)

    def test_budget_gate_uses_the_fixed_screen_limit(self):
        plan = fx.plan_document(
            self.tmp,
            screen_limits={**fx.SCREEN_LIMITS, "wall_seconds": 1},
            destinations={
                "diagnostics": str(self.tmp / "bd"),
                "output": str(self.tmp / "bo"),
            },
        )
        self.plan_path = self.save(plan, "budget-plan.json")
        summary = self.run_activation()
        self.assertEqual(summary["gate"]["decision"], "STOP-BUDGET")
        self.assertFalse(summary["screen_projection"]["within_budget"])

    def test_rehearsal_has_no_gate(self):
        self.plan_path = self.save(
            fx.plan_document(self.tmp, run_kind="rehearsal"), "rehearsal.json"
        )
        summary = self.run_activation()
        self.assertIsNone(summary["gate"])
        self.assertEqual(summary["status"], "REHEARSAL COMPLETE; NO GATE DECISION")
        self.assertEqual(summary["hanchan_count"], 2)
        self.assertIsNone(
            paijia_tie_activation.verify_activation(self.plan_path)["decision"]
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "allocated stage-1"):
            paijia_tie_activation.screen_prerequisite(self.plan_path)

    def test_failures_stop_without_output(self):
        cases = [
            (fx.fake_activation_count_mismatch, "Policy call count"),
            (fx.fake_activation_no_analysis, "no Policy observation"),
        ]
        for play, message in cases:
            with self.subTest(play=play.__name__):
                shutil.rmtree(self.diagnostics, ignore_errors=True)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    self.run_activation(play)
                self.assertFalse(self.output.exists())
                (stop,) = self.diagnostics.glob("stop-*.json")
                stop = json.loads(stop.read_text("utf-8"))
                self.assertEqual(stop["status"], "STOP-INCOMPLETE")
                self.assertTrue(stop["not_a_result"])

    def test_wall_limit_uses_the_longer_of_monotonic_and_realtime(self):
        ticks = iter(range(10**9))
        with self.assertRaisesRegex(RunStopped, "wall limit"):
            # monotonicは進まず、realtimeだけが進む（#517の時計差）
            self.run_activation(
                clock=lambda: 0.0, realtime=lambda: next(ticks) * 3600.0
            )
        self.assertFalse(self.output.exists())

    def test_tampering_is_rejected(self):
        self.run_activation()
        shadow_path = self.output / "shadow.json"
        original = shadow_path.read_text("utf-8")
        shadow = json.loads(original)
        shadow["hanchans"][0]["seats"][0]["decisions"][0]["outcome"] = "same"
        shadow_path.write_text(canonical_json_text(shadow), "utf-8", newline="\n")
        with self.assertRaises(EngineAabbRecordError):
            paijia_tie_activation.verify_activation(self.plan_path)
        shadow = json.loads(original)
        shadow["hanchans"][0]["seats"][0]["decisions"][1]["outcome"] = (
            "tile-type-change"
        )
        shadow_path.write_text(canonical_json_text(shadow), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "detail|PAIJIA_SWITCH"):
            paijia_tie_activation.verify_activation(self.plan_path)
        shadow = json.loads(original)
        shadow["hanchans"][0]["seats"][0]["decisions"][0]["a_seconds"] = 1e6
        shadow_path.write_text(canonical_json_text(shadow), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "exceed the hanchan wall"):
            paijia_tie_activation.verify_activation(self.plan_path)
        shadow_path.write_text(original, "utf-8", newline="\n")
        timings_path = self.output / "timings.json"
        timings = json.loads(timings_path.read_text("utf-8"))
        timings["hanchans"][0]["kyoku_count"] += 1
        timings_path.write_text(canonical_json_text(timings), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "kyoku_count"):
            paijia_tie_activation.verify_activation(self.plan_path)

    def test_completed_stage_is_never_rerun(self):
        self.run_activation()
        with self.assertRaisesRegex(EngineAabbRecordError, "write-once"):
            self.run_activation()


# ---------------------------------------------------------------------------
# 段階2
# ---------------------------------------------------------------------------


class ScreenRunTest(_Case):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.plan_path = self.save(fx.plan_document(self.tmp, stage="screen"))

    def save(self, plan, name="screen-plan.json"):
        self.plan = plan
        self.output = Path(plan["artifact_destinations"]["output"])
        self.diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
        return paijia_tie_plan.save_plan(plan, self.tmp / name)

    def run_screen(self, play=fx.fake_screen, **kwargs):
        return paijia_tie_screen.run_screen(
            self.plan_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=fx.gate,
            **kwargs,
        )

    def test_complete_screen_verify_and_report(self):
        summary = self.run_screen()
        self.assertEqual(
            summary["status"], "SCREEN COMPLETE; DEVELOPMENT, NOT A FORMAL RESULT"
        )
        self.assertEqual(summary["hanchan_count"], 18)
        self.assertEqual(summary["event_fact_status"], {"status": "VALID"})
        self.assertEqual(
            [(b["seed"], b["difference_internal"]) for b in summary["blocks"]],
            [(fx.SCREEN_SEEDS[0], -1000), (fx.SCREEN_SEEDS[1], 0), (fx.SCREEN_SEEDS[2], 0)],
        )  # fmt: skip
        statistic = summary["statistic"]
        self.assertAlmostEqual(statistic["mean_d"], -1000 / 120 / 3)
        self.assertNotIn("classification", statistic)
        self.assertNotIn("CANDIDATE SUPERIOR", json.dumps(summary))
        progression = summary["progression"]
        self.assertEqual(progression["decision"], "NOT-PROMISING-DEVELOPMENT")
        self.assertFalse(progression["checks"]["mean_d_pt"]["pass"])
        activation = summary["activation"]
        # A席は1半荘2席、各席1件のPAIJIA_SWITCH（交互に種類が違う）
        self.assertEqual(activation["changes"], 18 * 2)
        self.assertEqual(activation["seeds_with_change"], 3)
        self.assertEqual(summary["decision_seconds"]["A"]["count"] + summary["decision_seconds"]["B"]["count"], 18 * 14)  # fmt: skip
        self.assertIn("deal_in_rate", summary["secondary_metrics"]["kyoku_metrics"]["paired"])  # fmt: skip

        verified = paijia_tie_screen.verify_screen(self.plan_path)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertEqual(verified["decision"], "NOT-PROMISING-DEVELOPMENT")
        paths = [item["path"] for item in verified["files"]]
        self.assertIn("raw/manifest.json", paths)
        self.assertIn("policy-analysis.json", paths)

        report = paijia_tie_screen.write_report(self.plan_path)
        self.assertEqual(report["memory"]["worker_peak_rss_kib_max"], 4096)
        self.assertGreater(report["storage"]["raw_bytes"], 0)
        self.assertGreater(report["storage"]["analysis_bytes"], 0)
        self.assertEqual(report["timing"]["timeout_seconds"], 60)
        self.assertEqual(
            paijia_tie_screen.verify_screen(self.plan_path)["report_digest"],
            report["report_digest"],
        )
        with self.assertRaises(FileExistsError):
            paijia_tie_screen.write_report(self.plan_path)

    def test_rehearsal_runs_the_same_path_without_progression(self):
        self.plan_path = self.save(
            fx.plan_document(self.tmp, stage="screen", run_kind="rehearsal"),
            "rehearsal.json",
        )
        summary = self.run_screen()
        self.assertIsNone(summary["progression"])
        self.assertEqual(summary["hanchan_count"], 12)
        self.assertEqual(
            paijia_tie_screen.verify_screen(self.plan_path)["status"],
            "REHEARSAL COMPLETE; NO PROGRESSION DECISION",
        )

    def test_failures_stop_without_output(self):
        cases = [
            (fx.fake_screen_bad_analysis, "Policy call count"),
            (fx.fake_screen_missing_evidence, ""),
            (fx.fake_screen_invariant_stop, "synthetic analysis inconsistency"),
        ]
        for play, message in cases:
            with self.subTest(play=play.__name__):
                shutil.rmtree(self.diagnostics, ignore_errors=True)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    self.run_screen(play)
                self.assertFalse(self.output.exists())
                self.assertEqual(len(list(self.diagnostics.glob("stop-*.json"))), 1)

    def test_rss_limit_stops_the_screen(self):
        limits = {**fx.SCREEN_LIMITS, "process_tree_rss_bytes": 1}
        plan = fx.plan_document(
            self.tmp,
            stage="screen",
            resource_limits=limits,
            prerequisite={
                "activation_plan_digest": "1" * 64,
                "activation_summary_digest": "2" * 64,
                "screen_limits": limits,
            },
            destinations={
                "diagnostics": str(self.tmp / "rd"),
                "output": str(self.tmp / "ro"),
            },
        )
        self.plan_path = self.save(plan, "rss.json")
        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            self.run_screen()
        self.assertFalse(self.output.exists())

    def test_tampering_is_rejected(self):
        self.run_screen()
        path = self.output / "policy-analysis.json"
        analysis = json.loads(path.read_text("utf-8"))
        analysis["hanchans"][0]["seats"][0]["switches"] = []
        path.write_text(canonical_json_text(analysis), "utf-8", newline="\n")
        with self.assertRaisesRegex(EngineAabbRecordError, "switches"):
            paijia_tie_screen.verify_screen(self.plan_path)


class ProgressionTest(_Case):
    def secondary(self, base_differences, deal_in):
        return {
            "kyoku_metrics": {"paired": {"deal_in_rate": {"mean_difference": deal_in}}},
            "point_decomposition": [
                {"difference": {"base_points": value}} for value in base_differences
            ],
        }

    def test_all_conditions_and_each_failure(self):
        plan = fx.plan_document(self.tmp, stage="screen")
        decide = paijia_tie_screen.progression
        promising = decide(plan, {"mean_d": 0.5}, self.secondary([120, 0], 0.01))
        self.assertEqual(promising["decision"], "PROMISING-DEVELOPMENT")
        self.assertAlmostEqual(
            promising["checks"]["base_points_mean_difference_pt"]["value"], 0.5
        )
        failures = [
            ({"mean_d": 0.0}, self.secondary([120], 0.0), "mean_d_pt"),
            ({"mean_d": 1.0}, self.secondary([-120, 0], 0.0), "base_points_mean_difference_pt"),
            ({"mean_d": 1.0}, self.secondary([0], 0.0101), "deal_in_rate_mean_difference"),
            ({"mean_d": 1.0}, self.secondary([0], None), "deal_in_rate_mean_difference"),
        ]  # fmt: skip
        for statistic, secondary, failed in failures:
            with self.subTest(failed=failed):
                result = decide(plan, statistic, secondary)
                self.assertEqual(result["decision"], "NOT-PROMISING-DEVELOPMENT")
                self.assertFalse(result["checks"][failed]["pass"])


# ---------------------------------------------------------------------------
# worker（lisjong #330の実際の型）
# ---------------------------------------------------------------------------


def _tiles():
    from lisjong.policy_contract import Tile, TileCategory, TileType

    def tile(category, rank, red=False):
        return Tile(TileType(category, rank), is_red=red)

    return tile, TileCategory


def _discard(tile, tsumogiri=False):
    from lisjong.policy_contract import DiscardAction, Seat

    return DiscardAction(actor=Seat(0), tile=tile, tsumogiri=tsumogiri)


def _analysis(stage, champion, selected, evaluations):
    from lisjong.policies.placement_aware_speed_call_suited_paijia_tie import (
        SuitedPaijiaCandidateEvaluation,
        SuitedPaijiaTieAnalysis,
        SuitedPaijiaTieStage,
    )

    return SuitedPaijiaTieAnalysis(
        stage=SuitedPaijiaTieStage[stage],
        champion_action=champion,
        selected_action=selected,
        action_changed=champion != selected,
        paijia_evaluations=tuple(
            SuitedPaijiaCandidateEvaluation(action=action, paijia=paijia)
            for action, paijia in evaluations
        ),
    )


class ClassifyShadowTest(unittest.TestCase):
    def setUp(self):
        tile, category = _tiles()
        self.m2 = _discard(tile(category.MANZU, 2))
        self.m5 = _discard(tile(category.MANZU, 5))
        self.m5_red = _discard(tile(category.MANZU, 5, red=True))
        self.m5_tsumogiri = _discard(tile(category.MANZU, 5), tsumogiri=True)
        self.p9 = _discard(tile(category.PINZU, 9))
        self.east = _discard(tile(category.HONOR, 1))
        self.classify = paijia_tie_worker.classify_shadow

    def test_switch_kinds(self):
        switch = _analysis(
            "PAIJIA_SWITCH", self.m5, self.p9, [(self.m5, 9), (self.p9, 3)]
        )
        self.assertEqual(self.classify(self.m5, self.p9, switch)[0], "tile-type-change")
        red = _analysis(
            "PAIJIA_SWITCH", self.m5, self.m5_red, [(self.m5, 9), (self.m5_red, 3)]
        )
        self.assertEqual(self.classify(self.m5, self.m5_red, red)[0], "red-only-change")
        tsumogiri = _analysis(
            "PAIJIA_SWITCH",
            self.m5_tsumogiri,
            self.m5,
            [(self.m5_tsumogiri, 9), (self.m5, 3), (self.m2, 5)],
        )
        self.assertEqual(
            self.classify(self.m5_tsumogiri, self.m5, tsumogiri)[0], "tsumogiri-only"
        )  # fmt: skip

    def test_same_actions(self):
        minimum = _analysis(
            "CHAMPION_MINIMUM_PAIJIA", self.m5, self.m5, [(self.m5, 3), (self.p9, 3)]
        )
        self.assertEqual(
            self.classify(self.m5, self.m5, minimum), ("same", "CHAMPION_MINIMUM_PAIJIA", [])
        )  # fmt: skip
        keep = _analysis("NO_FINAL_TIE", self.m5, self.m5, [])
        self.assertEqual(self.classify(self.m5, self.m5, keep)[0], "same")
        from lisjong.policy_contract import PassAction, Seat

        passing = PassAction(actor=Seat(0))
        self.assertEqual(
            self.classify(passing, passing, None), ("same", "NO_ANALYSIS", [])
        )

    def test_invalid_outcomes(self):
        keep = _analysis("NO_FINAL_TIE", self.m5, self.m5, [])
        # analysisのChampion行動が実際のBと違う
        outcome, _, violations = self.classify(self.m2, self.m5, keep)
        self.assertEqual(outcome, "analysis-invariant-violation")
        self.assertIn("champion_action", violations[0])
        # 返した行動がanalysis.selected_actionと違う
        self.assertEqual(
            self.classify(self.m5, self.m2, keep)[0], "analysis-invariant-violation"
        )
        # analysisなしの判断で行動が違う（打牌でない行動）
        from lisjong.policy_contract import PassAction, Seat

        self.assertEqual(
            self.classify(self.m5, PassAction(actor=Seat(0)), None)[0],
            "out-of-scope-difference",
        )
        # 打牌なのにanalysisがない（lisjongのanalysis経路を通っていない）
        self.assertEqual(
            self.classify(self.m5, self.m5, None)[0], "analysis-invariant-violation"
        )
        self.assertEqual(
            self.classify(self.m5, None, None)[0], "candidate-illegal-action"
        )

    def test_analysis_value_relations(self):
        violations = paijia_tie_worker.analysis_violations
        cases = [
            (_analysis("PAIJIA_SWITCH", self.m5, self.p9, [(self.m5, 3), (self.p9, 3)]), "already minimum"),
            (_analysis("PAIJIA_SWITCH", self.m5, self.p9, [(self.m5, 9), (self.p9, 5), (self.m2, 1)]), "not the minimum"),
            (_analysis("CHAMPION_MINIMUM_PAIJIA", self.m5, self.m5, [(self.m5, 9), (self.p9, 5)]), "not minimum"),
            (_analysis("PAIJIA_SWITCH", self.m5, self.east, [(self.m5, 9), (self.east, 3)]), "honor"),
            (_analysis("PAIJIA_SWITCH", self.m5, self.p9, [(self.p9, 3), (self.m2, 5)]), "not among"),
        ]  # fmt: skip
        for analysis, message in cases:
            with self.subTest(message=message):
                found = violations(analysis, analysis.selected_action)
                self.assertTrue(any(message in item for item in found), found)
        good = _analysis("PAIJIA_SWITCH", self.m5, self.p9, [(self.m5, 9), (self.p9, 3)])  # fmt: skip
        self.assertEqual(violations(good, self.p9), [])


class _FakePolicy:
    def __init__(self, action, analysis=None, calls=None):
        self.action = action
        self.analysis = analysis
        self.calls = calls if calls is not None else []

    def choose_action(self, decision):
        self.calls.append(decision)
        return self.action


class ShadowProxyTest(unittest.TestCase):
    """B（実行経路の1回）だけを適用し、Aは判断ごとに新しく作って採点することを確かめる。"""

    def test_only_the_incumbent_action_is_applied(self):
        tile, category = _tiles()
        b_action = _discard(tile(category.MANZU, 5))
        a_action = _discard(tile(category.PINZU, 9))
        analysis = _analysis(
            "PAIJIA_SWITCH", b_action, a_action, [(b_action, 9), (a_action, 3)]
        )
        incumbent_calls, candidates = [], []

        def new_candidate():
            candidate = _FakePolicy(a_action, analysis)
            candidates.append(candidate)
            return candidate

        def decide(policy, decision):
            policy.calls.append(decision)
            return policy.action, policy.analysis

        rows = []
        proxy = paijia_tie_worker._ShadowedIncumbent(
            _FakePolicy(b_action, calls=incumbent_calls), new_candidate, rows
        )
        with (
            mock.patch(
                "lisjong.policy_contract.execute_policy",
                lambda policy, decision: policy.choose_action(decision),
            ),
            mock.patch.object(paijia_tie_worker, "_decide_with_analysis", decide),
        ):
            first = proxy.choose_action("decision-1")
            second = proxy.choose_action("decision-2")
        self.assertIs(first, b_action)
        self.assertIs(second, b_action)
        self.assertEqual(incumbent_calls, ["decision-1", "decision-2"])
        self.assertEqual(len(candidates), 2)  # 判断ごとに新しいA
        self.assertEqual(
            [c.calls for c in candidates], [["decision-1"], ["decision-2"]]
        )
        self.assertEqual([row["outcome"] for row in rows], ["tile-type-change"] * 2)
        self.assertEqual(rows[0]["stage"], "PAIJIA_SWITCH")
        self.assertEqual(rows[0]["detail"]["violations"], [])
        self.assertEqual(
            rows[0]["detail"]["incumbent_action"]["__type"], "DiscardAction"
        )


class PlayTaskWrapperTest(unittest.TestCase):
    """executorのwrap_policy hookは、Noneなら従来と同じで、与えればseat・roleを渡す。"""

    def test_wrap_policy_receives_seat_role_and_fresh_policy(self):
        from _engine_aabb_protocol_fixtures import CANDIDATE, INCUMBENT

        from lisjong_arena.heuristic_candidate_engine_aabb import executor

        seen = []
        created = []

        def fake_factory(**_):
            policy = object()
            created.append(policy)
            return policy

        def fake_run(policies, **kwargs):
            raise _Stop(policies)

        def wrap(seat, role, policy):
            seen.append((seat.name, role, policy))
            return policy

        task = HanchanTask(
            seed=1,
            placement=0,
            candidate=CANDIDATE.to_document(),
            incumbent=INCUMBENT.to_document(),
            wheel_path="w",
        )
        with (
            mock.patch.object(executor, "resolve_factory", lambda _: fake_factory),
            mock.patch.object(executor, "run_policy_hanchan", fake_run),
        ):
            with self.assertRaises(_Stop):
                play_task(task, wrap_policy=wrap)
            with self.assertRaises(_Stop) as unwrapped:
                play_task(task)
        self.assertEqual([role for _, role, _ in seen], ["A", "A", "B", "B"])
        self.assertEqual(len({id(policy) for _, _, policy in seen}), 4)
        proxies = unwrapped.exception.policies
        self.assertEqual(
            [proxy._policy for proxy in proxies.values()], created[4:]
        )  # fmt: skip


class _Stop(Exception):
    def __init__(self, policies):
        super().__init__("stop")
        self.policies = policies


if __name__ == "__main__":
    unittest.main()
