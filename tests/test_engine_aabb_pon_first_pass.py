"""#553 pon-first-pass-branch-v1（P1 pilot）の観測・照合・配分判定・plan・run・verify。

実engineの対局・Rust wheel・実Policyの半荘実行は使わない（engine呼び出しとworkerの
playを差し替える）。lisjongの分岐点の共有判定・P1 wrapper・鳴き解析は実物を使う。
"""

import os
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import _engine_aabb_call_value_shadow_fixtures as cvfx
import _engine_aabb_pon_first_pass_fixtures as fx
from _engine_aabb_record_fixtures import SCRIPT_X, play
from lisjong.policies import PlacementAwareSpeedCallPolicy
from lisjong_engine.round_completion import project_round_completion
from lisjong_engine.round_evidence import (
    MeldCalledEvidence,
    ResponseOutcome,
    RoundEndedEvidence,
    RoundEndKind,
)
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.win_context import WinMethod

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import pon_first_pass as core
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_allocation as rules,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_worker as worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.call_value_shadow_worker import (
    CANCELLED_RON,
    ESTABLISHED,
    INVALID,
    encode_action,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)
from lisjong_arena.heuristic_candidate_engine_aabb.riichi_dama_branch_plan import (
    champion,
)

SEED = plan_module.SEEDS[0]


# ---------------------------------------------------------------------------
# 配分判定（lisjong #362の修正Cと境界条件）
# ---------------------------------------------------------------------------


class AllocationTest(unittest.TestCase):
    def test_reviewed_four_conditions(self) -> None:
        cases = [
            ((44, 23, Fraction(5, 2)), 190, 475, rules.NOT_VIABLE_SUPPORT),
            ((44, 23, Fraction(5)), 120, 600, rules.SUPPORT_OK),
            ((65, 35, Fraction(5, 2)), 124, 310, rules.NOT_VIABLE_SUPPORT),
            ((65, 35, Fraction(5)), 78, 390, rules.NOT_VIABLE_SUPPORT),
        ]
        for (r_mean, d_mean, rate), seeds, branches, verdict in cases:
            with self.subTest(r=r_mean, d=d_mean, rate=rate):
                judgment = rules.support_judgment(
                    r_mean=r_mean,
                    d_mean=d_mean,
                    branches=int(rate * 24),
                    seeds=24,
                )
                self.assertEqual(judgment["allocation"]["split_seeds"], seeds)
                self.assertEqual(judgment["allocation"]["blocks_per_split"], 2)
                self.assertEqual(
                    judgment["projected_test_branches"],
                    {"denominator": 1, "numerator": branches},
                )
                self.assertEqual(judgment["verdict"], verdict)
                self.assertTrue(judgment["not_a_learning_decision"])

    def test_the_500_branch_floor_is_inclusive(self) -> None:
        # c = 40 + 5 × 30 = 190: n_blk = 50、T(2) = 100 -> 100 × 5 = 500
        exact = rules.support_judgment(r_mean=40, d_mean=30, branches=120, seeds=24)
        self.assertEqual(exact["allocation"]["split_seeds"], 100)
        self.assertEqual(exact["verdict"], rules.SUPPORT_OK)
        # わずかに遅い: n_blk = 49、T(2) = 98 -> 490
        slower = rules.support_judgment(r_mean=40, d_mean=30.1, branches=120, seeds=24)
        self.assertEqual(slower["allocation"]["split_seeds"], 98)
        self.assertEqual(slower["verdict"], rules.NOT_VIABLE_SUPPORT)

    def test_no_established_branch_is_undefined_not_zero(self) -> None:
        judgment = rules.support_judgment(r_mean=50, d_mean=None, branches=0, seeds=24)
        self.assertEqual(judgment["verdict"], rules.NOT_VIABLE_SUPPORT)
        self.assertEqual(judgment["reason"], rules.NO_BRANCH)
        self.assertIsNone(judgment["d_mean_wall_seconds"])
        self.assertIsNone(judgment["cost_per_seed_seconds"])
        self.assertIsNone(judgment["projected_test_branches"])

    def test_no_valid_allocation(self) -> None:
        # c = 5000秒: 1ブロックに本実行2 seedsとverify 2 seedsが入らない
        judgment = rules.support_judgment(
            r_mean=4000, d_mean=1000, branches=24, seeds=24
        )
        self.assertEqual(judgment["block_capacity"], 0)
        self.assertIsNone(judgment["allocation"])
        self.assertEqual(judgment["reason"], rules.NO_VALID_ALLOCATION)
        self.assertEqual(judgment["verdict"], rules.NOT_VIABLE_SUPPORT)

    def test_every_block_verifies_two_distinct_seeds(self) -> None:
        for cost in (101.5, 159.0, 240.0, 900.0, 1800.0, 2400.0):
            with self.subTest(cost=cost):
                chosen = rules.allocation(cost)
                if chosen is None:
                    self.assertLess(rules.block_capacity(cost), 2)
                    continue
                sizes = chosen["block_seeds"]
                self.assertEqual(sum(sizes), chosen["split_seeds"])
                self.assertLessEqual(max(sizes) - min(sizes), 1)
                self.assertGreaterEqual(min(sizes), 2)
                self.assertLessEqual(max(sizes), rules.block_capacity(cost))
                # 1 split（12時間の半分）に収まる
                spent = chosen["split_seeds"] * cost + chosen["blocks_per_split"] * (
                    2 * cost + 900
                )
                self.assertLessEqual(spent, rules.SPLIT_BUDGET_SECONDS)
        self.assertEqual(rules.block_sizes(7, 3), [3, 2, 2])

    def test_block_start_keeps_the_seed_count_and_stops_when_slower(self) -> None:
        start = rules.block_start_condition(
            block_seeds=60,
            r_mean=44,
            d_mean=23,
            rate=Fraction(5),
            session_elapsed_seconds=1000.0,
        )
        self.assertEqual(start["verdict"], rules.BLOCK_WITHIN_LIMITS)
        self.assertEqual(start["projected_seconds"], 62 * 159 + 900)
        slower = rules.block_start_condition(
            block_seeds=60,
            r_mean=65,
            d_mean=35,
            rate=Fraction(5),
            session_elapsed_seconds=1000.0,
        )
        # seed数は60のまま（縮めない）で、3時間を超えるので開始しない
        self.assertEqual(slower["block_seeds"], 60)
        self.assertEqual(slower["verdict"], rules.BLOCK_NOT_STARTED)
        late = rules.block_start_condition(
            block_seeds=60,
            r_mean=44,
            d_mean=23,
            rate=Fraction(5),
            session_elapsed_seconds=14_400.0 - 10_000.0,
        )
        self.assertEqual(late["limit_seconds"], 10_000.0)
        self.assertEqual(late["verdict"], rules.BLOCK_NOT_STARTED)
        with self.assertRaises(ValueError):
            rules.block_start_condition(
                block_seeds=1,
                r_mean=44,
                d_mean=23,
                rate=Fraction(5),
                session_elapsed_seconds=0.0,
            )


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


