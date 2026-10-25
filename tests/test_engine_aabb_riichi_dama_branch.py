"""#540 riichi-dama-branch-v1（#539）の観測・照合・判定・plan・run・strict verify。

実engineの対局・Rust wheel・実Policyの半荘実行は使わない（engine呼び出しとworkerの
playを差し替える）。lisjongの特徴APIとD arm wrapperは実物を使う。
"""

import os
import random
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import _engine_aabb_riichi_dama_branch_fixtures as fx
from _engine_aabb_record_fixtures import SCRIPT_X, play
from lisjong.policies import PlacementAwareSpeedCallPolicy
from lisjong.policy_contract.action import DiscardAction, RiichiAction
from lisjong.policy_contract.seat import Seat
from lisjong_engine.round_completion import project_round_completion
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.heuristic_candidate_engine_aabb import riichi_dama_branch as core
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_dama_branch_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_dama_branch_worker as worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    canonical_digest,
    encode_action,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    SeatAssignment,
    project_row,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)

RIICHI = RiichiAction(actor=Seat.SEAT_0)
SEED = plan_module.SEEDS[0]


def _facts(seed=SEED):
    return [project_round_completion(item) for item in play(seed, SCRIPT_X).history]


def _kyoku_row(seed=SEED):
    match = play(seed, SCRIPT_X)
    return project_row(
        match,
        seed=seed,
        keys={"placement": 0},
        seats=(SeatAssignment("B", fx.CHAMPION),) * 4,
        selector_call_counts=(fx.DECISIONS,) * 4,
        policy_call_counts=(fx.DECISIONS,) * 4,
        layout=core.LAYOUT,
    )


# ---------------------------------------------------------------------------
# workerの観測（R）
# ---------------------------------------------------------------------------


class RObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = _facts()

    def _first_riichi(self, observer):
        other = fx.discard_decision(fx.KANCHAN, seat=Seat.SEAT_1)
        observer.observe(1, other, other.legal_actions[0])
        observer.observe(0, fx.K0_DECISION, RIICHI)
        declaration = fx.declaration_decision(fx.KANCHAN, opponent_riichi=True)
        observer.observe(0, declaration, fx.DECLARATION)
        return other

    def test_first_riichi_is_a_branch_with_its_declaration(self) -> None:
        observer = worker.RObserver()
        other = self._first_riichi(observer)
        observer.on_delivery((self.facts[0],))
        analysis = observer.finish(1)
        (record,) = analysis["branches"]
        self.assertEqual(record["decision_index"], 1)
        self.assertEqual(record["declaration"], encode_action(fx.DECLARATION))
        self.assertEqual(record["declaration_decision_index"], 2)
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["round"], fx.context_round(fx.K0_DECISION))
        self.assertEqual(
            record["prefix_sha256"],
            canonical_digest([[1, encode_action(other.legal_actions[0])]]),
        )
        self.assertEqual(
            record["post_sha256"],
            canonical_digest(
                [[0, encode_action(RIICHI)], [0, encode_action(fx.DECLARATION)]]
            ),
        )
        self.assertEqual(record["context_sha256"], canonical_digest(record["context"]))
        self.assertEqual(analysis["decisions"], 3)
        self.assertEqual(analysis["riichi_declined"], [0, 0, 0, 0])

    def test_legal_riichi_not_chosen_is_only_counted(self) -> None:
        observer = worker.RObserver()
        declined = fx.riichi_decision(fx.KANCHAN, seat=Seat.SEAT_2)
        observer.observe(2, declined, declined.legal_actions[1])
        observer.on_delivery((self.facts[0],))
        analysis = observer.finish(1)
        self.assertEqual(analysis["branches"], [])
        self.assertEqual(analysis["riichi_declined"], [0, 0, 1, 0])

    def test_the_declaration_must_follow_immediately(self) -> None:
        observer = worker.RObserver()
        observer.observe(0, fx.K0_DECISION, RIICHI)
        other = fx.discard_decision(fx.KANCHAN, seat=Seat.SEAT_1)
        observer.observe(1, other, other.legal_actions[0])
        declaration = fx.declaration_decision(fx.KANCHAN, opponent_riichi=True)
        observer.observe(0, declaration, fx.DECLARATION)
        observer.on_delivery((self.facts[0],))
        (record,) = observer.finish(1)["branches"]
        self.assertIsNone(record["declaration"])
        self.assertIn("not its declaration discard", record["invalid"][0])

    def test_round_end_before_the_declaration_is_invalid(self) -> None:
        observer = worker.RObserver()
        observer.observe(0, fx.K0_DECISION, RIICHI)
        observer.on_delivery((self.facts[0],))
        (record,) = observer.finish(1)["branches"]
        self.assertEqual(
            record["invalid"], ["the round ended before the declaration discard"]
        )

    def test_context_of_another_round_is_invalid(self) -> None:
        observer = worker.RObserver()
        observer.observe(0, fx.K9_DECISION, RIICHI)
        observer.observe(0, fx.declaration_decision(fx.SANSHOKU), fx.DECLARATION)
        observer.on_delivery((self.facts[0],))
        (record,) = observer.finish(1)["branches"]
        self.assertEqual(
            record["invalid"], ["the branch context belongs to another round"]
        )

    def test_a_second_riichi_of_the_seat_in_one_round_is_invalid(self) -> None:
        observer = worker.RObserver()
        self._first_riichi(observer)
        observer.observe(0, fx.K0_DECISION, RIICHI)
        observer.observe(
            0, fx.declaration_decision(fx.KANCHAN, opponent_riichi=True), fx.DECLARATION
        )
        observer.on_delivery((self.facts[0],))
        branches = observer.finish(1)["branches"]
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            branches[1]["invalid"], ["a second riichi of the seat in one round"]
        )

    def test_round_count_must_match(self) -> None:
        observer = worker.RObserver()
        observer.on_delivery((self.facts[0],))
        with self.assertRaises(worker.BranchObservationError):
            observer.finish(2)


