"""#556 pon-first-pass-source-v1（P1学習source）のplan・ブロック順・開始条件・run・verify・見積もり。

実engineの対局・Rust wheel・実Policyの半荘実行は使わない（#553のfake playを使う）。観測・照合・
field分離・測定値の再利用部分のregressionはpilotのtest（test_engine_aabb_pon_first_pass）で担保し、
ここではsourceの差分だけを確かめる。
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_pon_first_source_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_allocation as allocation_rules,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_source as source,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    pon_first_pass_source_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)

TRAIN_1 = tuple(range(556_000, 556_040))


def previous_records(*blocks, artifact=fx.ARTIFACT):
    """先行ブロックの完了記録（開始条件の入力。TEST blockを直接作るため）。"""
    return [
        {
            "artifact_identity": artifact if name.startswith("TEST") else None,
            "block": name,
            "plan_digest": f"{index + 1:x}" * 64,
            "run_elapsed_seconds": 3000.0,
            "summary_digest": "d" * 64,
            "verify_digest": "e" * 64,
            "verify_elapsed_seconds": 300.0,
        }
        for index, name in enumerate(blocks)
    ]


def block_estimate_after(records):
    return fx.time_estimate(
        basis={"kind": "block", "plan_digests": [records[-1]["plan_digest"]]}
    )


def _keys(value):
    if type(value) is dict:
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif type(value) is list:
        for item in value:
            yield from _keys(item)


class _Case(unittest.TestCase):
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

    def run_block(self, play_d=fx.fake_d_better, play_r=fx.fake_r_both, **kwargs):
        path, plan = fx.write_plan(self.tmp, **kwargs)
        summary = source.run_plan(
            path,
            ledger_path=self._ledger_path(),
            play_r=play_r,
            play_d=play_d,
            live_gate=fx.gate,
        )
        return path, plan, summary

    def verify(self, path, play_d=fx.fake_d_better, **kwargs):
        return source.verify_plan(
            path,
            environment=fx.ENVIRONMENT,
            play_r=fx.fake_r_both,
            play_d=play_d,
            **kwargs,
        )

    def completed(self, **kwargs):
        """runとstrict verifyを済ませたブロック（次のplanの先行ブロック）。"""
        path, plan, _ = self.run_block(**kwargs)
        self.verify(path)
        return path, plan, plan_module.completed_block(plan)


class PlanTest(_Case):
    def test_six_blocks_splits_and_verify_seeds(self) -> None:
        self.assertEqual(
            plan_module.BLOCK_ORDER,
            ("TRAIN-1", "TRAIN-2", "TRAIN-3", "TEST-1", "TEST-2", "TEST-3"),
        )
        self.assertEqual(
            {
                name: (split, seeds[0], seeds[-1], len(seeds))
                for name, (split, seeds) in plan_module.BLOCKS.items()
            },
            {
                "TRAIN-1": ("TRAIN", 556_000, 556_039, 40),
                "TRAIN-2": ("TRAIN", 556_040, 556_079, 40),
                "TRAIN-3": ("TRAIN", 556_080, 556_119, 40),
                "TEST-1": ("TEST", 556_200, 556_239, 40),
                "TEST-2": ("TEST", 556_240, 556_279, 40),
                "TEST-3": ("TEST", 556_280, 556_319, 40),
            },
        )
        self.assertEqual(
            plan_module.SPLIT_SEEDS["TRAIN"], tuple(range(556_000, 556_120))
        )
        self.assertEqual(
            plan_module.SPLIT_SEEDS["TEST"], tuple(range(556_200, 556_320))
        )
        for name in plan_module.BLOCK_ORDER:
            seeds = plan_module.BLOCKS[name][1]
            self.assertEqual(
                plan_module.replay_seeds(name, "allocated"), [seeds[0], seeds[-1]]
            )
        plan = fx.plan_document(self.tmp)
        self.assertEqual((plan["block"], plan["split"]), ("TRAIN-1", "TRAIN"))
        self.assertEqual(plan["population"]["ordered_seeds"], list(TRAIN_1))
        self.assertEqual(plan["population"]["k"], 40)
        self.assertEqual(plan["verify_replay_seeds"], [556_000, 556_039])
        self.assertIsNone(plan["artifact_identity"])
        self.assertEqual(
            plan["population"]["allocation_binding"]["allocation_identity"],
            fx.TRAIN_RECORD["allocation_identity"],
        )
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal", name="r")
        self.assertIsNone(rehearsal["block"])
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["verify_replay_seeds"], [])

    def test_test_plan_needs_the_frozen_artifact(self) -> None:
        records = previous_records("TRAIN-1", "TRAIN-2", "TRAIN-3")
        estimate = fx.time_estimate(
            basis={"kind": "block", "plan_digests": [records[-1]["plan_digest"]]}
        )
        arguments = {
            "block": "TEST-1",
            "previous_blocks": records,
            "time_estimate": estimate,
            "collected_before_seconds": 9900.0,
        }
        with self.assertRaisesRegex(EngineAabbProtocolError, "artifact_identity"):
            fx.plan_document(self.tmp, artifact_identity=None, **arguments)
        plan = fx.plan_document(self.tmp, name="t", **arguments)
        self.assertEqual(
            (plan["split"], plan["artifact_identity"]), ("TEST", fx.ARTIFACT)
        )
        self.assertEqual(
            plan["population"]["allocation_binding"]["allocation_identity"],
            fx.TEST_RECORD["allocation_identity"],
        )
        with self.assertRaisesRegex(
            EngineAabbProtocolError, "TRAIN block has no artifact"
        ):
            fx.plan_document(self.tmp, artifact_identity="b" * 64)

    def test_split_allocation_is_required(self) -> None:
        document = fx.ledger()
        with self.assertRaises(EngineAabbProtocolError):
            fx.plan_document(
                self.tmp,
                ledger=document,
                allocation_binding=fx.binding(document, fx.TEST_RECORD),
            )

    def test_drift_is_detected(self) -> None:
        plan = fx.plan_document(self.tmp)
        drifts = {
            "verify_replay_seeds": [556_001, 556_038],
            "block": "TRAIN-2",
            "split": "TEST",
            "artifact_identity": "c" * 64,
            "protocol_version": "pon-first-pass-branch-v1",
            "consumer_issue": "lisbun/lisjong#360",
            "execution_target": {**plan["execution_target"], "revision": "f" * 40},
        }
        for name, value in drifts.items():
            with self.subTest(name=name), self.assertRaises(EngineAabbProtocolError):
                plan_module.parse_plan({**plan, name: value})
        population = {**plan["population"], "ordered_seeds": list(TRAIN_1[:20])}
        with self.assertRaises(EngineAabbProtocolError):
            plan_module.parse_plan({**plan, "population": population})

    def test_owner_must_be_a_later_issue(self) -> None:
        for owner in ("lisbun/lisjong-arena#556", "x"):
            with self.subTest(owner=owner), self.assertRaises(EngineAabbProtocolError):
                fx.plan_document(self.tmp, owner_issue=owner)


class StartConditionTest(_Case):
    def test_estimate_uses_the_pilot_branch_rate_floor(self) -> None:
        estimate = fx.time_estimate(branches=2)
        self.assertEqual(
            estimate["branches_per_seed"], {"denominator": 6, "numerator": 29}
        )
        cost = 50.0 + 29 / 6 * 20.0
        self.assertAlmostEqual(estimate["cost_per_seed_seconds"], cost)
        self.assertAlmostEqual(estimate["projected_block_seconds"], 42 * cost + 900)
        higher = fx.time_estimate(branches=16)
        self.assertEqual(
            higher["branches_per_seed"], {"denominator": 1, "numerator": 8}
        )
        tampered = dict(estimate, projected_block_seconds=1.0)
        with self.assertRaisesRegex(EngineAabbProtocolError, "re-derive"):
            plan_module.parse_time_estimate(tampered)

    def test_block_limit_of_three_hours(self) -> None:
        # c = 210 + 29/6 x 2 > (10800 - 900) / 42 なので、session 0でも3時間を超える
        estimate = fx.time_estimate(r_wall=230.0, d_wall=2.0)
        self.assertGreater(
            estimate["projected_block_seconds"], allocation_rules.BLOCK_LIMIT_SECONDS
        )
        self.assertLess(
            estimate["projected_block_seconds"],
            allocation_rules.SESSION_LIMIT_SECONDS,
        )
        with self.assertRaisesRegex(EngineAabbProtocolError, "NOT-STARTED"):
            fx.plan_document(
                self.tmp, time_estimate=estimate, session_elapsed_seconds=0.0
            )

    def test_session_limit_of_four_hours(self) -> None:
        estimate = fx.time_estimate()
        projected = estimate["projected_block_seconds"]
        self.assertLess(projected, allocation_rules.BLOCK_LIMIT_SECONDS)
        session = allocation_rules.SESSION_LIMIT_SECONDS - projected
        plan = fx.plan_document(self.tmp, session_elapsed_seconds=session - 1.0)
        self.assertAlmostEqual(plan["resource_limits"]["wall_seconds"], projected + 1.0)
        self.assertEqual(plan["population"]["k"], 40)
        with self.assertRaisesRegex(EngineAabbProtocolError, "NOT-STARTED"):
            fx.plan_document(
                self.tmp, name="late", session_elapsed_seconds=session + 1.0
            )

    def test_cumulative_collection_of_fourteen_hours(self) -> None:
        self.assertEqual(plan_module.COLLECTION_BUDGET_SECONDS, 50_400)
        projected = fx.time_estimate()["projected_block_seconds"]
        collected = plan_module.COLLECTION_BUDGET_SECONDS - projected
        plan = fx.plan_document(
            self.tmp, name="edge", collected_before_seconds=collected - 1.0
        )
        self.assertEqual(plan["start_condition"]["verdict"], plan_module.WITHIN_BUDGET)
        with self.assertRaisesRegex(EngineAabbProtocolError, "14 h collection"):
            fx.plan_document(
                self.tmp, name="over", collected_before_seconds=collected + 1.0
            )
        # NOT-STARTED-BUDGETでもseed数を縮めた判定はしない（ブロックは常に40 seeds）
        condition = plan_module.start_condition(
            block="TRAIN-1",
            estimate=fx.time_estimate(),
            session_elapsed_seconds=0.0,
            collected_before_seconds=collected + 1.0,
            previous_blocks=[],
        )
        self.assertEqual(condition["verdict"], plan_module.NOT_STARTED_BUDGET)
        self.assertEqual(condition["block_condition"]["block_seeds"], 40)

    def test_run_limit_includes_the_collection_remainder(self) -> None:
        # 累積11時間45分の後: 残り2時間15分。新しいsession（経過0）でも上限は3時間ではない
        collected = 11 * 3600 + 45 * 60.0
        projected = fx.time_estimate()["projected_block_seconds"]
        self.assertLess(projected, 2 * 3600 + 15 * 60)
        plan = fx.plan_document(
            self.tmp, session_elapsed_seconds=0.0, collected_before_seconds=collected
        )
        condition = plan["start_condition"]
        self.assertEqual(condition["block_condition"]["limit_seconds"], 10_800.0)
        self.assertEqual(condition["collection_remaining_seconds"], 8_100.0)
        self.assertEqual(condition["limit_seconds"], 8_100.0)
        self.assertEqual(plan["resource_limits"]["wall_seconds"], 8_100.0)
        # session側が小さければそちら
        late = fx.plan_document(
            self.tmp,
            name="late",
            session_elapsed_seconds=14_400.0 - 7_900.0,
            collected_before_seconds=collected,
        )
        self.assertEqual(late["resource_limits"]["wall_seconds"], 7_900.0)
        drifted = {
            **plan,
            "resource_limits": {**plan["resource_limits"], "wall_seconds": 10_800.0},
        }
        with self.assertRaises(EngineAabbProtocolError):
            plan_module.parse_plan(drifted)

    def test_estimate_basis_and_block_order(self) -> None:
        _, first, record = self.completed(name="t1")
        with self.assertRaisesRegex(EngineAabbProtocolError, "rehearsal estimate"):
            fx.plan_document(self.tmp, name="x", time_estimate=fx.block_estimate(first))
        with self.assertRaisesRegex(EngineAabbProtocolError, "completed blocks"):
            fx.plan_document(
                self.tmp,
                block="TRAIN-2",
                name="y",
                time_estimate=fx.block_estimate(first),
            )
        with self.assertRaisesRegex(
            EngineAabbProtocolError, "immediately previous block"
        ):
            fx.plan_document(
                self.tmp,
                block="TRAIN-2",
                name="z",
                previous_blocks=[record],
                collected_before_seconds=record["run_elapsed_seconds"],
            )
        with self.assertRaisesRegex(
            EngineAabbProtocolError, "smaller than the recorded"
        ):
            fx.plan_document(
                self.tmp,
                block="TRAIN-2",
                name="w",
                previous_blocks=[record],
                time_estimate=fx.block_estimate(first),
                collected_before_seconds=record["run_elapsed_seconds"],
            )
        second = fx.plan_document(
            self.tmp,
            block="TRAIN-2",
            name="ok",
            previous_blocks=[record],
            time_estimate=fx.block_estimate(first),
            collected_before_seconds=record["run_elapsed_seconds"]
            + record["verify_elapsed_seconds"],
        )
        self.assertEqual(second["start_condition"]["previous_blocks"], [record])

    def test_previous_plans_are_bound_to_their_outputs(self) -> None:
        path, first, _ = self.completed(name="t1")
        environment = first["environment_digest"]
        target = first["execution_target"]
        records = plan_module.previous_block_records(
            [path], environment_digest=environment, execution_target=target
        )
        self.assertEqual(records, [plan_module.completed_block(first)])
        with self.assertRaisesRegex(EngineAabbProtocolError, "another environment"):
            plan_module.previous_block_records(
                [path], environment_digest="0" * 64, execution_target=target
            )
        output = Path(first["artifact_destinations"]["output"])
        (output / "summary.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(EngineAabbProtocolError, "SHA256SUMS"):
            plan_module.completed_block(first)


class PreviousBlockTest(_Case):
    def test_next_plan_needs_the_strict_verify_record(self) -> None:
        path, first, _ = self.run_block(name="t1")
        with self.assertRaisesRegex(
            EngineAabbProtocolError, "no strict verify success record"
        ):
            plan_module.completed_block(first)
        record = self.verify(path)
        self.assertEqual(record["plan_digest"], first["plan_digest"])
        self.assertEqual(record["replayed"]["seeds"], [556_000, 556_039])
        self.assertEqual(
            record["wall_seconds"], first["resource_limits"]["wall_seconds"]
        )
        stored = plan_module.verify_record_path(first)
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8")), record)
        self.assertEqual(
            plan_module.completed_block(first)["verify_digest"],
            record["verify_digest"],
        )
        # verifyは1回だけ（結果を見て再実行しない）
        with self.assertRaisesRegex(EngineAabbProtocolError, "already been started"):
            self.verify(path)
        with self.assertRaisesRegex(EngineAabbProtocolError, "plan diagnostics"):
            source.verify_plan(path, output=self.tmp / "v.json")

    def test_a_verify_record_of_another_plan_is_rejected(self) -> None:
        _, first, _ = self.run_block(name="t1")
        other_path, other, _ = self.run_block(name="t1-other")
        self.verify(other_path)
        destination = plan_module.verify_record_path(first)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(plan_module.verify_record_path(other), destination)
        with self.assertRaisesRegex(EngineAabbProtocolError, "another plan"):
            plan_module.completed_block(first)

    def test_a_replay_mismatch_stops_the_next_plan(self) -> None:
        path, first, _ = self.run_block(name="t1")
        with self.assertRaisesRegex(EngineAabbProtocolError, "STOP-INVALID"):
            self.verify(path, play_d=fx.fake_d_worse)
        diagnostics = Path(first["artifact_destinations"]["diagnostics"])
        failed = json.loads(
            (diagnostics / plan_module.VERIFY_FAILED_FILENAME).read_text()
        )
        self.assertEqual(failed["schema"], plan_module.VERIFY_FAILED_SCHEMA)
        self.assertFalse((diagnostics / plan_module.VERIFY_FILENAME).exists())
        with self.assertRaisesRegex(EngineAabbProtocolError, "verify failed"):
            plan_module.completed_block(first)
        with self.assertRaisesRegex(EngineAabbProtocolError, "already been started"):
            self.verify(path)

    def test_run_and_verify_over_the_plan_limit_fail(self) -> None:
        path, first, _ = self.run_block(name="t1")
        ticks = iter([0.0, 1e6, 1e6, 1e6])
        with self.assertRaisesRegex(EngineAabbProtocolError, "exceeded the plan limit"):
            self.verify(path, clock=lambda: next(ticks), realtime=lambda: 0.0)
        with self.assertRaisesRegex(EngineAabbProtocolError, "verify failed"):
            plan_module.completed_block(first)

    def test_test_blocks_share_the_frozen_artifact(self) -> None:
        records = previous_records("TRAIN-1", "TRAIN-2", "TRAIN-3", "TEST-1")
        arguments = {
            "block": "TEST-2",
            "previous_blocks": records,
            "time_estimate": block_estimate_after(records),
            "collected_before_seconds": 13_200.0,
        }
        plan = fx.plan_document(self.tmp, name="same", **arguments)
        self.assertEqual(plan["artifact_identity"], fx.ARTIFACT)
        with self.assertRaisesRegex(EngineAabbProtocolError, "frozen artifact"):
            fx.plan_document(
                self.tmp, name="other", artifact_identity="b" * 64, **arguments
            )
        tampered = previous_records("TRAIN-1", "TRAIN-2", "TRAIN-3")
        tampered[0]["artifact_identity"] = fx.ARTIFACT
        with self.assertRaisesRegex(EngineAabbProtocolError, "TRAIN block"):
            fx.plan_document(
                self.tmp,
                name="train",
                block="TEST-1",
                previous_blocks=tampered,
                time_estimate=block_estimate_after(tampered),
                collected_before_seconds=9_900.0,
            )

    def test_completed_test_block_record_carries_the_artifact(self) -> None:
        records = previous_records("TRAIN-1", "TRAIN-2", "TRAIN-3")
        _, plan, record = self.completed(
            name="e1",
            block="TEST-1",
            previous_blocks=records,
            time_estimate=block_estimate_after(records),
            collected_before_seconds=9_900.0,
        )
        self.assertEqual(record["artifact_identity"], fx.ARTIFACT)
        chained = [*records, record]
        later = {
            "block": "TEST-2",
            "previous_blocks": chained,
            "time_estimate": fx.block_estimate(plan),
            "collected_before_seconds": 9_900.0
            + record["run_elapsed_seconds"]
            + record["verify_elapsed_seconds"],
        }
        fx.plan_document(self.tmp, name="a-a", **later)
        with self.assertRaisesRegex(EngineAabbProtocolError, "frozen artifact"):
            fx.plan_document(self.tmp, name="a-b", artifact_identity="b" * 64, **later)


class RunTest(_Case):
    def test_block_output_contract_without_delta_statistics(self) -> None:
        path, plan, summary = self.run_block()
        self.assertEqual(summary["decision"], plan_module.SOURCE_COMPLETE)
        self.assertEqual(summary["schema"], "arena-pon-first-pass-source-v1-summary-v1")
        self.assertEqual((summary["block"], summary["split"]), ("TRAIN-1", "TRAIN"))
        self.assertEqual(summary["invalid"]["count"], 0)
        self.assertEqual(
            summary["population"],
            {
                "branches": 40,
                "candidates": 80,
                "ron_cancel_rate": {"denominator": 2, "numerator": 1},
                "ron_cancelled": 40,
            },
        )
        self.assertEqual(summary["pre_branch"]["share"], 0.75)
        for name in (
            "variance",
            "support",
            "descriptive",
            "rows",
            "mean_difference_sizing",
            "breakdown",
        ):
            self.assertNotIn(name, summary)
        self.assertEqual(
            [key for key in _keys(summary) if "delta" in key], ["no_delta_statistic"]
        )
        self.assertNotIn("cost_reduction_may_be_proposed", summary["pre_branch"])
        output = Path(plan["artifact_destinations"]["output"])
        expected = {
            "allocation_identity": fx.TRAIN_RECORD["allocation_identity"],
            "artifact_identity": None,
            "block": "TRAIN-1",
            "call_analysis_version": "call-value-shadow-v1",
            "kind": "header",
            "plan_digest": plan["plan_digest"],
            "protocol_version": "pon-first-pass-source-v1",
            "split": "TRAIN",
            "wrapper_version": "pon-first-pass-branch-wrapper-v1",
        }
        for name in ("branches", "actions"):
            header = fx.read_lines(output / f"{name}.jsonl")[0]
            self.assertEqual(
                header,
                {**expected, "schema": f"arena-pon-first-pass-source-v1-{name}-v1"},
            )
        verified = source.verify_plan(
            path,
            environment=fx.ENVIRONMENT,
            play_r=fx.fake_r_both,
            play_d=fx.fake_d_better,
        )
        self.assertEqual(verified["decision"], plan_module.SOURCE_COMPLETE)
        self.assertEqual(verified["replayed"]["seeds"], [556_000, 556_039])
        self.assertEqual(verified["replayed"]["arms"], 2)
        self.assertEqual(verified["schema"], "arena-pon-first-pass-source-v1-verify-v1")

    def test_test_block_header_binds_the_artifact(self) -> None:
        records = previous_records("TRAIN-1", "TRAIN-2", "TRAIN-3")
        _, plan, summary = self.run_block(
            name="e1",
            block="TEST-1",
            previous_blocks=records,
            time_estimate=block_estimate_after(records),
            collected_before_seconds=9900.0,
        )
        self.assertEqual(summary["decision"], plan_module.SOURCE_COMPLETE)
        self.assertEqual(summary["split"], "TEST")
        header = fx.read_lines(
            Path(plan["artifact_destinations"]["output"]) / "branches.jsonl"
        )[0]
        self.assertEqual(header["artifact_identity"], fx.ARTIFACT)
        self.assertEqual(
            header["allocation_identity"], fx.TEST_RECORD["allocation_identity"]
        )

    def test_invalid_candidate_is_stop_invalid(self) -> None:
        _, _, summary = self.run_block(play_d=fx.fake_d_not_passed)
        self.assertEqual(summary["decision"], plan_module.STOP_INVALID)
        self.assertEqual(summary["invalid"]["count"], 1)

    def test_failure_keeps_a_stop_record_without_output(self) -> None:
        path, plan = fx.write_plan(self.tmp)
        with self.assertRaises(RunStopped):
            source.run_plan(
                path,
                ledger_path=self._ledger_path(),
                play_r=fx.fake_r_failure,
                play_d=fx.fake_d_better,
                live_gate=fx.gate,
            )
        self.assertFalse(Path(plan["artifact_destinations"]["output"]).exists())
        (stop,) = list(Path(plan["artifact_destinations"]["diagnostics"]).iterdir())
        document = json.loads(stop.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], plan_module.STOP_SCHEMA)
        self.assertEqual(document["decision"], plan_module.STOP_INCOMPLETE)
        self.assertEqual([row["seed"] for row in document["tasks"]], [556_000, 556_001])

    def test_estimate_from_the_previous_block(self) -> None:
        path, plan, _ = self.completed(name="t1")
        estimate = source.estimate_from_block(path, output=self.tmp / "estimate.json")
        self.assertEqual(
            estimate["basis"], {"kind": "block", "plan_digests": [plan["plan_digest"]]}
        )
        self.assertEqual((estimate["branches"], estimate["seat_hanchans"]), (40, 160))
        self.assertEqual(
            plan_module.load_time_estimate(self.tmp / "estimate.json"), estimate
        )


class RehearsalTest(_Case):
    def test_rehearsal_compare_and_estimate(self) -> None:
        observed, _ = fx.write_plan(
            self.tmp, run_kind="rehearsal", observation="observed"
        )
        unobserved, _ = fx.write_plan(
            self.tmp, run_kind="rehearsal", observation="unobserved"
        )
        for plan, play in (
            (observed, fx.fake_r_both),
            (unobserved, fx.fake_r_unobserved),
        ):
            summary = source.run_plan(
                plan, play_r=play, play_d=fx.fake_d_better, live_gate=fx.gate
            )
            self.assertIsNone(summary["decision"])
            self.assertEqual(summary["schema"], plan_module.SCHEMAS["summary"])
        compared = source.compare_rehearsals(
            observed, unobserved, environment=fx.ENVIRONMENT
        )
        self.assertTrue(compared["match"])
        self.assertEqual(compared["schema"], plan_module.SCHEMAS["compare"])
        with self.assertRaisesRegex(EngineAabbProtocolError, "pilot estimate"):
            pon_first_pass.estimate_budget(
                observed, unobserved, environment=fx.ENVIRONMENT
            )
        estimate = source.estimate_from_rehearsal(
            observed, unobserved, output=self.tmp / "e.json", environment=fx.ENVIRONMENT
        )
        self.assertEqual(estimate["basis"]["kind"], "rehearsal")
        plan = fx.plan_document(self.tmp, name="main", time_estimate=estimate)
        self.assertEqual(plan["start_condition"]["verdict"], plan_module.WITHIN_BUDGET)


class LisjongReaderContractTest(_Case):
    """pinしたlisjong #366のreaderが、このprotocolのblockをそのまま読めること（契約の整合）。"""

    def test_block_is_readable_by_lisjong(self) -> None:
        from lisjong.learning import p1_value

        _, plan, _ = self.run_block()
        block = p1_value.read_source_block(plan["artifact_destinations"]["output"])
        self.assertEqual((block.block, block.split), ("TRAIN-1", "TRAIN"))
        self.assertEqual(len(block.rows), 40)
        self.assertEqual(block.ron_cancelled, 40)
        self.assertEqual(
            block.allocation_identity, fx.TRAIN_RECORD["allocation_identity"]
        )
        self.assertEqual(p1_value.BLOCKS, plan_module.BLOCKS)
        self.assertEqual(p1_value.SPLIT_SEEDS, plan_module.SPLIT_SEEDS)
        self.assertEqual(p1_value.BLOCK_SEEDS, plan_module.BLOCK_SEEDS)
        self.assertEqual(p1_value.SOURCE_PROTOCOL, plan_module.PROTOCOL_VERSION)
        self.assertEqual(p1_value.BRANCHES_SCHEMA, plan_module.SCHEMAS["branches"])
        self.assertEqual(p1_value.SUMMARY_SCHEMA, plan_module.SCHEMAS["summary"])
        self.assertEqual(p1_value.SOURCE_COMPLETE, plan_module.SOURCE_COMPLETE)


