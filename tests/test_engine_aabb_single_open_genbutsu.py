"""#524 単独副露者genbutsu候補のshadow変更頻度gate（DEVELOPMENT）のplan・run・verify（合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘と合成したPolicy観測を
返す ``fake_*`` を使う。判断の分類（worker側）はlisjong #343の実際のanalysis・action型で
確かめる。合成値であり、強さの結果でも実測の頻度でもない。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_paijia_tie_fixtures as paijia_fx
import _engine_aabb_single_open_genbutsu_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    paijia_tie_plan,
    single_open_genbutsu_activation,
    single_open_genbutsu_plan,
    single_open_genbutsu_worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import FORMAL_SPLIT

activation = single_open_genbutsu_activation
plan_module = single_open_genbutsu_plan
worker = single_open_genbutsu_worker


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


def _rewrite(path, change):
    document = json.loads(path.read_text("utf-8"))
    change(document)
    path.write_text(canonical_json_text(document), "utf-8", newline="\n")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(_Case):
    def test_plan_binds_pair_population_criteria_and_limits(self):
        plan = fx.plan_document(self.tmp)
        self.assertEqual(plan["evaluation_kind"], "DEVELOPMENT")
        self.assertIs(plan["not_a_formal_result"], True)
        self.assertEqual(plan["owner_issue"], "lisbun/lisjong-arena#524")
        self.assertEqual(plan["stage"], "activation")
        self.assertEqual(
            (plan["participants"]["A"]["policy_identity"], plan["participants"]["B"]["policy_identity"]),
            ("placement-aware-speed-call-single-open-genbutsu-v1", "placement-aware-speed-call"),
        )  # fmt: skip
        for role in ("A", "B"):
            participant = plan["participants"][role]
            self.assertEqual(participant["behavior_configuration"], {})
            self.assertIsNone(participant["artifact"])
            self.assertEqual(participant["lisjong_revision"], fx.LISJONG_REVISION)
        self.assertEqual(
            plan["participants"]["A"]["factory_binding"],
            "lisjong_arena.policy_catalog:"
            "create_placement_aware_speed_call_single_open_genbutsu",
        )
        population = plan["population"]
        self.assertEqual(population["split"], "DEVELOPMENT")
        self.assertEqual(population["ordered_seeds"], list(fx.SEEDS))
        self.assertEqual(
            population["population"],
            "heuristic-candidate-engine-aabb-single-open-genbutsu-activation-524",
        )
        criteria = plan["criteria"]
        self.assertEqual(criteria["minimum_changes"], 32)
        self.assertEqual(criteria["minimum_seeds_with_change"], 4)
        self.assertEqual(criteria["seat_hanchan_denominator"], 32)
        self.assertEqual(
            criteria["order"], ["STOP-INVALID", "STOP-LOW-ACTIVATION", "PROCEED"]
        )
        self.assertIn("trace-action-mismatch", criteria["invalid_outcomes"])
        self.assertEqual(criteria["excluded_from_changes"], ["tsumogiri-only"])
        self.assertEqual(plan["resource_limits"], fx.LIMITS)
        self.assertEqual(plan["execution"]["workers"], 1)
        self.assertEqual(plan["execution"]["backend"], "rust")
        self.assertEqual(plan["execution"]["timeout_seconds"], 600)
        path = plan_module.save_plan(plan, self.tmp / "plan.json")
        self.assertEqual(plan_module.load_plan(path), plan)
        with self.assertRaises(FileExistsError):
            plan_module.save_plan(plan, path)

    def test_rehearsal_uses_fixed_development_seeds_without_allocation(self):
        plan = fx.plan_document(self.tmp, run_kind="rehearsal")
        self.assertEqual(plan["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertNotIn("allocation_binding", plan["population"])
        with self.assertRaisesRegex(EngineAabbRecordError, "development seeds"):
            fx.plan_document(
                self.tmp, run_kind="rehearsal", ordered_seeds=(472_005, 472_007)
            )

    def test_plan_rejects_inputs_that_change_its_meaning(self):
        formal = fx.allocation(split=FORMAL_SPLIT)
        other_owner = fx.allocation(owner="lisbun/lisjong-arena#520")
        paijia_population = fx.allocation(
            population=paijia_tie_plan.POPULATIONS["activation"]
        )
        overlapping = seed_registry.build_allocation_record(
            owner_issue="lisbun/lisjong-arena#1",
            protocol="other",
            seed_domain="riichienv-4p-red-half-hanchan-v1",
            purpose="test",
            population="other",
            split="FORMAL-EVAL",
            seeds=(fx.SEEDS[3],),
            arena_revision="e" * 40,
            protocol_revision="test",
            provenance_reference="test",
            allocation_timestamp="2026-10-24T00:00:00Z",
        )
        python = {**fx.ENVIRONMENT, "shanten_backend": {"backend": "python", "lisjong_revision": fx.LISJONG_REVISION}}  # fmt: skip
        other_native = {
            **fx.ENVIRONMENT,
            "native": {**fx.ENVIRONMENT["native"], "source_revision": "f" * 40},
        }
        cases = [
            ({"record": formal, "ledger": fx.ledger(formal)}, "split"),
            ({"record": other_owner, "ledger": fx.ledger(other_owner)}, "owner_issue"),
            ({"record": paijia_population, "ledger": fx.ledger(paijia_population)}, "population"),
            ({"ledger": fx.ledger(fx.RECORD, overlapping)}, "collide"),
            ({"ordered_seeds": fx.SEEDS[:7]}, "exactly 8"),
            ({"timeout_seconds": 0}, "positive int"),
            ({"timeout_seconds": True}, "positive int"),
            ({"resource_limits": {**fx.LIMITS, "wall_seconds": 0}}, "positive int"),
            ({"resource_limits": {**fx.LIMITS, "enforcement": " "}}, "enforcement"),
            ({"wheel_path": "relative.whl"}, "absolute"),
            ({"environment": python}, "rust"),
            ({"environment": other_native}, "SOURCE_REVISION"),
            ({"arena_revision": "f" * 40}, "Arena revision"),
            ({"destinations": {"output": str(self.tmp / "x")}}, "destinations"),
            ({"run_kind": "formal"}, "run_kind"),
        ]  # fmt: skip
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    fx.plan_document(self.tmp, **overrides)

    def test_saved_plan_tampering_is_rejected(self):
        plan = fx.plan_document(self.tmp)
        for key, value in (
            ("criteria", {**plan["criteria"], "minimum_changes": 16}),
            ("criteria", {**plan["criteria"], "minimum_seeds_with_change": 3}),
            ("not_a_formal_result", False),
            ("evaluation_kind", "FORMAL-EVAL"),
            ("owner_issue", "lisbun/lisjong-arena#520"),
            ("run_kind", "rehearsal"),
            ("stage", "screen"),
            ("plan_digest", "0" * 64),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(EngineAabbRecordError):
                    plan_module.parse_plan({**plan, key: value})
        swapped = {
            **plan,
            "participants": {
                "A": plan["participants"]["B"],
                "B": plan["participants"]["A"],
            },
        }
        with self.assertRaisesRegex(EngineAabbRecordError, "fixed pair"):
            plan_module.parse_plan(swapped)

    def test_plans_of_520_and_524_do_not_accept_each_other(self):
        """#520のplan・allocationは変えず、#524と相互に受理しない。"""
        mine = fx.plan_document(self.tmp)
        with self.assertRaisesRegex(EngineAabbRecordError, "schema"):
            paijia_tie_plan.parse_plan(mine)
        theirs = paijia_fx.plan_document(self.tmp)
        with self.assertRaisesRegex(EngineAabbRecordError, "schema"):
            plan_module.parse_plan(theirs)
        self.assertEqual(
            theirs["participants"]["A"]["policy_identity"],
            "placement-aware-speed-call-suited-paijia-tie-v1",
        )
        document = fx.ledger(fx.RECORD, paijia_fx.ACTIVATION_RECORD)
        with self.assertRaisesRegex(EngineAabbRecordError, "allocation"):
            plan_module.require_stage_seeds(
                document,
                fx.binding(document, paijia_fx.ACTIVATION_RECORD),
                paijia_fx.ACTIVATION_SEEDS,
            )

    def test_live_gate_requires_head_environment_and_an_active_allocation(self):
        document = fx.ledger()
        plan = fx.plan_document(self.tmp, ledger=document)
        fx.gate(plan, ledger=document)
        retired = seed_registry.transition_allocation(
            document, fx.RECORD["allocation_identity"], state="RETIRED"
        )
        with self.assertRaisesRegex(EngineAabbRecordError, "not active"):
            fx.gate(plan, ledger=retired)
        with self.assertRaisesRegex(EngineAabbRecordError, "HEAD"):
            plan_module.require_live_target(
                plan, ledger=document, environment=fx.ENVIRONMENT, head="f" * 40
            )
        other_wheel = {
            **fx.ENVIRONMENT,
            "native": {**fx.ENVIRONMENT["native"], "wheel_sha256": "0" * 64},
        }
        with self.assertRaisesRegex(EngineAabbRecordError, "environment"):
            plan_module.require_live_target(
                plan, ledger=document, environment=other_wheel, head=fx.ARENA_REVISION
            )
        fx.gate(fx.plan_document(self.tmp, run_kind="rehearsal"), ledger=None)