def _fact(ordinal=0):
    return project_round_completion(play(SEED, SCRIPT_X).history[ordinal])


def _ron_ended(call):
    target = cvfx.engine_seat(call.target)
    return RoundEndedEvidence(
        kind=RoundEndKind.WIN,
        win_method=WinMethod.RON,
        winner_seats=(target.next(),),
        source_seat=target,
    )


class RObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decision = fx.K0_DECISION
        self.other = fx.v1.discard_decision(fx.v1.KANCHAN, seat=fx.v1.Seat.SEAT_1)
        self.other_action = self.other.legal_actions[0]

    def _observe(self, evidence):
        observer = worker.PonRObserver()
        observer.observe(1, self.other, self.other_action)
        observer.observe(0, self.decision, fx.PON)
        observer.on_round_evidence_complete(cvfx.completion(self.decision, evidence))
        observer.on_delivery((_fact(0),))
        return observer.finish(1)

    def test_champion_pons_from_the_closed_hand(self) -> None:
        self.assertEqual(
            PlacementAwareSpeedCallPolicy().choose_action(self.decision), fx.PON
        )

    def test_established_candidate_separates_decision_input_and_selection(self) -> None:
        analysis = self._observe(cvfx.established(fx.PON, fx.CALLED_ORDER))
        (record,) = analysis["candidates"]
        self.assertEqual(record["invalid"], [])
        self.assertEqual(set(record["decision_input"]), worker.DECISION_INPUT_FIELDS)
        self.assertEqual(set(record["selection"]), worker.SELECTION_FIELDS)
        self.assertEqual(
            record["selection"],
            {"called_discard_order": fx.CALLED_ORDER, "resolution": ESTABLISHED},
        )
        self.assertEqual(record["decision_input"]["pon"], encode_action(fx.PON))
        self.assertIsNone(record["decision_input"]["call_analysis_error"])
        self.assertEqual(record["decision_index"], 1)
        self.assertEqual(len(analysis["actions"]), 2)
        self.assertEqual(
            analysis["actions"][1][3], self.decision.legal_actions.index(fx.PON)
        )

    def test_ron_cancellation_and_impossible_resolutions(self) -> None:
        cancelled = self._observe(
            cvfx.epoch(fx.PON, fx.CALLED_ORDER, ResponseOutcome.RON, _ron_ended(fx.PON))
        )["candidates"][0]
        self.assertEqual(cancelled["selection"]["resolution"], CANCELLED_RON)
        self.assertEqual(cancelled["invalid"], [])
        other = cvfx.engine_seat(fx.v1.Seat.SEAT_3)
        meld = cvfx.engine_meld(fx.PON)
        priority = self._observe(
            cvfx.epoch(
                fx.PON,
                fx.CALLED_ORDER,
                ResponseOutcome.CALL,
                MeldCalledEvidence(other, meld, fx.CALLED_ORDER),
            )
        )["candidates"][0]
        self.assertEqual(priority["selection"]["resolution"], "CANCELLED-PRIORITY-CALL")
        self.assertTrue(priority["invalid"])
        missing = self._observe(())["candidates"][0]
        self.assertEqual(missing["selection"]["resolution"], INVALID)
        self.assertTrue(missing["invalid"])

    def test_only_the_first_closed_pon_of_a_seat_and_round(self) -> None:
        observer = worker.PonRObserver()
        observer.observe(0, self.decision, fx.PON)
        observer.observe(0, self.decision, fx.PON)
        observer.observe(0, self.decision, fx.PASS)
        self.assertEqual(len(observer.candidates), 1)
        observer.on_round_evidence_complete(
            cvfx.completion(self.decision, cvfx.established(fx.PON, fx.CALLED_ORDER))
        )
        observer.on_delivery((_fact(0),))
        # 次の局ではもう一度候補になる
        observer.observe(0, fx.K9_DECISION, fx.PON)
        self.assertEqual(len(observer.candidates), 2)

    def test_pon_from_an_open_hand_is_not_a_candidate(self) -> None:
        from lisjong.policy_contract.meld import MeldKind, PublicMeld

        tile = fx.v1.tile("1z")
        meld = PublicMeld(
            kind=MeldKind.PON,
            tiles=(tile,) * 3,
            from_seat=fx.v1.Seat.SEAT_3,
            called_tile=tile,
        )
        players = list(self.decision.input.players)
        players[0] = replace(players[0], melds=(meld,))
        concealed = fx.v1.hand("1235799m2233z")
        decision = replace(
            self.decision,
            input=replace(
                self.decision.input,
                players=tuple(players),
                own_hand=replace(
                    self.decision.input.own_hand, concealed_tiles=concealed
                ),
            ),
        )
        observer = worker.PonRObserver()
        observer.observe(0, decision, fx.PON)
        self.assertEqual(observer.candidates, [])

    def test_wrapper_version_is_required(self) -> None:
        api = worker.analysis_api()
        self.assertEqual(api.PON_FIRST_PASS_BRANCH_VERSION, worker.WRAPPER_VERSION)


