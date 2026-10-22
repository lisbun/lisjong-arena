"""#522 混成focal比較（DEVELOPMENT）のprotocol・plan・実行・verify（合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘と合成したPolicy観測を
返す ``fake_*`` を使う。Kは32の代わりに3へ差し替える（workerはKを参照しない）。
手計算値は ``_engine_focal_mixed_fixtures`` のdocstringにある。強さの結果ではない。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_focal_mixed_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    focal_mixed_compare,
    focal_mixed_plan,
    focal_mixed_worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.errors import (
    EngineAabbRecordError,
    KyokuRecordError,
    SecondaryMetricError,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    validate_hanchan_row,
)
from lisjong_arena.heuristic_candidate_engine_aabb.paijia_tie_worker import (
    _AnalyzedCandidate,
    _TimedIncumbent,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import PROTOCOL_DIGEST
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    read_kyoku_records,
)
from lisjong_arena.heuristic_candidate_engine_aabb.secondary_metrics import (
    summarize_seat_groups,
)

A = "placement-aware-speed-call-suited-paijia-tie-v1"
B = "placement-aware-speed-call"
O2 = "targeted-honor-release-terminal-progression"
O3 = "placement-aware-speed-call-kobalab-0004-belief-paijia"
DEAL_IN_SEED_1_2 = 8 / 34 - 8 / 32


class _Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(focal_mixed_plan, "K", len(fx.SEEDS))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


# ---------------------------------------------------------------------------
# protocol・schedule
# ---------------------------------------------------------------------------


class ScheduleTest(unittest.TestCase):
    def test_seat_assignment_and_eight_hanchan_order(self):
        items = focal_mixed_plan.schedule([7, 9])
        self.assertEqual(len(items), 16)
        self.assertEqual(
            [(item["seed"], item["rotation"], item["condition"]) for item in items[:8]],
            [(7, 0, "A"), (7, 0, "B"), (7, 1, "A"), (7, 1, "B"),
             (7, 2, "A"), (7, 2, "B"), (7, 3, "A"), (7, 3, "B")],
        )  # fmt: skip
        self.assertEqual(items[8]["seed"], 9)
        roles = {item["rotation"]: item["roles"] for item in items}
        self.assertEqual(roles[0], ["focal", "O1", "O2", "O3"])
        self.assertEqual(roles[1], ["O3", "focal", "O1", "O2"])
        self.assertEqual(roles[2], ["O2", "O3", "focal", "O1"])
        self.assertEqual(roles[3], ["O1", "O2", "O3", "focal"])

    def test_participants_and_protocol_are_fixed(self):
        entries = focal_mixed_plan.participants(fx.LISJONG_REVISION)
        self.assertEqual(
            {role: item.policy_identity for role, item in entries.items()},
            {"A": A, "B": B, "O1": B, "O2": O2, "O3": O3},
        )
        self.assertTrue(
            all(item.behavior_configuration == {} for item in entries.values())
        )
        self.assertNotEqual(focal_mixed_plan.PROTOCOL_DIGEST, PROTOCOL_DIGEST)
        self.assertEqual(focal_mixed_plan.DIVISOR, 40)
        self.assertEqual(focal_mixed_plan.COMPARE_HANCHANS, 256)


# ---------------------------------------------------------------------------
# focal endpoint・LOO・進行条件（純粋な関数、手計算）
# ---------------------------------------------------------------------------


def _row(seed, rotation, condition, points):
    roles = focal_mixed_plan.seat_roles(rotation)
    identities = {"focal": A if condition == "A" else B, "O1": B, "O2": O2, "O3": O3}
    return {
        "condition": condition,
        "final_score": [{"final_points": value} for value in points],
        "rotation": rotation,
        "seats": [
            {"policy_identity": identities[role], "role": role} for role in roles
        ],
        "seed": seed,
    }


def _block_rows(seed, a_points, b_points):
    """条件ごとに、seat 0..3の値をそのまま使う（focalはseat r）。"""
    return [
        _row(seed, rotation, condition, points)
        for rotation in range(4)
        for condition, points in (("A", a_points), ("B", b_points))
    ]


def _secondary(base_rows, deal_in):
    return {
        "kyoku_metrics": {"paired": {"deal_in_rate": {"mean_difference": deal_in}}},
        "point_decomposition": [
            {"difference": {"base_points": value}} for value in base_rows
        ],
    }


def _plan_criteria():
    return {"criteria": focal_mixed_plan.CRITERIA}


def _blocks(differences):
    return [
        {"difference_internal": value, "seed": index}
        for index, value in enumerate(differences, start=1)
    ]


class FocalEndpointTest(unittest.TestCase):
    def test_focal_is_the_role_at_seat_r_not_an_identity(self):
        # 条件BではfocalとO1が同じidentity。seat r+1（O1）の値を大きくしておく
        b_points = (100, 900, -400, -600)
        rows = _block_rows(5, (0, 0, 0, 0), b_points)
        for row in rows:
            self.assertEqual(focal_mixed_compare.focal_seat(row), row["rotation"])
        block = focal_mixed_compare.focal_blocks(rows)[0]
        self.assertEqual(block["f_b_internal"], [100, 900, -400, -600])
        self.assertEqual(block["f_a_internal"], [0, 0, 0, 0])
        # 4 rotationの和はゼロ和の1半荘分 = 0
        self.assertEqual(block["difference_internal"], 0)
        swapped = json.loads(json.dumps(rows))
        swapped[1]["seats"][0]["role"], swapped[1]["seats"][1]["role"] = "O1", "focal"
        with self.assertRaisesRegex(EngineAabbRecordError, "focal seat at seat r"):
            focal_mixed_compare.focal_blocks(swapped)

    def test_divisor_40_and_seed_equal_mean(self):
        rows = _block_rows(1, (40, 0, 0, -40), (0, 0, 0, 0))
        rows[0]["final_score"][0]["final_points"] = 400  # r = 0, 条件A
        rows += _block_rows(2, (0, 0, 0, 0), (0, 0, 0, 0))
        rows[8 + 6]["final_score"][3]["final_points"] = -80  # seed 2、r = 3、条件A
        blocks = focal_mixed_compare.focal_blocks(rows)
        # seed 1: 400 + 0 + 0 - 40 = 360 -> 9.0 pt、seed 2: -80 -> -2.0 pt
        self.assertEqual([b["difference_internal"] for b in blocks], [360, -80])
        self.assertEqual([b["d"] for b in blocks], [9.0, -2.0])
        statistic = focal_mixed_compare.summarize_d([360, -80])
        self.assertEqual(statistic["mean_d"], 3.5)
        self.assertAlmostEqual(statistic["sd"], (2 * 5.5**2) ** 0.5)
        half = 1.96 * statistic["sd"] / 2**0.5
        self.assertAlmostEqual(statistic["lower"], 3.5 - half)
        self.assertTrue(statistic["interval_crosses_zero"])

    def test_order_within_a_seed_block_is_required(self):
        rows = _block_rows(1, (0,) * 4, (0,) * 4)
        rows[0], rows[1] = rows[1], rows[0]
        with self.assertRaisesRegex(EngineAabbRecordError, "in order"):
            focal_mixed_compare.focal_blocks(rows)
        with self.assertRaisesRegex(EngineAabbRecordError, "8K"):
            focal_mixed_compare.focal_blocks(rows[:7])

    def test_leave_one_seed_out_and_the_zero_boundary(self):
        loo = focal_mixed_compare.leave_one_seed_out(_blocks([-40, 40, 80]))
        # seed 3を除くと (-40 + 40) / 2 = 0
        self.assertEqual(loo["minimum_numerator_internal"], 0)
        self.assertEqual(loo["minimum_mean_d"], 0.0)
        self.assertEqual(loo["minimum_seed"], 3)
        self.assertEqual(loo["without_largest_positive_block"]["seed"], 3)
        self.assertEqual(
            [entry["mean_d_without"] for entry in loo["per_seed"]],
            [120 / 2 / 40, 40 / 2 / 40, 0.0],
        )
        none_positive = focal_mixed_compare.leave_one_seed_out(_blocks([-40, 0]))
        self.assertIsNone(none_positive["without_largest_positive_block"])
        self.assertIsNone(focal_mixed_compare.leave_one_seed_out(_blocks([40])))
        single = focal_mixed_compare.summarize_d([40])
        self.assertIsNone(single["sd"])
        self.assertIsNone(single["interval_crosses_zero"])

    def test_progression_conditions_each_failure(self):
        def run(differences, base_rows=(0,), deal_in=0.0):
            blocks = _blocks(differences)
            statistic = focal_mixed_compare.summarize_d(differences)
            loo = focal_mixed_compare.leave_one_seed_out(blocks)
            return focal_mixed_compare.progression(
                _plan_criteria(), blocks, statistic, loo, _secondary(base_rows, deal_in)
            )

        passed = run([-39, 40, 80])
        self.assertEqual(passed["decision"], "RECONFIRMED-DEVELOPMENT")
        self.assertTrue(all(check["pass"] for check in passed["checks"].values()))
        cases = {
            "leave_one_seed_out_minimum_mean_d_pt": run([-40, 40, 80]),
            "mean_d_pt": run([-80, 40, 40]),
            "base_points_mean_difference_pt": run([-39, 40, 80], base_rows=(1, -2)),
            "deal_in_rate_mean_difference": run([-39, 40, 80], deal_in=0.0101),
        }
        for name, result in cases.items():
            with self.subTest(failed=name):
                self.assertEqual(result["decision"], "NOT-RECONFIRMED-DEVELOPMENT")
                self.assertFalse(result["checks"][name]["pass"])
        # 境界: 素点差0・放銃率差ちょうど+0.01は通過、放銃率のnullは不通過
        self.assertTrue(
            run([-39, 40, 80], base_rows=(1, -1), deal_in=0.01)["checks"][
                "base_points_mean_difference_pt"
            ]["pass"]
        )
        self.assertTrue(
            run([-39, 40, 80], deal_in=0.01)["checks"]["deal_in_rate_mean_difference"][
                "pass"
            ]
        )
        missing = run([-39, 40, 80], deal_in=None)
        self.assertFalse(missing["checks"]["deal_in_rate_mean_difference"]["pass"])
        self.assertEqual(missing["decision"], "NOT-RECONFIRMED-DEVELOPMENT")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(_Case):
    def test_allocated_plan_binds_participants_population_and_criteria(self):
        plan = fx.plan_document(self.tmp)
        self.assertEqual(plan["protocol_id"], focal_mixed_plan.PROTOCOL_ID)
        self.assertEqual(plan["owner_issue"], "lisbun/lisjong-arena#522")
        self.assertEqual(
            {role: item["policy_identity"] for role, item in plan["participants"].items()},
            {"A": A, "B": B, "O1": B, "O2": O2, "O3": O3},
        )  # fmt: skip
        population = plan["population"]
        self.assertEqual(population["ordered_seeds"], list(fx.SEEDS))
        self.assertEqual(population["population"], focal_mixed_plan.POPULATION)
        self.assertEqual(population["split"], "DEVELOPMENT")
        self.assertEqual(plan["criteria"], focal_mixed_plan.CRITERIA)
        self.assertEqual(
            plan["criteria"]["conditions"]["deal_in_rate_mean_difference_at_most"], 0.01
        )
        self.assertIs(plan["not_a_formal_result"], True)
        self.assertEqual(focal_mixed_plan.parse_plan(plan), plan)

    def test_rehearsal_uses_the_development_seed_without_allocation(self):
        plan = fx.plan_document(self.tmp, run_kind="rehearsal")
        self.assertEqual(plan["population"]["ordered_seeds"], [472_005])
        self.assertNotIn("allocation_binding", plan["population"])
        with self.assertRaisesRegex(EngineAabbRecordError, "no allocation"):
            fx.plan_document(
                self.tmp, run_kind="rehearsal", allocation_binding={"x": 1}
            )
        with self.assertRaisesRegex(EngineAabbRecordError, "development seed"):
            fx.plan_document(self.tmp, run_kind="rehearsal", ordered_seeds=(472_006,))

    def test_plan_rejects_inputs_that_change_its_meaning(self):
        # 同じdomainの重なりはledger自体が拒否する。別domainの重なりもplanが拒否する
        other = fx.allocation(
            (520_100, fx.SEEDS[1]), owner="lisbun/lisjong-arena#520", domain="other-v1"
        )
        wrong = {
            "owner": fx.allocation(owner="lisbun/lisjong-arena#520"),
            "population": fx.allocation(population="other-population"),
            "split": fx.allocation(split="FORMAL-EVAL"),
            "protocol": fx.allocation(protocol="arena-heuristic-candidate-engine-aabb-half-v1"),
        }  # fmt: skip
        for name, record in wrong.items():
            with self.subTest(allocation=name):
                with self.assertRaises(EngineAabbRecordError):
                    fx.plan_document(self.tmp, ledger=fx.ledger(record), record=record)
        with self.assertRaisesRegex(EngineAabbRecordError, "collide"):
            fx.plan_document(self.tmp, ledger=fx.ledger(fx.RECORD, other))
        with self.assertRaisesRegex(EngineAabbRecordError, "exactly 3 seeds"):
            fx.plan_document(self.tmp, ordered_seeds=fx.SEEDS[:2])
        with self.assertRaisesRegex(EngineAabbRecordError, "outside the registry"):
            fx.plan_document(
                self.tmp, ordered_seeds=(fx.SEEDS[0], fx.SEEDS[1], 472_005)
            )
        cases = {
            "timeout_seconds": 0,
            "resource_limits": {**fx.LIMITS, "wall_seconds": 0},
            "wheel_path": "relative.whl",
            "destinations": {"output": str(self.tmp / "x")},
            "arena_revision": "f" * 40,
        }
        for name, value in cases.items():
            with self.subTest(argument=name):
                with self.assertRaises(EngineAabbRecordError):
                    fx.plan_document(self.tmp, **{name: value})

    def test_saved_plan_tampering_is_rejected(self):
        plan = fx.plan_document(self.tmp)
        path = focal_mixed_plan.save_plan(plan, self.tmp / "plan.json")
        self.assertEqual(focal_mixed_plan.load_plan(path), plan)
        with self.assertRaises(FileExistsError):
            focal_mixed_plan.save_plan(plan, path)

        def changed(edit):
            document = json.loads(json.dumps(plan))
            edit(document)
            return document

        edits = {
            "opponent": lambda d: d["participants"].__setitem__(
                "O2", d["participants"]["O3"]
            ),
            "criteria": lambda d: d["criteria"]["conditions"].__setitem__(
                "leave_one_seed_out_minimum_mean_d_pt_above", -1.0
            ),
            "seeds": lambda d: d["population"].__setitem__(
                "ordered_seeds", list(reversed(fx.SEEDS))
            ),
            "digest": lambda d: d.__setitem__("plan_digest", "0" * 64),
            "protocol": lambda d: d.__setitem__("protocol_digest", PROTOCOL_DIGEST),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                with self.assertRaises(EngineAabbRecordError):
                    focal_mixed_plan.parse_plan(changed(edit))

    def test_live_plan_requires_the_frozen_lisjong_and_wheel(self):
        with self.assertRaisesRegex(EngineAabbRecordError, "frozen"):
            focal_mixed_plan.require_frozen_environment(fx.ENVIRONMENT)
        frozen = json.loads(json.dumps(fx.ENVIRONMENT))
        frozen["provenance"]["lisjong_revision"] = (
            focal_mixed_plan.FROZEN_LISJONG_REVISION
        )
        frozen["native"]["wheel_sha256"] = focal_mixed_plan.FROZEN_WHEEL_SHA256
        focal_mixed_plan.require_frozen_environment(frozen)

    def test_live_gate_requires_head_environment_and_the_allocation(self):
        plan = fx.plan_document(self.tmp)
        fx.gate(plan, ledger=fx.ledger())
        with self.assertRaisesRegex(EngineAabbRecordError, "HEAD"):
            focal_mixed_plan.require_live_target(
                plan, ledger=fx.ledger(), environment=fx.ENVIRONMENT, head="f" * 40
            )
        changed = json.loads(json.dumps(fx.ENVIRONMENT))
        changed["python"]["version"] = "3.14.0"
        with self.assertRaisesRegex(EngineAabbRecordError, "environment"):
            focal_mixed_plan.require_live_target(
                plan,
                ledger=fx.ledger(),
                environment=changed,
                head=fx.ARENA_REVISION,
            )
        unrelated = fx.allocation((1, 2, 3), owner="lisbun/lisjong-arena#1")
        with self.assertRaises(EngineAabbRecordError):
            fx.gate(plan, ledger=fx.ledger(unrelated))


# ---------------------------------------------------------------------------
# 実行・verify・report
# ---------------------------------------------------------------------------


class RunTest(_Case):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.plan_path = self.save(fx.plan_document(self.tmp))

    def save(self, plan, name="plan.json"):
        self.plan = plan
        self.output = Path(plan["artifact_destinations"]["output"])
        self.diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
        return focal_mixed_plan.save_plan(plan, self.tmp / name)

    def run_comparison(self, play=fx.fake_focal, **kwargs):
        return focal_mixed_compare.run_comparison(
            self.plan_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=fx.gate,
            **kwargs,
        )

    def test_complete_run_matches_the_hand_values_and_verifies(self):
        summary = self.run_comparison()
        self.assertEqual(
            summary["status"], "COMPARISON COMPLETE; DEVELOPMENT, NOT A FORMAL RESULT"
        )
        self.assertEqual(summary["hanchan_count"], 24)
        self.assertEqual(summary["event_fact_status"], {"status": "VALID"})
        blocks = summary["blocks"]
        self.assertEqual(
            [(b["seed"], b["difference_internal"], b["d"]) for b in blocks],
            [(fx.SEEDS[0], -960, -24.0), (fx.SEEDS[1], 460, 11.5), (fx.SEEDS[2], 790, 19.75)],
        )  # fmt: skip
        self.assertEqual(blocks[0]["f_a_internal"], [-140, 50, -150, -720])
        self.assertEqual(blocks[0]["f_b_internal"], list(fx.Y_POINTS))
        statistic = summary["statistic"]
        self.assertAlmostEqual(statistic["mean_d"], (-24.0 + 11.5 + 19.75) / 3)
        self.assertTrue(statistic["interval_crosses_zero"])
        loo = summary["leave_one_seed_out"]
        self.assertEqual(loo["minimum_seed"], fx.SEEDS[2])
        self.assertEqual(loo["minimum_mean_d"], (-960 + 460) / 2 / 40)
        self.assertEqual(
            loo["without_largest_positive_block"],
            {"mean_d_without": -6.25, "seed": fx.SEEDS[2]},
        )
        progression = summary["progression"]
        self.assertEqual(progression["decision"], "NOT-RECONFIRMED-DEVELOPMENT")
        failed = [
            name for name, check in progression["checks"].items() if not check["pass"]
        ]
        self.assertEqual(failed, ["leave_one_seed_out_minimum_mean_d_pt"])  # fmt: skip
        checks = progression["checks"]
        self.assertEqual(checks["base_points_mean_difference_pt"]["value"], 0.75)
        self.assertAlmostEqual(
            checks["deal_in_rate_mean_difference"]["value"],
            (2 * DEAL_IN_SEED_1_2 - 0.25) / 3,
        )

        secondary = summary["secondary_metrics"]
        self.assertEqual(
            [row["difference"]["base_points"] for row in secondary["point_decomposition"]],
            [-260, 60, 290],
        )  # fmt: skip
        deal_in = secondary["kyoku_metrics"]["paired"]["deal_in_rate"]["per_seed"]
        self.assertEqual(
            [(row["A"]["numerator"], row["A"]["denominator"]) for row in deal_in],
            [(8, 34), (8, 34), (0, 34)],
        )
        self.assertEqual(
            [(row["B"]["numerator"], row["B"]["denominator"]) for row in deal_in],
            [(8, 32)] * 3,
        )
        # focal席だけ: 1 seedあたり条件ごとに4 seat-result
        ranks = secondary["rank_distribution"]
        self.assertEqual(sum(ranks["A"].values()), 12)
        self.assertEqual(sum(ranks["B"].values()), 12)

        activation = summary["activation"]
        self.assertEqual(activation["changes"], 12)
        self.assertEqual(activation["seeds_with_change"], 3)
        seconds = summary["decision_seconds"]
        self.assertEqual(seconds["focal-A"]["count"], 3 * sum(fx.COUNTS))
        self.assertEqual(seconds["focal-B"]["count"], 3 * sum(fx.COUNTS))
        self.assertEqual(seconds["O1"]["count"], 2 * 3 * sum(fx.COUNTS))
        self.assertNotIn("CANDIDATE SUPERIOR", json.dumps(summary))

        verified = focal_mixed_compare.verify_comparison(self.plan_path)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertEqual(verified["decision"], "NOT-RECONFIRMED-DEVELOPMENT")
        paths = [item["path"] for item in verified["files"]]
        self.assertIn("raw/manifest.json", paths)
        self.assertIn("raw/event-facts.jsonl", paths)
        self.assertIn("policy-analysis.json", paths)

        report = focal_mixed_compare.write_report(self.plan_path)
        self.assertIsNone(report["projection"])
        self.assertEqual(report["memory"]["worker_peak_rss_kib_max"], 4096)
        self.assertEqual(report["timing"]["seed_block_wall_seconds"]["count"], 3)
        self.assertEqual(
            focal_mixed_compare.verify_comparison(self.plan_path)["report_digest"],
            report["report_digest"],
        )
        with self.assertRaises(FileExistsError):
            focal_mixed_compare.write_report(self.plan_path)

    def test_verify_in_a_separate_process(self):
        summary = self.run_comparison()
        # 別processでもCLIのfocal-verifyを通す（Kの差し替えだけをそのprocessで行う）
        code = (
            "import sys\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb import focal_mixed_plan\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb.__main__ import main\n"
            "focal_mixed_plan.K = 3\n"
            "raise SystemExit(main(['focal-verify', '--plan', sys.argv[1]]))\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(self.plan_path)], text=True
        )
        verified = json.loads(output)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertEqual(verified["decision"], "NOT-RECONFIRMED-DEVELOPMENT")

    def test_all_conditions_give_reconfirmed(self):
        summary = self.run_comparison(fx.fake_focal_reconfirmed)
        self.assertEqual(
            [b["difference_internal"] for b in summary["blocks"]], [0, 460, 790]
        )
        progression = summary["progression"]
        self.assertEqual(progression["decision"], "RECONFIRMED-DEVELOPMENT")
        self.assertEqual(summary["leave_one_seed_out"]["minimum_mean_d"], 460 / 2 / 40)

    def test_rehearsal_runs_the_same_path_without_a_decision(self):
        self.plan_path = self.save(
            fx.plan_document(self.tmp, run_kind="rehearsal"), "rehearsal.json"
        )
        summary = self.run_comparison()
        self.assertIsNone(summary["progression"])
        self.assertEqual(summary["hanchan_count"], 8)
        self.assertIsNone(summary["leave_one_seed_out"])
        self.assertIsNone(summary["statistic"]["sd"])
        self.assertEqual(
            focal_mixed_compare.verify_comparison(self.plan_path)["status"],
            "REHEARSAL COMPLETE; NO PROGRESSION DECISION",
        )
        report = focal_mixed_compare.write_report(self.plan_path)
        projection = report["projection"]
        self.assertEqual(projection["compare_hanchans"], 256)
        walls = report["timing"]["hanchan_wall_seconds"]["total"]
        self.assertAlmostEqual(projection["projected_schedule_seconds"], walls * 32)

    def test_failures_stop_without_output(self):
        cases = [
            (fx.fake_focal_policy_error, "synthetic Policy failure"),
            (fx.fake_focal_bad_analysis, "Policy call count"),
            (fx.fake_focal_missing_evidence, ""),
            (fx.fake_focal_swapped_seats, "seat roles do not match the rotation"),
        ]
        for play, message in cases:
            with self.subTest(play=play.__name__):
                shutil.rmtree(self.diagnostics, ignore_errors=True)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    self.run_comparison(play)
                self.assertFalse(self.output.exists())
                stops = list(self.diagnostics.glob("stop-*.json"))
                self.assertEqual(len(stops), 1)
                stop = json.loads(stops[0].read_text("utf-8"))
                self.assertEqual(stop["status"], "STOP-INCOMPLETE")

    def test_rss_limit_stops_the_run(self):
        plan = fx.plan_document(
            self.tmp, resource_limits={**fx.LIMITS, "process_tree_rss_bytes": 1024}
        )
        self.plan_path = self.save(plan, "small.json")
        with self.assertRaisesRegex(EngineAabbRecordError, "process tree limit"):
            self.run_comparison()
        self.assertFalse(self.output.exists())

    def test_wall_limit_uses_the_longer_of_monotonic_and_realtime(self):
        ticks = iter(range(0, 10**6, 1))
        with self.assertRaisesRegex(EngineAabbRecordError, "wall limit"):
            self.run_comparison(
                clock=lambda: 0.0, realtime=lambda: float(next(ticks)) * 10**4
            )

    def test_tampering_is_rejected(self):
        self.run_comparison()

        def rewrite(path, change):
            document = json.loads(path.read_text("utf-8"))
            change(document)
            path.write_text(canonical_json_text(document), encoding="utf-8")

        analysis = self.output / "policy-analysis.json"
        original = analysis.read_bytes()
        rewrite(
            analysis,
            lambda d: d["hanchans"][0]["seats"][0]["switches"][0].__setitem__(
                "kind", "red-only-change"
            ),
        )
        with self.assertRaises(EngineAabbRecordError):
            focal_mixed_compare.verify_comparison(self.plan_path)
        analysis.write_bytes(original)
        # 判断時間だけを変えると、summaryが拘束するdigestで検出する
        rewrite(
            analysis,
            lambda d: d["hanchans"][0]["seats"][1]["decision_seconds"].__setitem__(
                0, 99.0
            ),
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "re-derivation"):
            focal_mixed_compare.verify_comparison(self.plan_path)
        analysis.write_bytes(original)
        focal_mixed_compare.verify_comparison(self.plan_path)

        raw = self.output / "raw" / "kyoku-records.jsonl"
        data = raw.read_bytes()
        raw.write_bytes(data.replace(b'"final_points":820', b'"final_points":821', 1))
        with self.assertRaises(EngineAabbRecordError):
            focal_mixed_compare.verify_comparison(self.plan_path)
        raw.write_bytes(data)

        timings = self.output / "timings.json"
        saved = timings.read_bytes()
        rewrite(
            timings,
            lambda d: d["hanchans"][0].__setitem__("worker_peak_rss_kib", 5 * 1024**2),
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "plan limit"):
            focal_mixed_compare.verify_comparison(self.plan_path)
        timings.write_bytes(saved)
        rewrite(timings, lambda d: d.__setitem__("realtime_elapsed_seconds", 10**6))
        with self.assertRaisesRegex(EngineAabbRecordError, "wall limit"):
            focal_mixed_compare.verify_comparison(self.plan_path)
        timings.write_bytes(saved)

        summary = self.output / "summary.json"
        rewrite(summary, lambda d: d["progression"].__setitem__("decision", "RECONFIRMED-DEVELOPMENT"))  # fmt: skip
        with self.assertRaisesRegex(EngineAabbRecordError, "re-derivation"):
            focal_mixed_compare.verify_comparison(self.plan_path)

    def test_records_are_not_readable_as_aabb_records(self):
        self.run_comparison()
        raw = self.output / "raw"
        with self.assertRaisesRegex(KyokuRecordError, "schema"):
            read_kyoku_records(raw, event_facts_required=True)
        records = read_kyoku_records(
            raw, event_facts_required=True, layout=focal_mixed_plan.LAYOUT
        )
        row = json.loads(json.dumps(records.hanchans[0]))
        validate_hanchan_row(row, focal_mixed_plan.LAYOUT)
        for name, edit in {
            "condition": lambda r: r.__setitem__("condition", "C"),
            "rotation": lambda r: r.__setitem__("rotation", 4),
            "roles": lambda r: r["seats"].reverse(),
            "placement": lambda r: r.__setitem__("placement", 0),
        }.items():
            with self.subTest(edit=name):
                changed = json.loads(json.dumps(row))
                edit(changed)
                with self.assertRaises(KyokuRecordError):
                    validate_hanchan_row(changed, focal_mixed_plan.LAYOUT)
        with self.assertRaises(KyokuRecordError):
            validate_hanchan_row(row)

        # 副指標のseat groupは明示した席だけを数え、不正なgroupを拒否する
        def summarize(groups):
            return summarize_seat_groups(
                records, groups, layout=focal_mixed_plan.LAYOUT
            )

        valid = [(0, 0, "A"), (1, 0, "B")]
        self.assertEqual(summarize([(fx.SEEDS[0], valid)])["blocks"], 1)
        invalid = {
            "duplicate seat": [(0, 0, "A"), (0, 0, "B")],
            "other seed": [(8, 0, "A")],
            "role": [(0, 0, "O1")],
            "empty": [],
        }
        for name, members in invalid.items():
            with self.subTest(group=name):
                with self.assertRaises(SecondaryMetricError):
                    summarize([(fx.SEEDS[0], members)])
        with self.assertRaises(SecondaryMetricError):
            summarize([(fx.SEEDS[0], valid)] * 2)

    def test_completed_run_is_never_rerun(self):
        self.run_comparison()
        with self.assertRaisesRegex(EngineAabbRecordError, "write-once"):
            self.run_comparison()


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


class _Stop(Exception):
    def __init__(self, policies):
        super().__init__("stop")
        self.policies = policies


class WorkerTest(unittest.TestCase):
    def test_fresh_policy_per_seat_and_only_focal_a_is_analyzed(self):
        documents = {
            role: item.to_document()
            for role, item in focal_mixed_plan.participants(fx.LISJONG_REVISION).items()
        }
        created = []

        def factory_for(participant):
            def factory(**_):
                created.append(participant.policy_identity)
                return object()

            return factory

        def fake_run(policies, **kwargs):
            raise _Stop(policies)

        seen = {}
        for condition in ("A", "B"):
            task = focal_mixed_plan.FocalTask(
                seed=1,
                rotation=2,
                condition=condition,
                participants=documents,
                wheel_path="w",
            )
            with (
                mock.patch.object(focal_mixed_worker, "resolve_factory", factory_for),
                mock.patch.object(focal_mixed_worker, "run_policy_hanchan", fake_run),
            ):
                with self.assertRaises(_Stop) as stopped:
                    focal_mixed_worker.focal_task(task)
            seen[condition] = [
                proxy._policy for proxy in stopped.exception.policies.values()
            ]
        # rotation 2: seat 0 = O2, 1 = O3, 2 = focal, 3 = O1
        self.assertEqual(created[:4], [O2, O3, A, B])
        self.assertEqual(created[4:], [O2, O3, B, B])
        self.assertIsInstance(seen["A"][2], _AnalyzedCandidate)
        self.assertIsInstance(seen["B"][2], _TimedIncumbent)
        for proxies in seen.values():
            self.assertTrue(
                all(isinstance(proxy, _TimedIncumbent) for proxy in proxies[:2])
            )
            self.assertIsInstance(proxies[3], _TimedIncumbent)
            self.assertEqual(len({id(proxy._policy) for proxy in proxies}), 4)
        self.assertTrue(
            {id(proxy._policy) for proxy in seen["A"]}.isdisjoint(
                id(proxy._policy) for proxy in seen["B"]
            )
        )


if __name__ == "__main__":
    unittest.main()