# ---------------------------------------------------------------------------
# workerのD arm（engine呼び出しを差し替える）
# ---------------------------------------------------------------------------


def _engine(seat: int) -> EngineSeat:
    return list(EngineSeat)[seat]


def scripted_runner(rounds, facts, *, error=None):
    """局ごとに (seat, decision) を順にPolicyへ渡し、局の終わりにfactを配る。"""

    def runner(policies, *, seed, rules, on_delivery=None, on_selector_call=None):
        for index, decisions in enumerate(rounds):
            for seat, decision in decisions:
                policies[_engine(seat)].choose_action(decision)
            if error is not None and index == 0:
                raise error
            on_delivery((facts[index],))
        return None

    return runner


def _task(round_ordinal=0, declaration=None):
    branch = {
        "declaration": encode_action(declaration or fx.DECLARATION),
        "round": fx.context_round(
            fx.BRANCH_POINTS.get(round_ordinal, (0, fx.K0_DECISION))[1]
        ),
        "round_ordinal": round_ordinal,
        "seat": 0,
    }
    return worker.BranchTask(
        seed=SEED,
        branch_ordinal=0,
        branch=branch,
        incumbent=plan_module.champion(fx.LISJONG_REVISION).to_document(),
        wheel_path=fx.WHEEL_PATH,
    )


class DArmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = _facts()
        self.other = fx.discard_decision(fx.KANCHAN, seat=Seat.SEAT_1)

    def test_branch_point_replacement_and_abort_after_settlement(self) -> None:
        rounds = [[(1, self.other), (0, fx.K0_DECISION), (0, fx.K0_DECISION)]]
        record = worker.d_arm(_task(), runner=scripted_runner(rounds, self.facts))
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["status"], "branched")
        self.assertEqual(record["branch_decision_index"], 1)
        self.assertEqual(record["replacements"], 1)
        self.assertEqual(record["riichi_after_branch"], 0)
        self.assertEqual(record["round"], fx.context_round(fx.K0_DECISION))
        self.assertEqual(
            record["prefix_sha256"],
            canonical_digest(
                [
                    [
                        1,
                        encode_action(
                            PlacementAwareSpeedCallPolicy().choose_action(self.other)
                        ),
                    ]
                ]
            ),
        )
        self.assertEqual(
            record["settlement"],
            worker.settlement_of_fact(self.facts[0], None),
        )
        from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
            encode_context,
        )

        self.assertEqual(
            record["branch_context_sha256"],
            canonical_digest(encode_context(fx.K0_DECISION)),
        )

    def test_non_target_round_is_the_champion(self) -> None:
        k9 = fx.K9_DECISION
        rounds = [[(0, fx.K0_DECISION)], [(0, k9)]]
        task = replace(
            _task(round_ordinal=1),
            branch={
                **_task().branch,
                "round": fx.context_round(k9),
                "round_ordinal": 1,
            },
        )
        record = worker.d_arm(
            task, runner=scripted_runner(rounds, [self.facts[0], self.facts[9]])
        )
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["branch_decision_index"], 1)
        # round 0のリーチはChampionのまま（対象局外）。対象局の分岐席のリーチは0
        self.assertEqual(record["riichi_after_branch"], 0)
        self.assertEqual(record["round_ordinal"], 1)

    def test_illegal_declaration_is_invalid_not_a_crash(self) -> None:
        declaration = DiscardAction(
            actor=Seat.SEAT_0, tile=fx.tile("9p"), tsumogiri=False
        )
        rounds = [[(0, fx.K0_DECISION)]]
        record = worker.d_arm(
            _task(declaration=declaration), runner=scripted_runner(rounds, self.facts)
        )
        self.assertEqual(record["status"], "pending")
        self.assertIsNone(record["settlement"])
        self.assertIn("RiichiDamaBranchError", record["invalid"][0])

    def test_other_exceptions_propagate(self) -> None:
        rounds = [[(0, fx.K0_DECISION)]]
        runner = scripted_runner(rounds, self.facts, error=RuntimeError("engine"))
        with self.assertRaises(RuntimeError):
            worker.d_arm(_task(), runner=runner)

    def test_hanchan_end_without_the_target_settlement_is_invalid(self) -> None:
        rounds = [[(0, fx.K0_DECISION)]]
        record = worker.d_arm(
            _task(round_ordinal=3), runner=scripted_runner(rounds, self.facts)
        )
        self.assertEqual(
            record["invalid"],
            ["the hanchan finished without settling the target round"],
        )
        self.assertIsNone(record["round"])


# ---------------------------------------------------------------------------
# 1分岐の照合・Δ
# ---------------------------------------------------------------------------