def scripted_runner(rounds, facts, evidence):
    """局ごとに (seat, decision) を順にPolicyへ渡し、局の終わりにevidenceとfactを配る。"""

    def runner(
        policies,
        *,
        seed,
        rules,
        on_delivery=None,
        on_selector_call=None,
        on_round_evidence_complete=None,
    ):
        for index, decisions in enumerate(rounds):
            for seat, decision in decisions:
                policies[list(EngineSeat)[seat]].choose_action(decision)
            on_round_evidence_complete(evidence[index])
            on_delivery((facts[index],))
        return None

    return runner


def _task():
    branch = {
        "pon": encode_action(fx.PON),
        "round": fx.v1.context_round(fx.K0_DECISION),
        "round_ordinal": 0,
        "seat": 0,
    }
    return worker.BranchTask(
        seed=SEED,
        branch_ordinal=0,
        branch=branch,
        incumbent=champion(fx.LISJONG_REVISION).to_document(),
        wheel_path=fx.WHEEL_PATH,
    )


def _later_pon_decision():
    """分岐の後、同じ局でWの別の9m（order 30）へ再びPonを選べる判断。"""
    return fx.pon_decision(called_order=30)


class DArmTest(unittest.TestCase):
    def _run(self, evidence):
        later = _later_pon_decision()
        rounds = [[(0, fx.K0_DECISION), (0, later)]]
        completion = cvfx.completion(fx.K0_DECISION, evidence)
        return worker.d_arm(
            _task(), runner=scripted_runner(rounds, [_fact(0)], [completion])
        )

    def test_branch_passes_and_the_later_call_is_resolved_from_evidence(self) -> None:
        evidence = (
            *cvfx.epoch(fx.PON, fx.CALLED_ORDER, ResponseOutcome.NO_PUBLIC_RESPONSE),
            *cvfx.established(fx.PON, 30),
        )
        record = self._run(evidence)
        self.assertEqual(record["invalid"], [])
        self.assertEqual(record["status"], "branched")
        self.assertEqual(record["branch_decision_index"], 0)
        self.assertEqual(record["branch_action"], encode_action(fx.PASS))
        self.assertIs(record["pass_applied"], True)
        # 選択回数（wrapper）と成立（evidence）を別に持つ
        self.assertEqual(record["wrapper_calls_after_branch"], 1)
        self.assertEqual(
            record["calls_after_branch"],
            [
                {
                    "decision_index": 1,
                    "kind": "pon",
                    "resolution": ESTABLISHED,
                    "seat_decisions_after_branch": 1,
                }
            ],
        )
        self.assertIsNotNone(record["settlement"])
        # 分岐前の時間は記録本体と分けた測定値（worker内のmonotonic）
        measurement = record["measurement"]
        self.assertEqual(measurement["clock"], worker.MEASUREMENT_CLOCK)
        self.assertLessEqual(
            measurement["pre_branch_seconds"], measurement["arm_seconds"]
        )
        body, split = worker.split_measurement(record)
        self.assertNotIn("measurement", body)
        self.assertEqual(split, measurement)

    def test_a_meld_of_the_passed_discard_is_detected(self) -> None:
        evidence = (*cvfx.established(fx.PON, fx.CALLED_ORDER),)
        record = self._run(evidence)
        self.assertIs(record["pass_applied"], False)

    def test_a_ron_cancelled_later_call_is_selected_but_not_established(self) -> None:
        evidence = (
            *cvfx.epoch(fx.PON, fx.CALLED_ORDER, ResponseOutcome.NO_PUBLIC_RESPONSE),
            *cvfx.epoch(fx.PON, 30, ResponseOutcome.RON, _ron_ended(fx.PON)),
        )
        record = self._run(evidence)
        self.assertEqual(record["wrapper_calls_after_branch"], 1)
        self.assertEqual(record["calls_after_branch"][0]["resolution"], CANCELLED_RON)