class PilotEntryTest(_Case):
    """sourceのallocated planを、pilotの入口からverify seedの再実行へ渡さない。"""

    def _counted(self):
        calls = {"execute": 0, "verified_plan": 0}
        execute, verified = pon_first_pass._execute, pon_first_pass.verified_plan

        def counting_execute(*args, **kwargs):
            calls["execute"] += 1
            return execute(*args, **kwargs)

        def counting_verified(*args, **kwargs):
            calls["verified_plan"] += 1
            return verified(*args, **kwargs)

        patches = contextlib.ExitStack()
        patches.enter_context(
            mock.patch.object(pon_first_pass, "_execute", counting_execute)
        )
        patches.enter_context(
            mock.patch.object(pon_first_pass, "verified_plan", counting_verified)
        )
        return calls, patches

    def test_pilot_commands_reject_a_source_plan_before_replay(self) -> None:
        from lisjong_arena.heuristic_candidate_engine_aabb import __main__ as cli

        path, plan, _ = self.run_block(name="t1")
        diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
        commands = (
            ["pon-first-verify", "--plan", str(path)],
            ["pon-first-run", "--plan", str(path)],
            [
                "pon-first-rehearsal-compare",
                "--observed",
                str(path),
                "--unobserved",
                str(path),
                "--output",
                str(self.tmp / "c.json"),
            ],
            [
                "pon-first-estimate",
                "--observed",
                str(path),
                "--unobserved",
                str(path),
                "--output",
                str(self.tmp / "e.json"),
            ],
        )
        calls, patches = self._counted()
        with patches:
            for command in commands:
                with self.subTest(command=command[0]):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(cli.main(command), 2)
                    self.assertIn("pon-first-pass-branch-v1 plans", stderr.getvalue())
        self.assertEqual(calls, {"execute": 0, "verified_plan": 0})
        self.assertFalse((diagnostics / plan_module.VERIFY_START_FILENAME).exists())
        self.assertFalse((self.tmp / "c.json").exists())
        # 正規のsourceのverifyは開始記録を取って1回だけ行える
        self.assertEqual(self.verify(path)["decision"], plan_module.SOURCE_COMPLETE)

    def test_compare_rejects_an_allocated_plan_before_replay(self) -> None:
        path, _, _ = self.run_block(name="t1")
        calls, patches = self._counted()
        for compare in (source.compare_rehearsals, pon_first_pass.compare_rehearsals):
            with self.subTest(compare=compare.__module__), patches:
                with self.assertRaisesRegex(EngineAabbProtocolError, "rehearsal plan"):
                    compare(path, path, environment=fx.ENVIRONMENT)
        self.assertEqual(calls, {"execute": 0, "verified_plan": 0})
        with self.assertRaisesRegex(EngineAabbProtocolError, "rehearsal plan"):
            source.estimate_from_rehearsal(path, path, environment=fx.ENVIRONMENT)


class CliTest(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        from lisjong_arena.heuristic_candidate_engine_aabb import __main__ as cli

        for command in (
            "pon-first-source-plan",
            "pon-first-source-run",
            "pon-first-source-verify",
            "pon-first-source-rehearsal-compare",
            "pon-first-source-estimate",
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit) as stop:
                cli.main([command, "--help"])
            self.assertEqual(stop.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