# ---------------------------------------------------------------------------
# run / verify
# ---------------------------------------------------------------------------


class ActivationRunTest(_Case):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.plan_path = self.save(fx.plan_document(self.tmp))

    def save(self, plan, name="plan.json"):
        self.plan = plan
        self.output = Path(plan["artifact_destinations"]["output"])
        self.diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
        return plan_module.save_plan(plan, self.tmp / name)

    def run_activation(self, play=fx.fake_proceed, **kwargs):
        return activation.run_activation(
            self.plan_path,
            ledger_path=self.ledger_path,
            play=play,
            live_gate=fx.gate,
            **kwargs,
        )

    def test_threshold_is_met_exactly_and_verifies(self):
        summary = self.run_activation()
        self.assertEqual(summary["gate"]["decision"], "PROCEED")
        counts = summary["activation"]
        self.assertEqual(counts["changes"], 32)
        self.assertEqual(counts["tile_type_changes"], 32)
        self.assertEqual(counts["red_only_changes"], 0)
        self.assertEqual(counts["seat_hanchans"], 32)
        self.assertEqual(counts["changes_per_seat_hanchan"], 1.0)
        self.assertEqual(counts["seeds_with_change"], 8)
        self.assertEqual(counts["decisions"], 8 * 4 * 3)
        self.assertEqual(counts["switched_decisions"], 32)
        self.assertEqual(counts["alternative_decisions"], 64)
        self.assertEqual(counts["single_open_decisions"], 64)
        self.assertEqual(counts["by_stage"]["NO_OPEN_OPPONENT"], 32)
        self.assertEqual(sum(counts["invalid"].values()), 0)
        self.assertEqual(
            counts["per_seed"][0],
            {
                "alternative_decisions_by_seat": [2, 2, 2, 2],
                "changes": 4,
                "changes_by_seat": [1, 1, 1, 1],
                "seed": fx.SEEDS[0],
                "single_open_decisions_by_seat": [2, 2, 2, 2],
            },
        )
        seconds = summary["decision_seconds"]
        self.assertEqual(
            {name: seconds[name]["count"] for name in seconds},
            {"A_shadow_normal": 96, "A_shadow_trace": 96, "B": 96},
        )
        for forbidden in ("mean_d", "statistic", "classification", "blocks"):
            self.assertNotIn(forbidden, summary)
        self.assertIs(summary["not_a_strength_result"], True)
        self.assertNotIn("within_budget", summary["screen_projection"])
        self.assertIs(summary["screen_projection"]["descriptive_only"], True)
        self.assertEqual(summary["screen_projection"]["screen_hanchans"], 192)
        self.assertEqual(summary["memory"]["worker_peak_rss_kib_max"], 2048)

        verified = activation.verify_activation(self.plan_path)
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])
        self.assertEqual(verified["decision"], "PROCEED")
        self.assertEqual(
            sorted(item["path"] for item in verified["files"]),
            ["hanchans.json", "shadow.json", "summary.json", "timings.json"],
        )
        self.assertEqual(
            verified["output_bytes"], sum(item["bytes"] for item in verified["files"])
        )
        self.assertIn("realtime_minus_monotonic_seconds", verified["clocks"])

    def test_verify_in_a_separate_process(self):
        summary = self.run_activation()
        code = (
            "import sys\n"
            "from lisjong_arena.heuristic_candidate_engine_aabb import "
            "single_open_genbutsu_activation as m\n"
            "print(m.verify_activation(sys.argv[1])['summary_digest'])\n"
        )
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(self.plan_path)], text=True
        )
        self.assertEqual(output.strip(), summary["summary_digest"])

    def test_gate_boundaries_in_fixed_order(self):
        cases = [
            (fx.fake_low, "STOP-LOW-ACTIVATION", 31, 8),
            (fx.fake_four_seeds, "PROCEED", 32, 4),
            (fx.fake_three_seeds, "STOP-LOW-ACTIVATION", 36, 3),
            (fx.fake_invariant, "STOP-INVALID", 32, 8),
            (fx.fake_trace_mismatch, "STOP-INVALID", 32, 8),
        ]
        for index, (play, decision, changes, seeds) in enumerate(cases):
            with self.subTest(play=play.__name__):
                plan = fx.plan_document(self.tmp, name=f"case{index}")
                self.plan_path = self.save(plan, f"plan-{index}.json")
                summary = self.run_activation(play)
                self.assertEqual(summary["gate"]["decision"], decision)
                self.assertEqual(summary["activation"]["changes"], changes)
                self.assertEqual(summary["activation"]["seeds_with_change"], seeds)
                self.assertEqual(
                    activation.verify_activation(self.plan_path)["decision"], decision
                )
                invalid = summary["activation"]["invalid"]
                if play is fx.fake_invariant:
                    self.assertEqual(invalid["analysis-invariant-violation"], 1)
                if play is fx.fake_trace_mismatch:
                    self.assertEqual(invalid["trace-action-mismatch"], 1)

    def test_rehearsal_has_no_gate(self):
        self.plan_path = self.save(
            fx.plan_document(self.tmp, run_kind="rehearsal"), "rehearsal.json"
        )
        summary = self.run_activation()
        self.assertIsNone(summary["gate"])
        self.assertEqual(summary["status"], "REHEARSAL COMPLETE; NO GATE DECISION")
        self.assertEqual(summary["hanchan_count"], 2)
        self.assertIsNone(activation.verify_activation(self.plan_path)["decision"])

    def test_failures_stop_without_output(self):
        cases = [
            (fx.fake_count_mismatch, "Policy call count"),
            (fx.fake_no_analysis, "no Policy observation"),
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
                self.assertEqual(stop["schema"], plan_module.STOP_SCHEMA)
                self.assertTrue(stop["not_a_result"])

    def test_wall_limit_stops_without_output(self):
        ticks = iter(range(10**9))
        with self.assertRaisesRegex(EngineAabbRecordError, "wall limit"):
            self.run_activation(
                clock=lambda: 0.0, realtime=lambda: next(ticks) * 3600.0
            )
        self.assertFalse(self.output.exists())

    def test_missing_duplicated_and_tampered_records_are_rejected(self):
        self.run_activation()
        shadow_path = self.output / "shadow.json"
        original = shadow_path.read_text("utf-8")

        def seat0(change):
            return lambda d: change(d["hanchans"][0]["seats"][0]["decisions"])

        def set_value(rows):
            rows[0]["detail"]["analysis"]["parent_evaluation"][
                "current_ukeire_count"
            ] = 9

        def duplicate(document):
            document["hanchans"][1] = document["hanchans"][0]

        def drop_hanchan(document):
            document["hanchans"].pop()

        def no_detail(rows):
            del rows[0]["detail"]

        def outside_switched(rows):
            rows[0]["stage"] = "NO_QUALIFYING_ALTERNATIVE"

        def not_qualifying(rows):
            rows[0]["detail"]["analysis"]["qualifying_actions"] = []

        def plain_differs(rows):
            rows[0]["detail"]["plain_candidate_action"] = fx.PARENT

        def kept_to_change(rows):
            rows[1]["outcome"] = "tile-type-change"

        cases = [
            (duplicate, "planned schedule"),
            (drop_hanchan, "planned schedule"),
            (seat0(lambda rows: rows.pop()), "Policy call count"),
            (seat0(no_detail), "detail is missing"),
            (seat0(outside_switched), "analysis differs|SWITCHED"),
            (seat0(not_qualifying), "qualifying SWITCHED"),
            (seat0(plain_differs), "normal and trace"),
            (seat0(kept_to_change), "analysis differs|SWITCHED"),
            # 形は正しいまま値だけ変えた詳細は、summaryが拘束するdigestで検出する
            (seat0(set_value), "summary differs"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                _rewrite(shadow_path, change)
                with self.assertRaisesRegex(EngineAabbRecordError, message):
                    activation.verify_activation(self.plan_path)
                shadow_path.write_text(original, "utf-8", newline="\n")
        activation.verify_activation(self.plan_path)
        hanchans = self.output / "hanchans.json"
        _rewrite(hanchans, lambda d: d["hanchans"][0].update(end_reason="other"))
        with self.assertRaisesRegex(EngineAabbRecordError, "summary differs"):
            activation.verify_activation(self.plan_path)

    def test_completed_run_is_never_rerun(self):
        self.run_activation()
        with self.assertRaisesRegex(EngineAabbRecordError, "write-once"):
            self.run_activation()


# ---------------------------------------------------------------------------
# worker側の分類（lisjong #343の実際の型）
# ---------------------------------------------------------------------------


def _discard(category, rank, *, red=False, tsumogiri=False):
    from lisjong.policy_contract import (
        DiscardAction,
        Seat,
        Tile,
        TileCategory,
        TileType,
    )

    tile = Tile(TileType(TileCategory[category], rank), is_red=red)
    return DiscardAction(actor=Seat(0), tile=tile, tsumogiri=tsumogiri)


def _evaluation(action, shanten=2, ukeire=10, retained=3, yaku=1):
    from lisjong.policies.single_open_genbutsu_placement_aware_speed_call import (
        GenbutsuCandidateEvaluation,
    )

    return GenbutsuCandidateEvaluation(action, shanten, ukeire, retained, yaku)


def _analysis(stage, parent, selected, **fields):
    from lisjong.policies.single_open_genbutsu_placement_aware_speed_call import (
        SingleOpenGenbutsuAnalysis,
        SingleOpenGenbutsuStage,
    )

    values = {
        "open_opponent_seat": None,
        "genbutsu_tile_types": (),
        "parent_in_genbutsu": None,
        "parent_evaluation": None,
        "alternative_evaluations": (),
        "qualifying_actions": (),
        **fields,
    }
    return SingleOpenGenbutsuAnalysis(
        stage=SingleOpenGenbutsuStage[stage],
        parent_action=parent,
        selected_action=selected,
        action_changed=parent != selected,
        **values,
    )


class ClassifyShadowTest(unittest.TestCase):
    def setUp(self):
        self.m5 = _discard("MANZU", 5)
        self.p9 = _discard("PINZU", 9)
        self.s1 = _discard("SOUZU", 1)
        self.genbutsu = (self.p9.tile.tile_type, self.s1.tile.tile_type)
        self.classify = worker.classify_shadow

    def evaluated(self, stage, selected, alternatives, qualifying, **extra):
        fields = {
            "open_opponent_seat": 2,
            "genbutsu_tile_types": self.genbutsu,
            "parent_in_genbutsu": False,
            "parent_evaluation": _evaluation(self.m5),
            "alternative_evaluations": alternatives,
            "qualifying_actions": qualifying,
            **extra,
        }
        return _analysis(stage, self.m5, selected, **fields)

    def switched(self):
        return self.evaluated(
            "SWITCHED",
            self.p9,
            (_evaluation(self.p9), _evaluation(self.s1, ukeire=4)),
            (self.p9,),
        )

    def test_switched_change_and_kept_parent(self):
        self.assertEqual(
            self.classify(self.m5, self.p9, self.switched()),
            ("tile-type-change", "SWITCHED", []),
        )
        kept = self.evaluated(
            "NO_QUALIFYING_ALTERNATIVE",
            self.m5,
            (_evaluation(self.p9, shanten=3),),
            (),
        )
        self.assertEqual(
            self.classify(self.m5, self.m5, kept),
            ("same", "NO_QUALIFYING_ALTERNATIVE", []),
        )
        gate = _analysis("NO_OPEN_OPPONENT", self.m5, self.m5)
        self.assertEqual(
            self.classify(self.m5, self.m5, gate), ("same", "NO_OPEN_OPPONENT", [])
        )
        from lisjong.policy_contract import PassAction, Seat

        passing = PassAction(actor=Seat(0))
        self.assertEqual(
            self.classify(passing, passing, None), ("same", "NO_ANALYSIS", [])
        )

    def test_invalid_outcomes(self):
        # analysisのparentが実際のBと違う
        outcome, _, violations = self.classify(self.s1, self.p9, self.switched())
        self.assertEqual(outcome, "analysis-invariant-violation")
        self.assertIn("parent_action", violations[-1])
        # 返した行動がanalysis.selected_actionと違う
        self.assertEqual(
            self.classify(self.m5, self.s1, self.switched())[0],
            "analysis-invariant-violation",
        )
        from lisjong.policy_contract import PassAction, Seat

        self.assertEqual(
            self.classify(self.m5, PassAction(actor=Seat(0)), None)[0],
            "out-of-scope-difference",
        )
        self.assertEqual(
            self.classify(self.m5, self.m5, None)[0], "analysis-invariant-violation"
        )
        self.assertEqual(
            self.classify(self.m5, None, None)[0], "candidate-illegal-action"
        )

    def test_analysis_value_relations(self):
        violations = worker.analysis_violations
        m5 = self.m5
        cases = [
            # 同向聴でない代替をqualifyingに入れた
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9, shanten=3),), (self.p9,)), "same-shanten"),
            # 受入・retained・yakuのどれかが下がる
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9, ukeire=9),), (self.p9,)), "same-shanten"),
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9, retained=2),), (self.p9,)), "same-shanten"),
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9, yaku=0),), (self.p9,)), "same-shanten"),
            # 条件を満たす代替をqualifyingから落とした
            (self.evaluated("NO_QUALIFYING_ALTERNATIVE", m5, (_evaluation(self.p9),), ()), "same-shanten"),
            # 現物でない代替
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9),), (self.p9,), genbutsu_tile_types=(self.s1.tile.tile_type,)), "not in the genbutsu"),
            # 聴牌のparentを比較した
            (_analysis("SWITCHED", m5, self.p9, open_opponent_seat=2, genbutsu_tile_types=self.genbutsu, parent_in_genbutsu=False, parent_evaluation=_evaluation(m5, shanten=0), alternative_evaluations=(_evaluation(self.p9, shanten=0),), qualifying_actions=(self.p9,)), "tenpai"),
            # parentがすでに現物なのに比較した
            (self.evaluated("SWITCHED", self.p9, (_evaluation(self.p9),), (self.p9,), genbutsu_tile_types=(*self.genbutsu, m5.tile.tile_type)), "parent_in_genbutsu"),
            # 副露者1人より前のgateで副露者を記録した
            (_analysis("NO_OPEN_OPPONENT", m5, m5, open_opponent_seat=1), "open_opponent_seat"),
            # 比較値のないstageで比較値を記録した
            (_analysis("NO_GENBUTSU_ALTERNATIVE", m5, m5, open_opponent_seat=1, parent_in_genbutsu=False, parent_evaluation=_evaluation(m5)), "evaluations"),
        ]  # fmt: skip
        for analysis, message in cases:
            with self.subTest(message=message):
                found = violations(analysis, analysis.selected_action)
                self.assertTrue(any(message in item for item in found), found)
        self.assertEqual(violations(self.switched(), self.p9), [])