# ---------------------------------------------------------------------------
# 1候補の照合とfield分離
# ---------------------------------------------------------------------------


def _row():
    from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
        SeatAssignment,
        project_row,
    )

    return {
        "kyoku_record": project_row(
            play(SEED, SCRIPT_X),
            seed=SEED,
            keys={"placement": 0},
            seats=(SeatAssignment("B", fx.CHAMPION),) * 4,
            selector_call_counts=(fx.DECISIONS,) * 4,
            policy_call_counts=(fx.DECISIONS,) * 4,
            layout=core.LAYOUT,
        )
    }


def _line(round_ordinal, **d_changes):
    record = fx.candidate(round_ordinal)
    row = _row()
    line = {
        "branch_id": f"{SEED}-{round_ordinal}-0",
        "branch_ordinal": 0,
        "d": None,
        "d_repeat": None,
        "decision_index": record["decision_index"],
        "decision_input": record["decision_input"],
        "kind": "candidate",
        "prefix_sha256": record["prefix_sha256"],
        "r_invalid": [],
        "r_post_sha256": record["post_sha256"],
        "r_settlement": core.r_settlement(row["kyoku_record"], round_ordinal),
        "round": record["round"],
        "round_ordinal": round_ordinal,
        "seat": 0,
        "seed": SEED,
        "selection": record["selection"],
    }
    if record["selection"]["resolution"] == ESTABLISHED:
        line["d"] = fx.d_record(
            SEED, 0, worker.task_branch(record), "better", **d_changes
        )
    return line, row