def _line(round_ordinal=0, outcome="better", **changes):
    record = fx.r_record(SEED, round_ordinal)
    row = _kyoku_row()
    line = {
        "branch_id": core.branch_id(SEED, round_ordinal, 0),
        "branch_ordinal": 0,
        "context": record["context"],
        "context_sha256": record["context_sha256"],
        "d": fx.d_record(SEED, 0, core.task_branch(record), outcome),
        "d_repeat": None,
        "decision_index": record["decision_index"],
        "declaration": record["declaration"],
        "declaration_decision_index": record["declaration_decision_index"],
        "kind": "branch",
        "prefix_sha256": record["prefix_sha256"],
        "r_invalid": [],
        "r_post_sha256": record["post_sha256"],
        "r_settlement": core.r_settlement(row, round_ordinal),
        "riichi": record["riichi"],
        "round": record["round"],
        "round_ordinal": round_ordinal,
        "seat": 0,
        "seed": SEED,
    }
    line.update(changes)
    feature = {"error": None, "features": record["features"]["features"]}
    return line, feature, {"kyoku_record": row, "seed": SEED}


class BranchEvaluationTest(unittest.TestCase):
    def test_riichi_stick_paid_in_r_counts_minus_1000(self) -> None:
        line, feature, row = _line(0, "same")
        result = core.evaluate_branch(line, feature, row)
        self.assertEqual(result["invalid"], [])
        # R: Eの立直供託だけ -1000（SのロンでSへ）、D: 0 -> Δ = +10（100点単位）
        self.assertEqual(line["r_settlement"]["point_deltas"][0], -1000)
        self.assertEqual(result["delta"], 10)
        self.assertEqual(result["outcomes"], {"D": "other", "R": "other"})

    def test_stick_left_on_the_table_and_final_award_are_separate(self) -> None:
        line, feature, row = _line(9, "same")
        result = core.evaluate_branch(line, feature, row)
        self.assertEqual(result["invalid"], [])
        # R: 聴牌料 +3000 - 供託1000（場に残る）= +2000、D: +3000 -> Δ = +10
        self.assertEqual(line["r_settlement"]["point_deltas"][0], 2000)
        self.assertEqual(result["delta"], 10)
        # 残った供託はS（首位）への終局精算で、Δに入れず別列（Eは0）
        self.assertEqual(result["final_riichi_stick_awards"], {"D": 0, "R": 0})
        awards = line["r_settlement"]["final_riichi_stick_awards"]
        self.assertEqual(awards, [0, 1000, 0, 0])
        self.assertEqual(result["outcomes"], {"D": "draw", "R": "draw"})

    def test_d_win_and_deal_in_outcomes(self) -> None:
        line, feature, row = _line(0, "better")
        self.assertEqual(
            core.evaluate_branch(line, feature, row)["outcomes"]["D"], "win"
        )
        line, feature, row = _line(0, "worse")
        result = core.evaluate_branch(line, feature, row)
        self.assertEqual(result["outcomes"]["D"], "deal_in")
        self.assertLess(result["delta"], 0)

    def test_each_mismatch_is_invalid(self) -> None:
        line, feature, row = _line()
        cases = {
            "the actions before the branch point differ from R": {
                "prefix_sha256": "0" * 64
            },
            "the D branch point is at another decision index": {
                "branch_decision_index": 2
            },
            "the D branch context differs from R": {"branch_context_sha256": "0" * 64},
            "the D arm wrapper status is pending": {"status": "pending"},
            "the D arm wrapper status is missed": {"status": "missed"},
            "a riichi of the branch seat was applied in the D arm": {
                "riichi_after_branch": 1
            },
            "the D arm settled another round": {"round_ordinal": 1},
            "D: synthetic": {"invalid": ["synthetic"]},
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                broken = {**line, "d": {**line["d"], **change}}
                self.assertIn(
                    reason, core.evaluate_branch(broken, feature, row)["invalid"]
                )

    def test_settlement_and_record_mismatches_are_invalid(self) -> None:
        line, feature, row = _line()
        settlement = dict(line["d"]["settlement"])
        settlement["points_after"] = [
            value + 100 for value in settlement["points_after"]
        ]
        broken = {**line, "d": {**line["d"], "settlement": settlement}}
        self.assertIn(
            "the D round does not start from the R scores",
            core.evaluate_branch(broken, feature, row)["invalid"],
        )
        broken = {
            **line,
            "r_settlement": {**line["r_settlement"], "point_deltas": [0] * 4},
        }
        self.assertIn(
            "the R settlement differs from the kyoku record",
            core.evaluate_branch(broken, feature, row)["invalid"],
        )
        broken = {**line, "d": None}
        self.assertIn(
            "the branch has no D arm",
            core.evaluate_branch(broken, feature, row)["invalid"],
        )
        broken = {**line, "d_repeat": {**line["d"], "replacements": 3}}
        self.assertIn(
            "the D arm repeat differs",
            core.evaluate_branch(broken, feature, row)["invalid"],
        )
        rejected = {"error": "RiichiDamaAnalysisError: synthetic", "features": None}
        self.assertIn(
            "lisjong rejects the features: RiichiDamaAnalysisError: synthetic",
            core.evaluate_branch(line, rejected, row)["invalid"],
        )
        broken = {**line, "r_invalid": ["synthetic"]}
        self.assertIn(
            "R: synthetic", core.evaluate_branch(broken, feature, row)["invalid"]
        )


# ---------------------------------------------------------------------------
# 部分集合の境界（lisjongの値の比較だけ）
# ---------------------------------------------------------------------------


def _features(decision):
    api = worker.analysis_api()
    return api.analyze_riichi_decision(decision, fx.tile("1z"))


class SubsetTest(unittest.TestCase):
    def test_s1_mangan_boundary(self) -> None:
        mangan = _features(fx.K9_DECISION)
        self.assertEqual(core.subsets_of(mangan), {"S1": True, "S3": False})
        below = _features(
            fx.riichi_decision(
                fx.SANSHOKU,
                round_wind=fx.Wind.SOUTH,
                hand_number=4,
                honba=4,
                dealer=Seat.SEAT_3,
            )
        )
        # ドラなし: 4翻30符 7700（切り上げ満貫なし）は満貫未満
        self.assertEqual({wait.dama_ron.limit.name for wait in below.waits}, {"NONE"})
        self.assertFalse(core.subsets_of(below)["S1"])
        # 1つの待ちだけ満貫未満なら不成立。その待ちが見えない残り0枚（W_live外）なら成立
        first, second = mangan.waits
        weak = replace(first, dama_ron=below.waits[0].dama_ron)
        self.assertFalse(core.subsets_of(replace(mangan, waits=(weak, second)))["S1"])
        dead = replace(weak, unseen_count=0)
        self.assertTrue(core.subsets_of(replace(mangan, waits=(dead, second)))["S1"])
        none_live = replace(mangan, waits=(dead, replace(second, unseen_count=0)))
        self.assertFalse(core.subsets_of(none_live)["S1"])
        self.assertFalse(core.subsets_of(replace(mangan, furiten=True))["S1"])

    def test_s3_four_tile_boundary(self) -> None:
        features = _features(fx.K0_DECISION)
        self.assertEqual(features.total_unseen_count, 4)
        self.assertEqual(core.subsets_of(features), {"S1": False, "S3": True})
        (wait,) = features.waits
        five = replace(features, waits=(replace(wait, unseen_count=4),))
        self.assertTrue(core.subsets_of(five)["S3"])
        pinfu = _features(
            fx.riichi_decision("123m456p789s2355s1z", opponent_riichi=True)
        )
        self.assertEqual(pinfu.total_unseen_count, 8)
        self.assertFalse(core.subsets_of(pinfu)["S3"])
        first, second = pinfu.waits
        four = replace(
            pinfu,
            waits=(replace(first, unseen_count=1), replace(second, unseen_count=3)),
        )
        self.assertTrue(core.subsets_of(four)["S3"])
        five = replace(
            pinfu,
            waits=(replace(first, unseen_count=2), replace(second, unseen_count=3)),
        )
        self.assertFalse(core.subsets_of(five)["S3"])
        self.assertFalse(
            core.subsets_of(replace(features, opponent_riichi_count=0))["S3"]
        )


# ---------------------------------------------------------------------------
# bootstrap・判定
# ---------------------------------------------------------------------------


def _reference_bootstrap(clusters):
    rng = random.Random(539)
    values = []
    for _ in range(10_000):
        drawn = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        deltas = [delta for cluster in drawn for delta in cluster]
        values.append(Fraction(sum(deltas), len(deltas)))
    values.sort()
    return {"P10": values[999], "P50": values[4999], "P90": values[8999]}


class BootstrapTest(unittest.TestCase):
    CLUSTERS = [[3, -1], [0], [5, 5, -2], [-4], [1, 1]]

    def test_matches_the_reference_nearest_rank(self) -> None:
        self.assertEqual(
            core.bootstrap(self.CLUSTERS), _reference_bootstrap(self.CLUSTERS)
        )

    def test_each_subset_initialises_its_own_rng(self) -> None:
        other = [[2], [2, -7], [9]]
        first = (core.bootstrap(self.CLUSTERS), core.bootstrap(other))
        random.Random(1).random()
        second = (core.bootstrap(other), core.bootstrap(self.CLUSTERS))
        self.assertEqual(first, (second[1], second[0]))
        rows = [
            {"delta": delta, "seed": seed}
            for seed, cluster in enumerate(self.CLUSTERS)
            for delta in cluster
        ]
        stats = core.delta_stats(rows, with_bootstrap=True)
        self.assertEqual(
            stats["bootstrap"],
            {
                name: core._ratio(value)
                for name, value in core.bootstrap(self.CLUSTERS).items()
            },
        )
        self.assertEqual(stats["mean"], {"denominator": 9, "numerator": 8})
        self.assertEqual(stats["seeds"], 5)

    def test_p10_exactly_zero_is_no_signal(self) -> None:
        clusters = [[0, 0, 0]] * 7 + [[30, 30, 30]]
        result = core.bootstrap(clusters)
        self.assertEqual(result["P10"], 0)
        rows = [
            {"delta": delta, "seed": seed}
            for seed, cluster in enumerate(clusters)
            for delta in cluster
        ]
        stats = core.delta_stats(rows, with_bootstrap=True)
        self.assertGreater(
            Fraction(stats["mean"]["numerator"], stats["mean"]["denominator"]), 0
        )
        empty = core.delta_stats([], with_bootstrap=True)
        decision, checks = core.gate_decision(
            plan_module.CRITERIA, invalid=0, subsets={"S1": stats, "S3": empty}
        )
        self.assertEqual(decision, plan_module.STOP_NO_SIGNAL)
        self.assertEqual(checks["qualified_subsets"], ["S1"])


def _stats(count, seeds, *, mean=1, p10=1):
    return {
        "bootstrap": {"P10": core._ratio(Fraction(p10))},
        "count": count,
        "mean": core._ratio(Fraction(mean)),
        "seeds": seeds,
    }


class GateTest(unittest.TestCase):
    def _decide(self, s1, s3, invalid=0):
        return core.gate_decision(
            plan_module.CRITERIA, invalid=invalid, subsets={"S1": s1, "S3": s3}
        )

    def test_minimum_branches_and_seeds_are_inclusive(self) -> None:
        self.assertEqual(
            self._decide(_stats(24, 8), _stats(0, 0))[0], plan_module.PROCEED
        )
        self.assertEqual(
            self._decide(_stats(23, 8), _stats(24, 7))[0],
            plan_module.STOP_LOW_ACTIVATION,
        )
        decision, checks = self._decide(_stats(23, 9), _stats(30, 8))
        self.assertEqual(decision, plan_module.PROCEED)
        self.assertEqual(checks["signal_subsets"], ["S3"])

    def test_signal_needs_positive_mean_and_p10(self) -> None:
        self.assertEqual(
            self._decide(_stats(24, 8, mean=0), _stats(24, 8, p10=0))[0],
            plan_module.STOP_NO_SIGNAL,
        )
        decision, checks = self._decide(_stats(24, 8, p10=-1), _stats(40, 10))
        self.assertEqual(
            (decision, checks["signal_subsets"]), (plan_module.PROCEED, ["S3"])
        )

    def test_invalid_comes_first(self) -> None:
        self.assertEqual(
            self._decide(_stats(24, 8), _stats(24, 8), invalid=1)[0],
            plan_module.STOP_INVALID,
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_fixed_invariants(self) -> None:
        plan = fx.plan_document(self.tmp)
        self.assertEqual(
            plan["population"]["ordered_seeds"], list(range(539_000, 539_024))
        )
        self.assertEqual(plan["verify_replay_seeds"], [539_000, 539_023])
        self.assertEqual(plan["execution"]["timeout_seconds"], 600)
        self.assertEqual(plan["resource_limits"]["wall_seconds"], 4 * 3600)
        self.assertEqual(plan["resource_limits"]["process_tree_rss_bytes"], 4 * 1024**3)
        self.assertEqual(plan["criteria"]["minimum_branches"], 24)
        self.assertEqual(plan["criteria"]["minimum_seeds"], 8)
        self.assertEqual(plan["participant"]["behavior_configuration"], {})
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal", name="r")
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["verify_replay_seeds"], [])
        self.assertEqual(rehearsal["resource_limits"]["wall_seconds"], 3600)
        self.assertIsNone(rehearsal["time_estimate"])

    def test_drift_is_detected(self) -> None:
        plan = fx.plan_document(self.tmp)
        drifts = {
            "criteria": {**plan["criteria"], "minimum_branches": 12},
            "verify_replay_seeds": [539_000],
            "protocol_version": "riichi-dama-branch-v0",
            "features_version": "riichi-dama-features-v0",
            "execution": {**plan["execution"], "timeout_seconds": 900},
            "resource_limits": {**plan["resource_limits"], "wall_seconds": 10**6},
            "population": {
                **plan["population"],
                "ordered_seeds": list(range(539_001, 539_025)),
            },
            "participant": {**plan["participant"], "behavior_configuration": {"x": 1}},
            "rules_digest": "0" * 64,
        }
        for name, value in drifts.items():
            # participantの改ざんはprotocol共通の検査（基底class）で拒否される
            with self.subTest(name=name), self.assertRaises(EngineAabbProtocolError):
                plan_module.parse_plan({**plan, name: value})
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            plan_module.parse_plan({**plan, "plan_digest": "0" * 64})

    def test_owner_observation_and_rehearsal_rules(self) -> None:
        for owner in ("lisbun/lisjong-arena#540", "lisbun/lisjong-arena#100", "x"):
            with (
                self.subTest(owner=owner),
                self.assertRaises(plan_module.RiichiDamaBranchError),
            ):
                fx.plan_document(self.tmp, owner_issue=owner)
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            fx.plan_document(self.tmp, observation="unobserved")
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            fx.plan_document(
                self.tmp, run_kind="rehearsal", time_estimate=fx.time_estimate()
            )

    def test_allocated_requires_a_within_budget_estimate(self) -> None:
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            fx.plan_document(self.tmp, time_estimate=None)
        slow = fx.time_estimate(d_wall=40.0)
        self.assertEqual(slow["verdict"], plan_module.NOT_STARTED_BUDGET)
        with self.assertRaisesRegex(
            plan_module.RiichiDamaBranchError, "NOT-STARTED-BUDGET"
        ):
            fx.plan_document(self.tmp, time_estimate=slow)
        other = fx.time_estimate(environment_digest="9" * 64)
        with self.assertRaisesRegex(
            plan_module.RiichiDamaBranchError, "another environment"
        ):
            fx.plan_document(self.tmp, time_estimate=other)
        tampered = {**fx.time_estimate(), "projected_seconds": 1.0}
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            fx.plan_document(self.tmp, time_estimate=tampered)
        verdict = {**fx.time_estimate(d_wall=40.0)}
        verdict["verdict"] = plan_module.WITHIN_BUDGET
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            plan_module.parse_time_estimate(verdict)

    def test_estimate_formula(self) -> None:
        values = plan_module.estimate_values(
            r_walls=[40.0, 60.0],
            d_walls=[20.0, 30.0],
            branches=10,
            rehearsal_seat_hanchans=8,
        )
        # 10/8 < 3.45 なので n̂ = 3.45 × 96 = 331.2
        self.assertEqual(values["n_hat"], {"denominator": 5, "numerator": 1656})
        self.assertEqual(values["projected_seconds"], 24 * 50.0 + 331.2 * 25.0)
        self.assertEqual(values["verdict"], plan_module.WITHIN_BUDGET)
        many = plan_module.estimate_values(
            r_walls=[50.0, 50.0], d_walls=[26.0], branches=40, rehearsal_seat_hanchans=8
        )
        self.assertEqual(many["n_hat"], {"denominator": 1, "numerator": 480})
        self.assertEqual(many["verdict"], plan_module.NOT_STARTED_BUDGET)

    def test_seed_overlap_with_another_allocation_is_rejected(self) -> None:
        from lisjong_arena import seed_registry

        # 同じdomainの重複はledger自体が拒否するので、別domainの既存allocationで確かめる
        other = fx.allocation(
            seeds=(539_010,),
            owner="lisbun/lisjong-arena#999",
            population="other",
            domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        )
        document = fx.ledger(fx.RECORD, other)
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            fx.plan_document(self.tmp, ledger=document)

    def test_plan_is_write_once(self) -> None:
        path, document = fx.write_plan(self.tmp)
        with self.assertRaises(FileExistsError):
            plan_module.save_plan(document, path)
        self.assertEqual(plan_module.load_plan(path), document)


# ---------------------------------------------------------------------------
# run / verify / compare / estimate
# ---------------------------------------------------------------------------


class _RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_allocated(self, play_r, play_d, *, name="run"):
        path, plan = fx.write_plan(self.tmp, name=name)
        summary = core.run_plan(
            path,
            ledger_path=self._ledger_path(),
            play_r=play_r,
            play_d=play_d,
            live_gate=fx.gate,
        )
        return path, plan, summary

    def _ledger_path(self):
        from lisjong_arena import seed_registry

        path = self.tmp / "ledger.json"
        if not path.exists():
            seed_registry.write_ledger(path, fx.ledger())
        return path

    def verify(self, path, play_r=fx.fake_r_both, play_d=fx.fake_d_better, **kwargs):
        return core.verify_plan(
            path, environment=fx.ENVIRONMENT, play_r=play_r, play_d=play_d, **kwargs
        )


class RunTest(_RunCase):
    def test_proceed_publishes_and_verifies_with_replay(self) -> None:
        path, plan, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        self.assertEqual(summary["gate"]["decision"], plan_module.PROCEED)
        self.assertEqual(summary["gate"]["checks"]["signal_subsets"], ["S1", "S3"])
        self.assertEqual(summary["branches"], 48)
        self.assertEqual(summary["subsets"]["S1"]["count"], 24)
        self.assertEqual(summary["subsets"]["S3"]["seeds"], 24)
        self.assertEqual(summary["riichi_declined"], 24)
        self.assertEqual(
            summary["replacements"], {"branches_with_replacement": 24, "total": 24}
        )
        self.assertEqual(summary["arms"]["seat_outcomes"]["D"]["win"], 24)
        output = Path(plan["artifact_destinations"]["output"])
        self.assertEqual(
            sorted(item.name for item in output.iterdir()),
            sorted(
                [
                    "SHA256SUMS",
                    "branches.jsonl",
                    "features.jsonl",
                    "hanchans.json",
                    "summary.json",
                    "timings.json",
                ]
            ),
        )
        verified = self.verify(path)
        self.assertEqual(verified["decision"], plan_module.PROCEED)
        self.assertEqual(verified["rederived_lisjong_features"], 48)
        self.assertEqual(verified["replayed"], {"arms": 4, "seeds": [539_000, 539_023]})
        branches = fx.read_lines(output / "branches.jsonl")
        self.assertEqual(branches[0]["schema"], core.BRANCHES_SCHEMA)
        self.assertEqual(branches[1]["branch_id"], "539000-0-0")
        self.assertNotIn("features", branches[1])

    def test_low_activation(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_seven_seeds, fx.fake_d_better)
        self.assertEqual(summary["gate"]["decision"], plan_module.STOP_LOW_ACTIVATION)

    def test_no_signal(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_worse)
        self.assertEqual(summary["gate"]["decision"], plan_module.STOP_NO_SIGNAL)

    def test_prefix_mismatch_is_stop_invalid(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_prefix_mismatch)
        self.assertEqual(summary["gate"]["decision"], plan_module.STOP_INVALID)
        self.assertEqual(
            summary["invalid"]["branches"],
            [
                {
                    "branch_id": "539005-0-0",
                    "reasons": ["the actions before the branch point differ from R"],
                }
            ],
        )

    def test_missing_declaration_runs_no_d_arm_and_is_invalid(self) -> None:
        path, plan, summary = self.run_allocated(
            fx.fake_r_missing_declaration, fx.fake_d_better
        )
        self.assertEqual(summary["gate"]["decision"], plan_module.STOP_INVALID)
        self.assertEqual(summary["invalid"]["count"], 1)
        timings = core.read_document(
            Path(plan["artifact_destinations"]["output"]) / "timings.json", "timings"
        )
        self.assertEqual(sum(1 for row in timings["tasks"] if row["arm"] == "D"), 47)

    def test_failure_is_incomplete_without_output(self) -> None:
        path, plan = fx.write_plan(self.tmp)
        with self.assertRaises(RunStopped):
            core.run_plan(
                path,
                ledger_path=self._ledger_path(),
                play_r=fx.fake_r_failure,
                play_d=fx.fake_d_better,
                live_gate=fx.gate,
            )
        self.assertFalse(Path(plan["artifact_destinations"]["output"]).exists())
        stops = list(Path(plan["artifact_destinations"]["diagnostics"]).iterdir())
        self.assertEqual(len(stops), 1)
        self.assertIn("synthetic worker failure", stops[0].read_text(encoding="utf-8"))

    def test_output_is_write_once(self) -> None:
        path, _, _ = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        with self.assertRaises(plan_module.RiichiDamaBranchError):
            core.run_plan(
                path,
                ledger_path=self._ledger_path(),
                play_r=fx.fake_r_both,
                play_d=fx.fake_d_better,
                live_gate=fx.gate,
            )


class VerifyTamperTest(_RunCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._shared = tempfile.TemporaryDirectory()
        shared = Path(cls._shared.name)
        cls.path, cls.plan = fx.write_plan(shared)
        from lisjong_arena import seed_registry

        ledger_path = shared / "ledger.json"
        seed_registry.write_ledger(ledger_path, fx.ledger())
        core.run_plan(
            cls.path,
            ledger_path=ledger_path,
            play_r=fx.fake_r_both,
            play_d=fx.fake_d_better,
            live_gate=fx.gate,
        )
        cls.output = Path(cls.plan["artifact_destinations"]["output"])
        cls.pristine = {item.name: item.read_bytes() for item in cls.output.iterdir()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._shared.cleanup()

    def tearDown(self) -> None:
        for item in list(self.output.iterdir()):
            if item.name not in self.pristine:
                item.unlink()
        for name, data in self.pristine.items():
            (self.output / name).write_bytes(data)
        super().tearDown()

    def _resum(self):
        (self.output / "SHA256SUMS").write_text(
            core._sums_text(self.output), encoding="utf-8", newline="\n"
        )

    def _rejects(self, pattern):
        with self.assertRaisesRegex(plan_module.RiichiDamaBranchError, pattern):
            self.verify(self.path)

    def test_changed_file_without_resum(self) -> None:
        lines = fx.read_lines(self.output / "branches.jsonl")
        lines[3]["d"]["replacements"] += 1
        fx.write_lines(self.output / "branches.jsonl", lines)
        self._rejects("SHA256SUMS")

    def test_changed_settlement_with_resum(self) -> None:
        lines = fx.read_lines(self.output / "branches.jsonl")
        settlement = lines[3]["d"]["settlement"]
        settlement["point_deltas"] = [value * 2 for value in settlement["point_deltas"]]
        fx.write_lines(self.output / "branches.jsonl", lines)
        self._resum()
        self._rejects("summary differs|inputs")

    def test_missing_and_extra_files(self) -> None:
        (self.output / "features.jsonl").unlink()
        self._resum()
        self._rejects("expected set")
        self.tearDown()
        (self.output / "extra.json").write_text("{}", encoding="utf-8")
        self._resum()
        self._rejects("expected set")

    def test_reordered_and_duplicated_branches(self) -> None:
        lines = fx.read_lines(self.output / "branches.jsonl")
        lines[1], lines[2] = lines[2], lines[1]
        fx.write_lines(self.output / "branches.jsonl", lines)
        self._resum()
        self._rejects("ordinal|order")
        self.tearDown()
        lines = fx.read_lines(self.output / "branches.jsonl")
        lines.insert(2, {**lines[1], "branch_ordinal": 1})
        for ordinal, line in enumerate(lines[1:]):
            line["branch_ordinal"] = ordinal
        fx.write_lines(self.output / "branches.jsonl", lines)
        self._resum()
        self._rejects("D arm|duplicated|two branch|one-to-one|belongs")

    def test_tampered_features_are_not_rederived(self) -> None:
        lines = fx.read_lines(self.output / "features.jsonl")
        lines[1]["features"]["rank"] = 2
        fx.write_lines(self.output / "features.jsonl", lines)
        self._resum()
        self._rejects("summary differs|re-derived features")

    def test_hanchan_schedule_tamper(self) -> None:
        document = core.read_document(self.output / "hanchans.json", "hanchans")
        document["hanchans"].reverse()
        (self.output / "hanchans.json").write_text(
            core.canonical_json_text(document), encoding="utf-8", newline="\n"
        )
        self._resum()
        self._rejects("schedule")

    def test_replay_mismatch_is_detected(self) -> None:
        with self.assertRaisesRegex(
            plan_module.RiichiDamaBranchError, "replayed D arm differs"
        ):
            self.verify(self.path, play_d=fx.fake_d_same)
        with self.assertRaisesRegex(plan_module.RiichiDamaBranchError, "replayed R"):
            self.verify(self.path, play_r=fx.fake_r_seven_seeds)

    def test_environment_mismatch(self) -> None:
        other = {
            **fx.ENVIRONMENT,
            "python": {"implementation": "CPython", "version": "3.14.0"},
        }
        with self.assertRaisesRegex(plan_module.RiichiDamaBranchError, "environment"):
            core.verify_plan(
                self.path,
                environment=other,
                play_r=fx.fake_r_both,
                play_d=fx.fake_d_better,
            )


class RehearsalTest(_RunCase):
    def _run(self, observation, play_r, play_d=fx.fake_d_better, name="r"):
        path, plan = fx.write_plan(
            self.tmp, run_kind="rehearsal", observation=observation, name=name
        )
        summary = core.run_plan(path, play_r=play_r, play_d=play_d, live_gate=fx.gate)
        return path, plan, summary

    def test_match_estimate_and_allocated_plan(self) -> None:
        observed, plan, summary = self._run("observed", fx.fake_r_rehearsal)
        self.assertIsNone(summary["gate"])
        self.assertEqual(summary["status"], core.REHEARSAL_STATUS)
        lines = fx.read_lines(
            Path(plan["artifact_destinations"]["output"]) / "branches.jsonl"
        )
        self.assertTrue(all(line["d_repeat"] == line["d"] for line in lines[1:]))
        unobserved, _, unobserved_summary = self._run(
            "unobserved", fx.fake_r_unobserved
        )
        self.assertNotIn("subsets", unobserved_summary)
        compared = core.compare_rehearsals(
            observed,
            unobserved,
            output=self.tmp / "compare.json",
            environment=fx.ENVIRONMENT,
        )
        self.assertTrue(compared["match"])
        estimate = core.estimate_budget(
            observed,
            unobserved,
            output=self.tmp / "estimate.json",
            environment=fx.ENVIRONMENT,
        )
        self.assertEqual(estimate["verdict"], plan_module.WITHIN_BUDGET)
        self.assertEqual(estimate["branches"], 4)
        self.assertEqual(estimate["d_arms"], 4)
        self.assertEqual(estimate["n_hat"], {"denominator": 5, "numerator": 1656})
        self.assertEqual(
            plan_module.load_time_estimate(self.tmp / "estimate.json"), estimate
        )
        allocated = fx.plan_document(self.tmp, time_estimate=estimate, name="after")
        self.assertEqual(allocated["time_estimate"], estimate)

    def test_unobserved_difference_fails(self) -> None:
        observed, _, _ = self._run("observed", fx.fake_r_rehearsal)
        unobserved, _, _ = self._run("unobserved", fx.fake_r_unobserved_other)
        with self.assertRaisesRegex(plan_module.RiichiDamaBranchError, "STOP-INVALID"):
            core.compare_rehearsals(
                observed,
                unobserved,
                output=self.tmp / "c.json",
                environment=fx.ENVIRONMENT,
            )
        compared = core.read_document(self.tmp / "c.json", "compare")
        self.assertFalse(compared["match"])
        self.assertEqual(
            compared["seeds"][1]["differences"], ["action_sequence_sha256"]
        )

    def test_d_arm_repeat_difference_fails(self) -> None:
        marker = self.tmp / "repeat-marker"
        os.environ["RIICHI_DAMA_TEST_REPEAT_MARKER"] = str(marker)
        try:
            observed, plan, summary = self._run(
                "observed", fx.fake_r_rehearsal, fx.fake_d_unstable
            )
        finally:
            del os.environ["RIICHI_DAMA_TEST_REPEAT_MARKER"]
        self.assertEqual(summary["invalid"]["count"], 1)
        self.assertIn(
            "the D arm repeat differs", summary["invalid"]["branches"][0]["reasons"]
        )
        unobserved, _, _ = self._run("unobserved", fx.fake_r_unobserved)
        with self.assertRaisesRegex(plan_module.RiichiDamaBranchError, "STOP-INVALID"):
            core.compare_rehearsals(observed, unobserved, environment=fx.ENVIRONMENT)


class RunPolicyHanchanDeliveryTest(unittest.TestCase):
    """``run_policy_hanchan`` は ``on_delivery`` をengineへそのまま渡す（engineは動かさない）。"""

    def _policies(self):
        return {seat: PlacementAwareSpeedCallPolicy() for seat in EngineSeat}

    def test_on_delivery_is_passed_through_and_defaults_are_unchanged(self) -> None:
        from unittest import mock

        from lisjong_arena.lisjong_engine import hanchan

        def callback(items):
            return None

        with mock.patch.object(hanchan, "run_hanchan", return_value="match") as run:
            self.assertEqual(
                hanchan.run_policy_hanchan(
                    self._policies(), seed=1, on_delivery=callback
                ),
                "match",
            )
            self.assertEqual(run.call_args.kwargs, {"on_delivery": callback})
            hanchan.run_policy_hanchan(self._policies(), seed=1)
            self.assertEqual(run.call_args.kwargs, {})
        with self.assertRaises(TypeError):
            hanchan.run_policy_hanchan(self._policies(), seed=1, on_delivery=1)


if __name__ == "__main__":
    unittest.main()