class _FakePolicy:
    def __init__(self, action, analysis=None, calls=None):
        self.action = action
        self.analysis = analysis
        self.calls = calls if calls is not None else []

    def choose_action(self, decision):
        self.calls.append(decision)
        return self.action


class ShadowProxyTest(unittest.TestCase):
    """Bだけを適用し、Aは判断ごと・実行ごとに新しく作って採点することを確かめる。"""

    def setUp(self):
        self.parent = _discard("MANZU", 5)
        self.genbutsu = _discard("PINZU", 9)
        self.analysis = _analysis(
            "SWITCHED",
            self.parent,
            self.genbutsu,
            open_opponent_seat=2,
            genbutsu_tile_types=(self.genbutsu.tile.tile_type,),
            parent_in_genbutsu=False,
            parent_evaluation=_evaluation(self.parent),
            alternative_evaluations=(_evaluation(self.genbutsu),),
            qualifying_actions=(self.genbutsu,),
        )

    def run_proxy(self, plain_actions):
        incumbent_calls, candidates = [], []
        plain = iter(plain_actions)

        def new_candidate():
            candidate = _FakePolicy(self.genbutsu, self.analysis)
            candidates.append(candidate)
            return candidate

        def decide(policy, decision):
            policy.calls.append(("trace", decision))
            return policy.action, policy.analysis

        def execute(policy, decision):
            if policy.calls is incumbent_calls:
                return policy.choose_action(decision)
            policy.calls.append(("normal", decision))
            return next(plain)

        rows = []
        proxy = worker._GenbutsuShadow(
            _FakePolicy(self.parent, calls=incumbent_calls), new_candidate, rows
        )
        with (
            mock.patch("lisjong.policy_contract.execute_policy", execute),
            mock.patch.object(worker, "_decide_with_analysis", decide),
        ):
            applied = [proxy.choose_action(f"decision-{i}") for i in (1, 2)]
        return applied, incumbent_calls, candidates, rows

    def test_only_the_incumbent_action_is_applied(self):
        applied, incumbent_calls, candidates, rows = self.run_proxy(
            [self.genbutsu, self.genbutsu]
        )
        self.assertEqual(applied, [self.parent, self.parent])
        self.assertEqual(incumbent_calls, ["decision-1", "decision-2"])
        # 判断ごとに、trace用と通常実行用の新しいAを1つずつ作る
        self.assertEqual(
            [c.calls for c in candidates],
            [
                [("trace", "decision-1")],
                [("normal", "decision-1")],
                [("trace", "decision-2")],
                [("normal", "decision-2")],
            ],
        )
        self.assertEqual(len({id(c) for c in candidates}), 4)
        self.assertEqual([row["outcome"] for row in rows], ["tile-type-change"] * 2)
        self.assertEqual(rows[0]["stage"], "SWITCHED")
        self.assertEqual(rows[0]["detail"]["violations"], [])
        self.assertEqual(
            rows[0]["detail"]["plain_candidate_action"],
            rows[0]["detail"]["candidate_action"],
        )
        for name in ("a_plain_seconds", "a_seconds", "b_seconds"):
            self.assertGreaterEqual(rows[0][name], 0)

    def test_normal_and_trace_actions_must_match(self):
        applied, _, _, rows = self.run_proxy([self.parent, self.genbutsu])
        self.assertEqual(applied, [self.parent, self.parent])
        self.assertEqual(
            [row["outcome"] for row in rows],
            ["trace-action-mismatch", "tile-type-change"],
        )
        self.assertNotEqual(
            rows[0]["detail"]["plain_candidate_action"],
            rows[0]["detail"]["candidate_action"],
        )


