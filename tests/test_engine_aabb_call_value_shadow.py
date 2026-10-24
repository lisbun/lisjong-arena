"""#534 call-value-shadow-v1（#533）: 観測・plan・判定・strict verify。

実engineの半荘・Rust wheel・spawnした実Policyは使わない。lisjongの解析API
（#350）は実物を使い、保存したpublic contextから再導出できることを確かめる。
"""

import dataclasses
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import _engine_aabb_call_value_shadow_fixtures as fx
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DoraIndicatorRevealedEvidence,
    DrawEvidence,
    MeldCalledEvidence,
    ResponseOutcome,
    RiichiDeclaredEvidence,
    RoundEndedEvidence,
    RoundEndKind,
)
from lisjong_engine.round_result import AbortiveDrawReason
from lisjong_engine.win_context import WinMethod

from lisjong_arena.heuristic_candidate_engine_aabb import (
    call_value_shadow as cvs,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    call_value_shadow_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    call_value_shadow_worker as worker,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "call-value-shadow-534.md"
R1 = "r1_primary_below_threshold"
KUIKAE = "kuikae_primary_below_threshold"


def _line(name="hanchan", seed=1, calls=(), rounds=8):
    return {
        "calls": list(calls),
        "kind": "hanchan",
        "placement": 0,
        "rounds": rounds,
        "seats": [
            {
                "call_opportunities": fx.OPPORTUNITIES,
                "decisions": fx.DECISIONS,
                "seat": s,
            }
            for s in range(4)
        ],
        "seed": seed,
    }


# ---------------------------------------------------------------------------
# 公開evidenceによる成立/取消
# ---------------------------------------------------------------------------


class ResolveCallTest(unittest.TestCase):
    def setUp(self):
        self.pre, self.call, _, _ = fx.contexts(R1)
        self.order = fx.called_order(self.pre)

    def _resolve(self, evidence):
        return worker.resolve_call(self.call, self.order, evidence)

    def test_established_call_returns_the_public_meld(self):
        status, meld = self._resolve(fx.established(self.call, self.order))
        self.assertEqual(status, worker.ESTABLISHED)
        self.assertTrue(worker.meld_matches_call(meld, self.call))

    def test_deferred_evidence_and_riichi_declaration_are_skipped(self):
        evidence = list(fx.established(self.call, self.order))
        indicator = fx.engine_tile(self.call.called_tile)
        evidence.insert(
            3,
            DoraIndicatorRevealedEvidence(fx.engine_seat(self.call.target), indicator),
        )
        target = fx.engine_seat(self.call.target)
        evidence.insert(1, RiichiDeclaredEvidence(target, indicator, self.order))
        self.assertEqual(self._resolve(evidence)[0], worker.ESTABLISHED)

    def test_priority_call_by_another_seat_is_a_cancellation(self):
        other = fx.engine_seat(self.call.actor).next()
        meld = fx.engine_meld(self.call)
        evidence = fx.epoch(
            self.call,
            self.order,
            ResponseOutcome.CALL,
            MeldCalledEvidence(other, meld, self.order),
        )
        self.assertEqual(
            self._resolve(evidence), (worker.CANCELLED_PRIORITY_CALL, None)
        )

    def test_ron_is_a_cancellation(self):
        target = fx.engine_seat(self.call.target)
        ended = RoundEndedEvidence(
            kind=RoundEndKind.WIN,
            win_method=WinMethod.RON,
            winner_seats=(target.next(),),
            source_seat=target,
        )
        evidence = fx.epoch(self.call, self.order, ResponseOutcome.RON, ended)
        self.assertEqual(self._resolve(evidence), (worker.CANCELLED_RON, None))

    def test_round_end_after_no_public_response_is_a_cancellation(self):
        ended = RoundEndedEvidence(
            kind=RoundEndKind.ABORTIVE_DRAW,
            abortive_reason=AbortiveDrawReason.FOUR_RIICHI,
        )
        evidence = fx.epoch(
            self.call, self.order, ResponseOutcome.NO_PUBLIC_RESPONSE, ended
        )
        self.assertEqual(self._resolve(evidence), (worker.CANCELLED_ROUND_END, None))

    def test_unexplained_or_inconsistent_evidence_is_rejected(self):
        draw = DrawEvidence(fx.engine_seat(self.call.actor), DrawSource.LIVE_WALL)
        other_tile = fx.engine_tile(self.call.consumed_tiles[0])
        cases = {
            "vanished": fx.epoch(
                self.call, self.order, ResponseOutcome.NO_PUBLIC_RESPONSE, draw
            ),
            "missing discard": fx.established(self.call, self.order + 1),
            "other tile": fx.epoch(
                self.call,
                self.order,
                ResponseOutcome.CALL,
                MeldCalledEvidence(
                    fx.engine_seat(self.call.actor),
                    fx.engine_meld(self.call),
                    self.order,
                ),
                tile=other_tile,
            ),
            "other discarder": fx.epoch(
                self.call,
                self.order,
                ResponseOutcome.NO_PUBLIC_RESPONSE,
                discard_seat=fx.engine_seat(self.call.actor),
            ),
            "other meld": fx.epoch(
                self.call,
                self.order,
                ResponseOutcome.CALL,
                MeldCalledEvidence(
                    fx.engine_seat(self.call.actor), _pon_like(self.call), self.order
                ),
            ),
            "other discard order": fx.epoch(
                self.call,
                self.order,
                ResponseOutcome.CALL,
                MeldCalledEvidence(
                    fx.engine_seat(self.call.actor),
                    fx.engine_meld(self.call),
                    self.order + 5,
                ),
            ),
            "epoch not closed": fx.established(self.call, self.order)[:2],
        }
        for name, evidence in cases.items():
            with self.subTest(name):
                with self.assertRaises(worker.CallResolutionError):
                    self._resolve(evidence)


def _pon_like(call):
    """同じ牌を3枚持つmeld（chiのcallとは一致しない）。"""
    from lisjong_engine.public_state import PublicMeld, PublicMeldType

    tile = fx.engine_tile(call.called_tile)
    return PublicMeld(
        meld_type=PublicMeldType.PON,
        tiles=(tile, tile, tile),
        from_seat=fx.engine_seat(call.target),
        called_tile=tile,
    )


# ---------------------------------------------------------------------------
# 観測（call意図 -> 成立 -> 直後打牌）
# ---------------------------------------------------------------------------


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.pre, self.call, self.post, self.discard = fx.contexts(R1)
        self.order = fx.called_order(self.pre)

    def _observe(self, evidence, *, post=True, honba=None):
        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, self.call)
        if post:
            observer.observe(0, self.post, self.discard)
        observer.on_round_complete(fx.completion(self.pre, evidence, honba=honba))
        return observer.finish(1)

    def test_established_speed_call_matches_the_stored_record(self):
        result = self._observe(fx.established(self.call, self.order))
        self.assertEqual(result["calls"], [fx.FIXTURES[R1]])
        self.assertEqual(result["seats"][0]["decisions"], 2)
        self.assertEqual(result["seats"][0]["call_opportunities"], 1)
        self.assertEqual(result["rounds"], 1)

    def test_observed_champion_returns_the_champion_action(self):
        from lisjong.policies import PlacementAwareSpeedCallPolicy

        observer = worker.CallValueObserver()
        wrapped = worker._ObservedChampion(PlacementAwareSpeedCallPolicy(), 0, observer)
        for decision in (self.pre, self.post):
            self.assertEqual(
                wrapped.choose_action(decision),
                PlacementAwareSpeedCallPolicy().choose_action(decision),
            )
        self.assertEqual(observer.actions[0], [self.call, self.discard])

    def test_cancelled_call_has_no_post_and_does_not_take_the_next_discard(self):
        target = fx.engine_seat(self.call.target)
        ended = RoundEndedEvidence(
            kind=RoundEndKind.WIN,
            win_method=WinMethod.RON,
            winner_seats=(target.next(),),
            source_seat=target,
        )
        result = self._observe(
            fx.epoch(self.call, self.order, ResponseOutcome.RON, ended)
        )
        (call,) = result["calls"]
        self.assertEqual(call["resolution"]["status"], worker.CANCELLED_RON)
        self.assertIsNone(call["post"])
        self.assertEqual(call["invalid"], [])

    def test_invalid_correspondences(self):
        draw = DrawEvidence(fx.engine_seat(self.call.actor), DrawSource.LIVE_WALL)
        unexplained = fx.epoch(
            self.call, self.order, ResponseOutcome.NO_PUBLIC_RESPONSE, draw
        )
        cases = {
            "unexplained cancellation": self._observe(unexplained)["calls"][0],
            "no following decision": self._observe(
                fx.established(self.call, self.order), post=False
            )["calls"][0],
            "another round": self._observe(
                fx.established(self.call, self.order), honba=7
            )["calls"][0],
        }
        for name, call in cases.items():
            with self.subTest(name):
                self.assertTrue(call["invalid"])
        self.assertEqual(
            cases["unexplained cancellation"]["resolution"]["status"], "INVALID"
        )

    def test_pending_at_the_end_of_the_hanchan_is_invalid(self):
        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, self.call)
        observer.observe(0, self.post, self.discard)
        with self.assertRaises(worker.CallResolutionError):
            observer.finish(1)  # 局の終了が届いていない
        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, self.call)
        result = observer.finish(0)
        self.assertEqual(result["calls"][0]["resolution"]["status"], "INVALID")
        self.assertIn("pending", result["calls"][0]["resolution"]["detail"])

    def test_post_context_without_the_meld_is_invalid(self):
        players = list(self.post.input.players)
        players[0] = dataclasses.replace(players[0], melds=())
        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, self.call)
        stripped = dataclasses.replace(
            self.post,
            input=dataclasses.replace(self.post.input, players=tuple(players)),
        )
        observer.observe(0, stripped, self.discard)
        observer.on_round_complete(
            fx.completion(self.pre, fx.established(self.call, self.order))
        )
        call = observer.finish(1)["calls"][0]
        self.assertIn(
            "the post-call context does not hold the established meld", call["invalid"]
        )
        self.assertIsNone(call["post"])

    def test_wrong_post_discard_is_an_invalid_analysis(self):
        other = next(a for a in self.post.legal_actions if a != self.discard)
        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, self.call)
        observer.observe(0, self.post, other)
        observer.on_round_complete(
            fx.completion(self.pre, fx.established(self.call, self.order))
        )
        call = observer.finish(1)["calls"][0]
        self.assertTrue(any("post-call analysis" in item for item in call["invalid"]))

    def test_pass_is_an_opportunity_but_not_an_intent(self):
        from lisjong.policy_contract.action import PassAction

        observer = worker.CallValueObserver()
        observer.observe(0, self.pre, PassAction(actor=self.call.actor))
        self.assertEqual(observer.calls, [])
        self.assertEqual(observer.opportunities[0], 1)

    def test_rejected_call_analysis_is_recorded_as_invalid(self):
        from unittest import mock

        from lisjong.policies import speed_call_analysis

        def reject(decision, selected):
            raise speed_call_analysis.SpeedCallAnalysisError("synthetic rejection")

        api = mock.Mock(analyze_speed_call_decision=reject)
        observer = worker.CallValueObserver()
        with mock.patch.object(worker, "_analysis_api", return_value=api):
            observer.observe(0, self.pre, self.call)
        (call,) = observer.calls
        self.assertIsNone(call["call_analysis"])
        self.assertIn("synthetic rejection", call["invalid"][0])
        self.assertIsNotNone(call["pre_context"])

    def test_context_codec_round_trips_red_fives(self):
        pre, _, post, _ = fx.contexts("red_five_consistent")
        for decision in (pre, post):
            self.assertEqual(
                worker.decode_context(worker.encode_context(decision)), decision
            )
        reds = [
            action
            for action in post.legal_actions
            if getattr(action, "tile", None) is not None and action.tile.is_red
        ]
        self.assertTrue(reds)