class EvaluateTest(unittest.TestCase):
    def test_ron_cancelled_candidate_is_counted_not_a_branch(self) -> None:
        line, row = _line(0)
        result = core.evaluate_line(line, row)
        self.assertEqual(result["invalid"], [])
        self.assertEqual(result["status"], "ron_cancelled")
        self.assertIsNone(result["delta"])

    def test_established_branch_has_a_delta(self) -> None:
        line, row = _line(9)
        result = core.evaluate_line(line, row)
        self.assertEqual(result["invalid"], [])
        self.assertEqual(result["status"], "branch")
        self.assertEqual(result["delta"], result["g_d"] - result["g_r"])
        self.assertIn(result["route"], core._ROUTES)
        self.assertIs(result["opponent_riichi"], False)

    def test_d_arm_checks(self) -> None:
        cases = {
            "not passed": {"branch_action": encode_action(fx.PON)},
            "pass not applied": {"pass_applied": False},
            "count mismatch": {"wrapper_calls_after_branch": 1},
            "missed": {"status": "missed"},
            "prefix": {"prefix_sha256": "0" * 64},
            "context": {"branch_context_sha256": "0" * 64},
            "unexplained call": {
                "calls_after_branch": [
                    {
                        "decision_index": 41,
                        "kind": "chi",
                        "resolution": INVALID,
                        "seat_decisions_after_branch": 1,
                    }
                ],
                "wrapper_calls_after_branch": 1,
            },
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                line, row = _line(9, **changes)
                self.assertTrue(core.evaluate_line(line, row)["invalid"])

    def test_a_chi_after_the_branch_may_lose_to_a_priority_call(self) -> None:
        def calls(kind, resolution):
            return {
                "calls_after_branch": [
                    {
                        "decision_index": 41,
                        "kind": kind,
                        "resolution": resolution,
                        "seat_decisions_after_branch": 1,
                    }
                ],
                "wrapper_calls_after_branch": 1,
            }

        # 分岐後のChiが他家のPon・大明槓に負けるのは継続方策の正常な結果
        line, row = _line(9, **calls("chi", "CANCELLED-PRIORITY-CALL"))
        result = core.evaluate_line(line, row)
        self.assertEqual(result["invalid"], [])
        self.assertEqual(
            result["calls_after_branch"],
            [{"kind": "chi", "resolution": "CANCELLED-PRIORITY-CALL"}],
        )
        # Ponが優先鳴きに負けることは牌の枚数上ない
        line, row = _line(9, **calls("pon", "CANCELLED-PRIORITY-CALL"))
        self.assertTrue(core.evaluate_line(line, row)["invalid"])
        line, row = _line(9, **calls("chi", "CANCELLED-ROUND-END"))
        self.assertTrue(core.evaluate_line(line, row)["invalid"])

    def test_cancelled_candidate_without_a_ron_is_invalid(self) -> None:
        # K9は流局（ロンなし）なので、ロン取消の記録と矛盾する
        line, row = _line(9)
        line = {
            **line,
            "d": None,
            "selection": {**line["selection"], "resolution": CANCELLED_RON},
        }
        self.assertTrue(core.evaluate_line(line, row)["invalid"])


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
            plan["population"]["ordered_seeds"], list(range(553_000, 553_024))
        )
        self.assertEqual(plan["verify_replay_seeds"], [553_000, 553_023])
        self.assertEqual(plan["execution"]["timeout_seconds"], 600)
        self.assertEqual(plan["resource_limits"]["process_tree_rss_bytes"], 4 * 1024**3)
        self.assertEqual(plan["wrapper_version"], "pon-first-pass-branch-wrapper-v1")
        self.assertEqual(plan["call_analysis_version"], "call-value-shadow-v1")
        self.assertEqual(plan["criteria"]["allocation"]["budget_seconds"], 43_200)
        self.assertEqual(plan["participant"]["behavior_configuration"], {})
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal", name="r")
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["verify_replay_seeds"], [])
        self.assertIsNone(rehearsal["start_condition"])

    def test_drift_is_detected(self) -> None:
        plan = fx.plan_document(self.tmp)
        drifts = {
            "criteria": {**plan["criteria"], "order": []},
            "verify_replay_seeds": [553_001, 553_022],
            "protocol_version": "riichi-first-dama-branch-v1",
            "wrapper_version": "riichi-first-dama-branch-wrapper-v1",
            "call_analysis_version": "other",
            "execution": {**plan["execution"], "timeout_seconds": 900},
            "population": {
                **plan["population"],
                "ordered_seeds": list(range(553_001, 553_025)),
            },
        }
        for name, value in drifts.items():
            with self.subTest(name=name), self.assertRaises(EngineAabbProtocolError):
                plan_module.parse_plan({**plan, name: value})

    def test_start_condition_and_estimate_floor(self) -> None:
        values = plan_module.estimate_values(
            r_walls=[50.0, 50.0],
            d_walls=[20.0],
            branches=1,
            rehearsal_seat_hanchans=8,
            rehearsal_elapsed_seconds=1000.0,
        )
        # 1/8 < 155/96 なので n̂ = 155/96 × 96 = 155
        self.assertEqual(values["n_hat"], {"denominator": 1, "numerator": 155})
        slow = fx.time_estimate(d_wall=80.0)
        self.assertGreater(slow["projected_main_seconds"], 3 * 3600)
        with self.assertRaisesRegex(plan_module.PonFirstPassError, "NOT-STARTED"):
            fx.plan_document(self.tmp, name="slow", time_estimate=slow)

    def test_owner_rules(self) -> None:
        for owner in ("lisbun/lisjong-arena#553", "lisbun/lisjong-arena#100", "x"):
            with (
                self.subTest(owner=owner),
                self.assertRaises(plan_module.PonFirstPassError),
            ):
                fx.plan_document(self.tmp, owner_issue=owner)

    def test_seed_overlap_is_rejected(self) -> None:
        other = fx.v1.allocation(
            seeds=(553_010,),
            owner="lisbun/lisjong-arena#999",
            population="other",
            domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        )
        with self.assertRaises(plan_module.PonFirstPassError):
            fx.plan_document(self.tmp, ledger=fx.ledger(fx.RECORD, other))