class StoredShapeTest(unittest.TestCase):
    """保存側の検査が、実際のlisjong値の正規化表現（canonical_engine_value）を受理する。"""

    def test_real_canonical_values_pass_the_stored_checks(self):
        from lisjong_arena.heuristic_candidate_engine_aabb.backend_check import (
            canonical_engine_value,
        )

        parent = _discard("MANZU", 5)
        genbutsu = _discard("PINZU", 9)
        switched = _analysis(
            "SWITCHED",
            parent,
            genbutsu,
            open_opponent_seat=2,
            genbutsu_tile_types=(genbutsu.tile.tile_type,),
            parent_in_genbutsu=False,
            parent_evaluation=_evaluation(parent),
            alternative_evaluations=(_evaluation(genbutsu),),
            qualifying_actions=(genbutsu,),
        )
        incumbent = canonical_engine_value(parent)
        candidate = canonical_engine_value(genbutsu)
        detail = {
            "analysis": canonical_engine_value(switched),
            "candidate_action": candidate,
            "incumbent_action": incumbent,
            "plain_candidate_action": candidate,
            "violations": [],
        }
        activation._require_detail("tile-type-change", "SWITCHED", detail, "test")
        # fixtureの合成値は実際の正規化表現と同じ形
        synthetic = fx.analysis("SWITCHED", fx.PARENT, fx.GENBUTSU)
        real = detail["analysis"]
        self.assertEqual(set(synthetic), set(real))
        self.assertEqual(
            set(synthetic["parent_evaluation"]), set(real["parent_evaluation"])
        )
        self.assertEqual(set(fx.discard()), set(candidate))
        self.assertEqual(synthetic["genbutsu_tile_types"], real["genbutsu_tile_types"])


if __name__ == "__main__":
    unittest.main()