# ---------------------------------------------------------------------------
# lisjongの値の読取・再導出
# ---------------------------------------------------------------------------


class PostCallRowTest(unittest.TestCase):
    def test_r1_and_kuikae_losses_are_exact_fractions(self):
        r1 = cvs.post_call_row(fx.record(R1), 1)["below_threshold"]
        self.assertEqual(
            Fraction(r1["loss"]["numerator"], r1["loss"]["denominator"]),
            Fraction(45980 - 40176, 45980),
        )
        self.assertEqual((r1["chosen_mass"], r1["alternative_mass"]), (45980, 40176))
        self.assertEqual(r1["change_kind"], "tile-type-change")
        self.assertEqual(r1["ukeire_difference"], 1)
        self.assertEqual(r1["value_difference"], 1)
        self.assertEqual(r1["proxy_difference"]["denominator"], 121 * 120 * 119)
        kuikae = cvs.post_call_row(fx.record(KUIKAE), 1)["below_threshold"]
        self.assertEqual(
            Fraction(kuikae["loss"]["numerator"], kuikae["loss"]["denominator"]),
            Fraction(320, 24576),
        )

    def test_value_mode_and_below_max_are_not_primary(self):
        value = cvs.post_call_row(fx.record("value_mode_below_threshold"), 1)
        self.assertFalse(value["primary_target"])
        self.assertEqual(value["classification"], cvs.PRIMARY_CLASSIFICATION)
        below_max = cvs.post_call_row(fx.record("closed_tenpai_below_max"), 1)
        self.assertNotIn("below_threshold", below_max)
        self.assertGreater(below_max["value_below_call"], 0)

    def test_inconsistent_lisjong_values_are_rejected(self):
        def tampered(change):
            value = fx.record(R1)
            change(value["post"]["analysis"])
            return value

        def tsumogiri(analysis):
            # 実打牌と同じ牌でflagだけ違う「代替」（他の整合はすべて満たすよう改ざん）
            chosen = next(
                item
                for item in analysis["candidates"]
                if item["action"] == analysis["discard"]
            )
            alternative = {**analysis["discard"], "tsumogiri": True}
            minimum = analysis["call_analysis"]["minimum_value"]
            analysis["candidates"].append(
                {**chosen, "action": alternative, "value": minimum}
            )
            analysis["alternative"] = alternative
            analysis["speed_loss"] = {
                "__type": "SpeedLoss",
                "mass_difference": 0,
                "relative_denominator": chosen["completion_mass"],
                "relative_numerator": 0,
                "zero_mass": False,
            }

        def over_denominator(analysis):
            analysis["candidates"][0]["completion_mass"] = (
                analysis["sequence_denominator"] + 1
            )

        def zero_mass_with_alternative(analysis):
            for item in analysis["candidates"]:
                if item["action"] == analysis["discard"]:
                    item["completion_mass"] = 0

        cases = {
            "loss": lambda a: a["speed_loss"].update(relative_numerator=1),
            "no alternative": lambda a: a.update(alternative=None),
            "tsumogiri-only": tsumogiri,
            "mass above D": over_denominator,
            "B = 0 < A": zero_mass_with_alternative,
            "D <= 0": lambda a: a.update(sequence_denominator=0),
        }
        for name, change in cases.items():
            with self.subTest(name):
                with self.assertRaises(cvs.CallValueShadowError):
                    cvs.post_call_row(tampered(change), 1)

    def test_rederive_from_stored_contexts(self):
        lines = [
            {"kind": "header"},
            _line(calls=fx.calls_for((*fx.PRIMARY, *fx.OTHERS))),
        ]
        # call解析9件 + post解析7件（priority cancelとparent decisionはpostなし）
        self.assertEqual(cvs.rederive_calls(lines), 9 + 7)

    def test_rederive_rejects_tampered_records(self):
        def mutate(change):
            call = fx.record(R1)
            change(call)
            return [{"kind": "header"}, _line(calls=[call])]

        def mass(call):
            call["post"]["analysis"]["candidates"][0]["completion_mass"] += 1

        def hand(call):
            tiles = call["pre_context"]["policy_input"]["own_hand"]["concealed_tiles"]
            tiles[0], tiles[-1] = tiles[-1], tiles[0]
            tiles[0] = dict(tiles[-2])

        def primary(call):
            call["is_primary_target"] = False

        def illegal(call):
            call["call"]["actor"] = 2

        for name, change in {
            "mass": mass,
            "hand": hand,
            "primary flag": primary,
            "illegal call": illegal,
        }.items():
            with self.subTest(name):
                with self.assertRaises(cvs.CallValueShadowError):
                    cvs.rederive_calls(mutate(change))