# ---------------------------------------------------------------------------
# run / verify / compare / estimate
# ---------------------------------------------------------------------------


class _RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _ledger_path(self):
        path = self.tmp / "ledger.json"
        if not path.exists():
            seed_registry.write_ledger(path, fx.ledger())
        return path

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

    def verify(self, path, play_r=fx.fake_r_both, play_d=fx.fake_d_better):
        return core.verify_plan(
            path, environment=fx.ENVIRONMENT, play_r=play_r, play_d=play_d
        )


class RunTest(_RunCase):
    def test_design_possible_publishes_and_verifies_with_replay(self) -> None:
        path, plan, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_varied)
        self.assertEqual(summary["invalid"]["count"], 0)
        self.assertEqual(
            summary["population"],
            {
                "branches": 24,
                "candidates": 48,
                "ron_cancel_rate": {"denominator": 2, "numerator": 1},
                "ron_cancelled": 24,
            },
        )
        # fake playの時間は極小なので、配分は大きくSUPPORT-OK
        self.assertEqual(summary["support"]["verdict"], rules.SUPPORT_OK)
        self.assertEqual(summary["decision"], plan_module.DESIGN_POSSIBLE)
        self.assertTrue(summary["mean_difference_sizing"]["not_a_stop_condition"])
        self.assertGreater(summary["variance"]["sigma_u"], 0)
        output = Path(plan["artifact_destinations"]["output"])
        self.assertEqual(
            sorted(item.name for item in output.iterdir()),
            sorted(
                [
                    "SHA256SUMS",
                    "actions.jsonl",
                    "branches.jsonl",
                    "hanchans.json",
                    "summary.json",
                    "timings.json",
                ]
            ),
        )
        verified = self.verify(path, play_d=fx.fake_d_varied)
        self.assertEqual(verified["decision"], plan_module.DESIGN_POSSIBLE)
        self.assertEqual(verified["support"], rules.SUPPORT_OK)
        self.assertEqual(verified["rederived_call_analysis"], 48)
        self.assertEqual(verified["replayed"], {"arms": 2, "seeds": [553_000, 553_023]})

    def test_recalls_are_selected_and_established_separately(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_recall)
        self.assertEqual(summary["invalid"]["count"], 0)
        self.assertEqual(
            summary["recalls"],
            {
                "branches": 24,
                "branches_with_established_call": 24,
                "cancelled_calls": {"priority_call": 24, "ron": 24},
                "established_calls": 24,
                "established_kinds": {"chi": 24},
                "selected_calls": 72,
            },
        )

    def test_pre_branch_share_is_measured_and_aggregated(self) -> None:
        path, plan, summary = self.run_allocated(fx.fake_r_both, fx.fake_d_measured)
        pre = summary["pre_branch"]
        self.assertEqual(pre["d_arms"], 24)
        self.assertEqual(pre["d_arms_without_branch_point"], 0)
        self.assertEqual(pre["pre_branch_seconds"], 48.0)
        self.assertEqual(pre["arm_seconds"], 60.0)
        self.assertEqual(pre["share"], 0.8)
        # DESIGN-POSSIBLEなので提案の条件は満たさない
        self.assertFalse(pre["cost_reduction_may_be_proposed"])
        timings = core.read_document(
            Path(plan["artifact_destinations"]["output"]) / "timings.json", "timings"
        )
        d_rows = [row for row in timings["tasks"] if row["arm"] == "D"]
        self.assertTrue(all(row["pre_branch_seconds"] == 2.0 for row in d_rows))
        r_rows = [row for row in timings["tasks"] if row["arm"] == "R"]
        self.assertTrue(all(row["pre_branch_seconds"] is None for row in r_rows))
        # 測定値は記録本体（branches.jsonl）に入らず、再実行で値が違っても一致判定は通る
        lines = fx.read_lines(
            Path(plan["artifact_destinations"]["output"]) / "branches.jsonl"
        )
        self.assertTrue(
            all("measurement" not in (line["d"] or {}) for line in lines[1:])
        )
        verified = self.verify(path, play_d=fx.fake_d_better)
        self.assertEqual(verified["replayed"]["arms"], 2)

    def test_pre_branch_proposal_rule(self) -> None:
        def timings(pre, arm):
            return {
                "tasks": [
                    {"arm": "R", "arm_seconds": None, "pre_branch_seconds": None},
                    {"arm": "D", "arm_seconds": arm, "pre_branch_seconds": pre},
                    {"arm": "D", "arm_seconds": 5.0, "pre_branch_seconds": None},
                ]
            }

        high = core.pre_branch_summary(
            timings(7.0, 10.0), plan_module.NOT_VIABLE_SUPPORT
        )
        self.assertEqual(high["share"], 0.7)
        self.assertEqual(high["d_arms_without_branch_point"], 1)
        self.assertTrue(high["cost_reduction_may_be_proposed"])
        low = core.pre_branch_summary(
            timings(6.9, 10.0), plan_module.NOT_VIABLE_SUPPORT
        )
        self.assertFalse(low["cost_reduction_may_be_proposed"])
        other = core.pre_branch_summary(timings(9.0, 10.0), plan_module.DESIGN_POSSIBLE)
        self.assertFalse(other["cost_reduction_may_be_proposed"])

    def test_an_impossible_measurement_stops_the_run(self) -> None:
        with self.assertRaises(EngineAabbProtocolError):
            self.run_allocated(fx.fake_r_both, fx.fake_d_bad_measurement)

    def test_no_established_branch_is_not_viable_support(self) -> None:
        _, _, summary = self.run_allocated(fx.fake_r_cancelled_only, fx.fake_d_better)
        self.assertEqual(summary["decision"], plan_module.NOT_VIABLE_SUPPORT)
        self.assertEqual(summary["support"]["reason"], rules.NO_BRANCH)
        self.assertIsNone(summary["variance"]["sigma_u"])

    def test_invalid_cases_are_stop_invalid(self) -> None:
        for name, play_r, play_d in (
            ("priority", fx.fake_r_priority, fx.fake_d_better),
            ("not-passed", fx.fake_r_both, fx.fake_d_not_passed),
            ("count", fx.fake_r_both, fx.fake_d_count_mismatch),
        ):
            with self.subTest(case=name):
                _, _, summary = self.run_allocated(play_r, play_d, name=name)
                self.assertEqual(summary["decision"], plan_module.STOP_INVALID)
                self.assertIsNone(summary["support"])

    def test_failure_keeps_the_evidence_without_a_result(self) -> None:
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

    def test_field_mixing_is_rejected_by_the_strict_reader(self) -> None:
        path, plan, _ = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        output = Path(plan["artifact_destinations"]["output"])
        lines = fx.read_lines(output / "branches.jsonl")
        hanchans = core.read_document(output / "hanchans.json", "hanchans")
        mixed = [dict(line) for line in lines]
        mixed[1] = {
            **mixed[1],
            "decision_input": {**mixed[1]["decision_input"], "resolution": ESTABLISHED},
        }
        with self.assertRaisesRegex(plan_module.PonFirstPassError, "decision_input"):
            core.require_branch_lines(plan, mixed, hanchans)
        moved = [dict(line) for line in lines]
        moved[1] = {
            **moved[1],
            "selection": {**moved[1]["selection"], "context": None},
        }
        with self.assertRaisesRegex(plan_module.PonFirstPassError, "selection"):
            core.require_branch_lines(plan, moved, hanchans)

    def test_tampered_branch_and_replay_mismatch_are_detected(self) -> None:
        path, plan, _ = self.run_allocated(fx.fake_r_both, fx.fake_d_better)
        # 再実行する最大seed（553023）でD armの結果が変わる
        with self.assertRaisesRegex(plan_module.PonFirstPassError, "replayed D arm"):
            self.verify(path, play_d=fx.fake_d_varied)
        output = Path(plan["artifact_destinations"]["output"])
        lines = fx.read_lines(output / "branches.jsonl")
        lines[2]["d"]["pass_applied"] = False
        fx.write_lines(output / "branches.jsonl", lines)
        with self.assertRaises(plan_module.PonFirstPassError):
            self.verify(path)