# ---------------------------------------------------------------------------
# 判定（整数比、境界値）
# ---------------------------------------------------------------------------


class GateTest(unittest.TestCase):
    criteria = plan_module.CRITERIA

    def _decide(self, changes, invalid=0):
        return cvs.gate_decision(self.criteria, invalid=invalid, changes=changes)[0]

    @staticmethod
    def _changes(losses, seeds=6):
        return [(index % seeds, Fraction(loss)) for index, loss in enumerate(losses)]

    def test_activation_boundaries(self):
        self.assertEqual(self._decide(self._changes([0] * 12)), plan_module.PROCEED)
        self.assertEqual(
            self._decide(self._changes([0] * 11)), plan_module.STOP_LOW_ACTIVATION
        )
        self.assertEqual(
            self._decide(self._changes([0] * 30, seeds=5)),
            plan_module.STOP_LOW_ACTIVATION,
        )
        self.assertEqual(self._decide([]), plan_module.STOP_LOW_ACTIVATION)

    def test_median_boundary_is_inclusive(self):
        at = ["1/5"] * 12
        self.assertEqual(self._decide(self._changes(at)), plan_module.PROCEED)
        above = ["1/5"] * 5 + ["1/5", "201/1000"] + ["201/1000"] * 5
        self.assertEqual(cvs.median([Fraction(x) for x in above]), Fraction(401, 2000))
        self.assertEqual(
            self._decide(self._changes(above)), plan_module.STOP_SPEED_TRADEOFF
        )

    def test_even_median_is_the_mean_of_the_middle_two(self):
        values = (
            [Fraction(0)] * 5
            + [Fraction(1, 10), Fraction(3, 10)]
            + [Fraction(1, 2)] * 5
        )
        self.assertEqual(cvs.median(values), Fraction(1, 5))
        self.assertEqual(
            self._decide(self._changes(values)), plan_module.STOP_SPEED_TRADEOFF
        )

    def test_p90_nearest_rank_boundary(self):
        # n = 12 -> ceil(10.8) = 11番目
        at = [Fraction(0)] * 10 + [Fraction(2, 5), Fraction(1)]
        self.assertEqual(cvs.p90(at), Fraction(2, 5))
        self.assertEqual(self._decide(self._changes(at)), plan_module.PROCEED)
        above = [Fraction(0)] * 10 + [Fraction(401, 1000), Fraction(1)]
        self.assertEqual(
            self._decide(self._changes(above)), plan_module.STOP_SPEED_TRADEOFF
        )

    def test_zero_mass_rows_count_as_zero_loss(self):
        self.assertEqual(self._decide(self._changes([0] * 12)), plan_module.PROCEED)

    def test_invalid_overrides_everything(self):
        self.assertEqual(
            self._decide(self._changes([0] * 12), invalid=1), plan_module.STOP_INVALID
        )

    def test_order_is_fixed(self):
        self.assertEqual(
            self.criteria["order"],
            [
                "STOP-INVALID",
                "STOP-INCOMPLETE",
                "STOP-LOW-ACTIVATION",
                "STOP-SPEED-TRADEOFF",
                "PROCEED-TO-CANDIDATE-DESIGN",
            ],
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_allocated_plan_binds_the_fixed_contract(self):
        plan = fx.plan_document(self.tmp)
        self.assertEqual(
            plan["population"]["ordered_seeds"], list(range(533_000, 533_024))
        )
        self.assertEqual(
            plan["population"]["allocation_protocol"], "call-value-shadow-v1"
        )
        self.assertEqual(plan["execution"]["timeout_seconds"], 600)
        self.assertEqual(plan["resource_limits"]["wall_seconds"], 4 * 3600)
        self.assertEqual(plan["resource_limits"]["process_tree_rss_bytes"], 4 * 1024**3)
        self.assertEqual(plan["criteria"]["seat_hanchan_denominator"], 96)
        self.assertEqual(
            plan["participant"]["policy_identity"], "placement-aware-speed-call"
        )
        self.assertEqual(plan["participant"]["behavior_configuration"], {})
        self.assertEqual(plan["rules"]["name"], "project-standard-v1")
        rehearsal = fx.plan_document(
            self.tmp, run_kind="rehearsal", observation="unobserved"
        )
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["resource_limits"]["wall_seconds"], 3600)

    def test_rejections(self):
        other_owner = fx.allocation(owner="lisbun/lisjong-arena#999")
        overlap = fx.allocation(
            seeds=(533_000,),
            population="other-population",
            domain="riichilab-other-domain-v1",
        )
        old_protocol = fx.allocation(
            protocol="arena-heuristic-candidate-engine-aabb-half-v1"
        )
        ledger_other = fx.ledger(other_owner)
        ledger_overlap = fx.ledger(fx.RECORD, overlap)
        ledger_protocol = fx.ledger(old_protocol)
        cases = {
            "allocated unobserved": dict(observation="unobserved"),
            "owner format": dict(owner_issue="#535"),
            "owner not after 534": dict(owner_issue="lisbun/lisjong-arena#534"),
            "rehearsal allocation": dict(run_kind="rehearsal"),
            "other owner": dict(
                ledger=ledger_other,
                allocation_binding=fx.binding(ledger_other, other_owner),
            ),
            "overlapping seed": dict(
                ledger=ledger_overlap, allocation_binding=fx.binding(ledger_overlap)
            ),
            "other protocol": dict(
                ledger=ledger_protocol,
                allocation_binding=fx.binding(ledger_protocol, old_protocol),
            ),
            "empty enforcement": dict(limit_enforcement=" "),
            "relative wheel": dict(wheel_path="wheel.whl"),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                run_kind = overrides.pop("run_kind", "allocated")
                if run_kind == "rehearsal":
                    overrides["allocation_binding"] = fx.binding(fx.ledger())
                with self.assertRaises(plan_module.CallValueShadowError):
                    fx.plan_document(self.tmp, run_kind=run_kind, **overrides)

    def test_parse_rejects_drift(self):
        plan = fx.plan_document(self.tmp)

        def mutated(change):
            value = json.loads(json.dumps(plan))
            change(value)
            return value

        cases = {
            "seeds": lambda p: p["population"]["ordered_seeds"].reverse(),
            "timeout": lambda p: p["execution"].update(timeout_seconds=601),
            "wall": lambda p: p["resource_limits"].update(wall_seconds=14401),
            "criteria": lambda p: p["criteria"].update(minimum_changes=11),
            "rules": lambda p: p["rules"].update(red_fives=False),
            "config": lambda p: p["participant"].update(
                behavior_configuration={"x": 1}
            ),
            "digest": lambda p: p.update(owner_issue="lisbun/lisjong-arena#536"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                with self.assertRaises(EngineAabbProtocolError):
                    plan_module.parse_plan(mutated(change))

    def test_save_is_write_once_and_live_gate_checks_head(self):
        plan = fx.plan_document(self.tmp)
        path = self.tmp / "plan.json"
        plan_module.save_plan(plan, path)
        with self.assertRaises(FileExistsError):
            plan_module.save_plan(plan, path)
        self.assertEqual(plan_module.load_plan(path), plan)
        fx.gate(plan, ledger=fx.ledger())
        with self.assertRaises(plan_module.CallValueShadowError):
            plan_module.require_live_target(
                plan, ledger=fx.ledger(), environment=fx.ENVIRONMENT, head="d" * 40
            )


# ---------------------------------------------------------------------------
# run / verify（fake play）
# ---------------------------------------------------------------------------


class RunVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, play, *, run_kind="allocated", observation="observed", name="run"):
        plan = fx.plan_document(
            self.tmp, run_kind=run_kind, observation=observation, name=name
        )
        path = self.tmp / f"{name}-{run_kind}-{observation}-plan.json"
        plan_module.save_plan(plan, path)
        summary = cvs.run_plan(
            path, ledger_path=self._ledger(), play=play, live_gate=fx.gate
        )
        return plan, path, summary

    def _ledger(self):
        path = self.tmp / "ledger.json"
        if not path.exists():
            from lisjong_arena._artifact_io import canonical_json_text

            path.write_text(
                canonical_json_text(fx.ledger()), encoding="utf-8", newline="\n"
            )
        return path

    def _verify(self, path, **kwargs):
        return cvs.verify_plan(path, environment=fx.ENVIRONMENT, **kwargs)

    def test_proceed_run_publishes_and_verifies(self):
        plan, path, summary = self._run(fx.fake_proceed)
        activation = summary["activation"]
        self.assertEqual(summary["gate"]["decision"], plan_module.PROCEED)
        self.assertEqual(activation["primary_changes"], 12)
        self.assertEqual(activation["seeds_with_primary_change"], 6)
        self.assertEqual(activation["seat_hanchans"], 96)
        self.assertEqual(
            activation["primary_changes_per_seat_hanchan"],
            {"denominator": 96, "numerator": 12},
        )
        self.assertEqual(activation["decisions"], 24 * 4 * fx.DECISIONS)
        self.assertEqual(activation["cancelled"]["CANCELLED-PRIORITY-CALL"], 1)
        self.assertEqual(activation["call_intents"], 12 + len(fx.OTHERS))
        self.assertEqual(summary["primary_loss"]["count"], 12)
        median = Fraction(320, 24576) / 2 + Fraction(5804, 45980) / 2
        self.assertEqual(
            summary["primary_loss"]["median"],
            {"denominator": median.denominator, "numerator": median.numerator},
        )
        secondary = summary["secondary"][
            "established_speed_calls_by_mode_target_classification"
        ]
        self.assertIn(
            "GameSituationMode.ALL_LAST_LAST_VALUE|primary=False|"
            "PostCallClassification.BELOW_THRESHOLD",
            secondary,
        )
        output = Path(plan["artifact_destinations"]["output"])
        self.assertEqual(
            sorted(item.name for item in output.iterdir()),
            [
                "SHA256SUMS",
                "hanchans.json",
                "shadow.jsonl",
                "summary.json",
                "timings.json",
            ],
        )
        verified = self._verify(path, output=self.tmp / "verify.json")
        self.assertEqual(verified["decision"], plan_module.PROCEED)
        # 6 seeds × 主対象2件 × (call + post) + seed 0のその他7件（post付き5件）
        self.assertEqual(verified["rederived_lisjong_analyses"], 6 * 2 * 2 + 7 + 5)
        with self.assertRaises(FileExistsError):
            self._verify(path, output=self.tmp / "verify.json")
        with self.assertRaises(EngineAabbProtocolError):
            cvs.run_plan(
                path,
                ledger_path=self._ledger(),
                play=fx.fake_proceed,
                live_gate=fx.gate,
            )

    def test_separate_process_verify(self):
        _, path, summary = self._run(fx.fake_proceed)
        script = (
            "import json, sys; sys.path.insert(0, sys.argv[2]);"
            "import _engine_aabb_call_value_shadow_fixtures as fx;"
            "from lisjong_arena.heuristic_candidate_engine_aabb import call_value_shadow as c;"
            "print(json.dumps(c.verify_plan(sys.argv[1], environment=fx.ENVIRONMENT)))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path), str(Path(__file__).parent)],
            capture_output=True,
            text=True,
            check=True,
        )
        verified = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(verified["summary_digest"], summary["summary_digest"])

    def test_low_activation_and_invalid(self):
        _, _, low = self._run(fx.fake_low_seeds, name="low")
        self.assertEqual(low["gate"]["decision"], plan_module.STOP_LOW_ACTIVATION)
        self.assertEqual(low["activation"]["seeds_with_primary_change"], 5)
        _, path, invalid = self._run(fx.fake_invalid, name="invalid")
        self.assertEqual(invalid["gate"]["decision"], plan_module.STOP_INVALID)
        self.assertEqual(invalid["activation"]["invalid_calls"], 1)
        self.assertEqual(self._verify(path)["decision"], plan_module.STOP_INVALID)

    def test_partial_run_is_not_published(self):
        plan = fx.plan_document(self.tmp, name="stop")
        path = self.tmp / "stop-plan.json"
        plan_module.save_plan(plan, path)
        with self.assertRaises(RunStopped):
            cvs.run_plan(
                path, ledger_path=self._ledger(), play=fx.fake_stop, live_gate=fx.gate
            )
        self.assertFalse(Path(plan["artifact_destinations"]["output"]).exists())
        (stop,) = Path(plan["artifact_destinations"]["diagnostics"]).iterdir()
        document = json.loads(stop.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "STOP-INCOMPLETE")
        self.assertEqual(document["schema"], plan_module.STOP_SCHEMA)

    def test_verify_rejects_tampering(self):
        plan, path, _ = self._run(fx.fake_proceed)
        output = Path(plan["artifact_destinations"]["output"])
        shadow = output / "shadow.jsonl"
        original = shadow.read_bytes()
        sums = output / "SHA256SUMS"
        original_sums = sums.read_bytes()

        def rewrite(lines, *, fix_sums):
            shadow.write_bytes(cvs.shadow_text(lines).encode("utf-8"))
            if fix_sums:
                sums.write_bytes(cvs._sums_text(output).encode("utf-8"))

        def restore():
            shadow.write_bytes(original)
            sums.write_bytes(original_sums)

        lines = cvs.read_shadow(shadow)
        mutations = {
            "hash only": (
                lambda ls: ls[1]["seats"][0].update(call_opportunities=5),
                False,
            ),
            "missing hanchan": (lambda ls: ls.pop(3), True),
            "duplicate hanchan": (lambda ls: ls.insert(3, ls[2]), True),
            "reordered": (lambda ls: ls.insert(1, ls.pop(2)), True),
            "decisions": (lambda ls: ls[1]["seats"][0].update(decisions=1), True),
            "duplicate post": (
                lambda ls: ls[1]["calls"][0]["post"].update(
                    decision_ordinal=ls[1]["calls"][1]["post"]["decision_ordinal"]
                ),
                True,
            ),
            "analysis mass": (
                lambda ls: ls[1]["calls"][0]["post"]["analysis"]["candidates"][
                    0
                ].update(completion_mass=0),
                True,
            ),
            "stored context": (
                lambda ls: ls[2]["calls"][0]["pre_context"]["policy_input"][
                    "round"
                ].update(
                    honba=ls[2]["calls"][0]["pre_context"]["policy_input"]["round"][
                        "honba"
                    ]
                    + 1
                ),
                True,
            ),
        }
        for name, (change, fix_sums) in mutations.items():
            with self.subTest(name):
                copied = json.loads(json.dumps(lines))
                change(copied)
                rewrite(copied, fix_sums=fix_sums)
                with self.assertRaises(plan_module.CallValueShadowError):
                    self._verify(path)
                restore()
        self._verify(path)
        extra = output / "extra.json"
        extra.write_text("{}\n", encoding="utf-8", newline="\n")
        with self.assertRaises(plan_module.CallValueShadowError):
            self._verify(path)
        extra.unlink()
        (output / "summary.json").unlink()
        with self.assertRaises(plan_module.CallValueShadowError):
            self._verify(path)

    def test_verify_requires_the_plan_analysis_environment(self):
        _, path, _ = self._run(fx.fake_proceed)
        other = json.loads(json.dumps(fx.ENVIRONMENT))
        other["native"]["wheel_sha256"] = "0" * 64
        with self.assertRaises(plan_module.CallValueShadowError):
            cvs.verify_plan(path, environment=other)


class RehearsalCompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _plan(self, observation, play, name):
        plan = fx.plan_document(
            self.tmp, run_kind="rehearsal", observation=observation, name=name
        )
        path = self.tmp / f"{name}-{observation}.json"
        plan_module.save_plan(plan, path)
        summary = cvs.run_plan(path, ledger_path=None, play=play, live_gate=fx.gate)
        self.assertIsNone(summary["gate"])
        self.assertEqual(summary["status"], cvs.REHEARSAL_STATUS)
        return path

    def test_matching_rehearsals(self):
        observed = self._plan("observed", fx.fake_rehearsal_observed, "a")
        unobserved = self._plan("unobserved", fx.fake_rehearsal_unobserved, "a")
        result = cvs.compare_rehearsals(
            observed,
            unobserved,
            output=self.tmp / "compare.json",
            environment=fx.ENVIRONMENT,
        )
        self.assertTrue(result["match"])
        self.assertEqual([item["seed"] for item in result["seeds"]], [472_005, 472_006])

    def test_action_difference_is_invalid(self):
        observed = self._plan("observed", fx.fake_rehearsal_observed, "b")
        unobserved = self._plan("unobserved", fx.fake_rehearsal_unobserved_other, "b")
        with self.assertRaises(plan_module.CallValueShadowError):
            cvs.compare_rehearsals(
                observed,
                unobserved,
                output=self.tmp / "compare-b.json",
                environment=fx.ENVIRONMENT,
            )
        document = json.loads((self.tmp / "compare-b.json").read_text(encoding="utf-8"))
        self.assertFalse(document["match"])
        self.assertEqual(
            document["seeds"][1]["differences"], ["action_sequence_sha256"]
        )
        with self.assertRaises(plan_module.CallValueShadowError):
            cvs.compare_rehearsals(observed, observed, environment=fx.ENVIRONMENT)


class DocumentationTest(unittest.TestCase):
    def test_docs_and_cli_name_the_fixed_contract(self):
        from lisjong_arena.heuristic_candidate_engine_aabb import __main__ as cli

        text = DOC.read_text(encoding="utf-8")
        for command in (
            "call-value-plan",
            "call-value-run",
            "call-value-verify",
            "call-value-rehearsal-compare",
        ):
            self.assertIn(command, text)
            self.assertIn(command, cli.__doc__)
        for value in (
            "533000..533023",
            "472005",
            "STOP-SPEED-TRADEOFF",
            "PROCEED-TO-CANDIDATE-DESIGN",
            "1/5",
            "2/5",
            plan_module.PLAN_SCHEMA,
            cvs.SHADOW_SCHEMA,
            plan_module.ALLOCATION_PROTOCOL,
        ):
            self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