class RehearsalTest(_RunCase):
    def _rehearse(self, play_d=fx.fake_d_better, unobserved=fx.fake_r_unobserved):
        observed, _ = fx.write_plan(self.tmp, run_kind="rehearsal", name="r")
        core.run_plan(observed, play_r=fx.fake_r_both, play_d=play_d, live_gate=fx.gate)
        other, _ = fx.write_plan(
            self.tmp, run_kind="rehearsal", observation="unobserved", name="r"
        )
        core.run_plan(other, play_r=unobserved, live_gate=fx.gate)
        return observed, other

    def test_match_and_estimate(self) -> None:
        observed, other = self._rehearse()
        compared = core.compare_rehearsals(
            observed,
            other,
            output=self.tmp / "compare.json",
            environment=fx.ENVIRONMENT,
        )
        self.assertTrue(compared["match"])
        self.assertEqual(compared["candidates"], 4)
        estimate = core.estimate_budget(
            observed,
            other,
            output=self.tmp / "estimate.json",
            environment=fx.ENVIRONMENT,
        )
        # 2分岐 / 8 seat-hanchans < 155/96 なので下限の率で見積もる
        self.assertEqual(estimate["n_hat"], {"denominator": 1, "numerator": 155})

    def test_d_arm_repeat_difference_fails(self) -> None:
        marker = self.tmp / "repeat-marker"
        os.environ["PON_FIRST_TEST_REPEAT_MARKER"] = str(marker)
        try:
            observed, other = self._rehearse(play_d=fx.fake_d_unstable)
        finally:
            del os.environ["PON_FIRST_TEST_REPEAT_MARKER"]
        with self.assertRaises(plan_module.PonFirstPassError):
            core.compare_rehearsals(
                observed,
                other,
                output=self.tmp / "compare.json",
                environment=fx.ENVIRONMENT,
            )


class CliTest(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        from lisjong_arena.heuristic_candidate_engine_aabb.__main__ import main

        for command in (
            "pon-first-plan",
            "pon-first-run",
            "pon-first-verify",
            "pon-first-rehearsal-compare",
            "pon-first-estimate",
        ):
            with (
                self.subTest(command=command),
                self.assertRaises(SystemExit) as raised,
            ):
                main([command, "--help"])
            self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
