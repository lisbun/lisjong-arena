"""#548 riichi-first-dama-source-v1（R1学習source）のplan・ブロック順・開始条件・run・verify・見積もり。

実engineの対局・Rust wheel・実Policyの半荘実行は使わない（#544のfake playを使う）。
"""

import json
import tempfile
import unittest
from pathlib import Path

import _engine_aabb_riichi_first_source_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_source as source,
)
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_source_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
)


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

    def run_block(self, play_d=fx.fake_d_better, **kwargs):
        path, plan = fx.write_plan(self.tmp, **kwargs)
        summary = source.run_plan(
            path,
            ledger_path=self._ledger_path(),
            play_r=fx.fake_r_both,
            play_d=play_d,
            live_gate=fx.gate,
        )
        return path, plan, summary


class PlanTest(_Case):
    def test_blocks_splits_and_verify_seeds(self) -> None:
        plan = fx.plan_document(self.tmp)
        self.assertEqual(plan["block"], "TRAIN-1")
        self.assertEqual(plan["split"], "TRAIN")
        self.assertEqual(
            plan["population"]["ordered_seeds"], list(range(547_000, 547_022))
        )
        self.assertEqual(plan["verify_replay_seeds"], [547_000, 547_021])
        self.assertIsNone(plan["artifact_identity"])
        self.assertEqual(plan_module.SPLIT_SEEDS["TEST"][0], 547_100)
        self.assertEqual(plan_module.SPLIT_SEEDS["TEST"][-1], 547_143)
        self.assertEqual(
            [plan_module.BLOCKS[name][1][0] for name in plan_module.BLOCK_ORDER],
            [547_000, 547_022, 547_100, 547_122],
        )
        rehearsal = fx.plan_document(self.tmp, run_kind="rehearsal", name="r")
        self.assertIsNone(rehearsal["block"])
        self.assertEqual(rehearsal["population"]["ordered_seeds"], [472_005, 472_006])
        self.assertEqual(rehearsal["verify_replay_seeds"], [])

    def test_test_plan_needs_the_frozen_artifact(self) -> None:
        estimate = fx.time_estimate()
        with self.assertRaisesRegex(EngineAabbProtocolError, "artifact_identity"):
            fx.plan_document(
                self.tmp, block="TEST-1", artifact_identity=None, time_estimate=estimate
            )
        with self.assertRaisesRegex(
            EngineAabbProtocolError, "TRAIN block has no artifact"
        ):
            fx.plan_document(self.tmp, artifact_identity="b" * 64)

    def test_drift_is_detected(self) -> None:
        plan = fx.plan_document(self.tmp)
        drifts = {
            "verify_replay_seeds": [547_001, 547_020],
            "block": "TRAIN-2",
            "split": "TEST",
            "artifact_identity": "c" * 64,
            "protocol_version": "riichi-first-dama-branch-v1",
            "execution_target": {**plan["execution_target"], "revision": "f" * 40},
        }
        for name, value in drifts.items():
            with self.subTest(name=name), self.assertRaises(EngineAabbProtocolError):
                plan_module.parse_plan({**plan, name: value})

    def test_owner_must_be_a_later_issue(self) -> None:
        for owner in ("lisbun/lisjong-arena#548", "x"):
            with self.subTest(owner=owner), self.assertRaises(EngineAabbProtocolError):
                fx.plan_document(self.tmp, owner_issue=owner)


class StartConditionTest(_Case):
    def test_session_and_collection_limits(self) -> None:
        estimate = fx.time_estimate()
        projected = estimate["projected_block_seconds"]
        self.assertLess(projected, plan_module.MAIN_BLOCK_SECONDS)
        session = plan_module.SESSION_LIMIT_SECONDS - projected
        plan = fx.plan_document(self.tmp, session_elapsed_seconds=session - 1.0)
        self.assertAlmostEqual(plan["resource_limits"]["wall_seconds"], projected + 1.0)
        with self.assertRaisesRegex(EngineAabbProtocolError, "NOT-STARTED"):
            fx.plan_document(
                self.tmp, name="late", session_elapsed_seconds=session + 1.0
            )
        collected = plan_module.COLLECTION_BUDGET_SECONDS - projected
        fx.plan_document(
            self.tmp, name="edge", collected_before_seconds=collected - 1.0
        )
        with self.assertRaisesRegex(EngineAabbProtocolError, "NOT-STARTED"):
            fx.plan_document(
                self.tmp, name="over", collected_before_seconds=collected + 1.0
            )

    def test_estimate_basis_and_block_order(self) -> None:
        _, first, _ = self.run_block(name="t1")
        record = plan_module.completed_block(first)
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
                collected_before_seconds=record["run_elapsed_seconds"] / 2,
            )
        second = fx.plan_document(
            self.tmp,
            block="TRAIN-2",
            name="ok",
            previous_blocks=[record],
            time_estimate=fx.block_estimate(first),
            collected_before_seconds=record["run_elapsed_seconds"] + 100.0,
        )
        self.assertEqual(second["start_condition"]["previous_blocks"], [record])


class RunTest(_Case):
    def test_block_output_contract_without_delta_statistics(self) -> None:
        path, plan, summary = self.run_block()
        self.assertEqual(summary["decision"], plan_module.SOURCE_COMPLETE)
        self.assertEqual(
            summary["schema"], "arena-riichi-first-dama-source-v1-summary-v1"
        )
        self.assertEqual((summary["block"], summary["split"]), ("TRAIN-1", "TRAIN"))
        self.assertEqual(summary["invalid"]["count"], 0)
        for name in ("variance", "cost_screening", "descriptive", "rows"):
            self.assertNotIn(name, summary)
        self.assertNotIn("delta", json.dumps(summary["quality"]))
        output = Path(plan["artifact_destinations"]["output"])
        header = fx.read_lines(output / "branches.jsonl")[0]
        self.assertEqual(
            header,
            {
                "allocation_identity": fx.TRAIN_RECORD["allocation_identity"],
                "artifact_identity": None,
                "block": "TRAIN-1",
                "features_version": "riichi-dama-features-v2",
                "kind": "header",
                "plan_digest": plan["plan_digest"],
                "protocol_version": "riichi-first-dama-source-v1",
                "schema": "arena-riichi-first-dama-source-v1-branches-v1",
                "split": "TRAIN",
                "wrapper_version": "riichi-first-dama-branch-wrapper-v1",
            },
        )
        verified = source.verify_plan(
            path,
            environment=fx.ENVIRONMENT,
            play_r=fx.fake_r_both,
            play_d=fx.fake_d_better,
        )
        self.assertEqual(verified["decision"], plan_module.SOURCE_COMPLETE)
        self.assertEqual(verified["replayed"]["seeds"], [547_000, 547_021])
        self.assertEqual(
            verified["schema"], "arena-riichi-first-dama-source-v1-verify-v1"
        )

    def test_test_block_header_binds_the_artifact(self) -> None:
        _, first, _ = self.run_block(name="t1")
        record = plan_module.completed_block(first)
        _, second, _ = self.run_block(
            name="t2",
            block="TRAIN-2",
            previous_blocks=[record],
            time_estimate=fx.block_estimate(first),
            collected_before_seconds=record["run_elapsed_seconds"],
        )
        records = [record, plan_module.completed_block(second)]
        _, test, summary = self.run_block(
            name="e1",
            block="TEST-1",
            previous_blocks=records,
            time_estimate=fx.block_estimate(second),
            collected_before_seconds=sum(
                item["run_elapsed_seconds"] for item in records
            ),
        )
        self.assertEqual(summary["split"], "TEST")
        header = fx.read_lines(
            Path(test["artifact_destinations"]["output"]) / "branches.jsonl"
        )[0]
        self.assertEqual(header["artifact_identity"], fx.ARTIFACT)
        self.assertEqual(
            header["allocation_identity"], fx.TEST_RECORD["allocation_identity"]
        )

    def test_invalid_branch_is_stop_invalid(self) -> None:
        _, _, summary = self.run_block(play_d=fx.fake_d_riichi_first_seed)
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
        self.assertEqual([row["seed"] for row in document["tasks"]], [547_000, 547_001])

    def test_estimate_from_the_previous_block(self) -> None:
        path, plan, _ = self.run_block(name="t1")
        estimate = source.estimate_from_block(path, output=self.tmp / "estimate.json")
        self.assertEqual(
            estimate["basis"], {"kind": "block", "plan_digests": [plan["plan_digest"]]}
        )
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
        compared = source.compare_rehearsals(
            observed, unobserved, environment=fx.ENVIRONMENT
        )
        self.assertTrue(compared["match"])
        estimate = source.estimate_from_rehearsal(
            observed, unobserved, output=self.tmp / "e.json", environment=fx.ENVIRONMENT
        )
        self.assertEqual(estimate["basis"]["kind"], "rehearsal")
        plan = fx.plan_document(self.tmp, name="main", time_estimate=estimate)
        self.assertEqual(plan["start_condition"]["verdict"], plan_module.WITHIN_BUDGET)


class LisjongReaderContractTest(_Case):
    """lisjong #360のreaderが、このprotocolのblockをそのまま読めること（pin更新後に実行）。"""

    def test_block_is_readable_by_lisjong(self) -> None:
        try:
            from lisjong.learning import r1_value
        except ImportError:
            self.skipTest("the pinned lisjong does not have the #360 reader yet")
        # lisjongは本番のseed範囲を検査するので、fixtureのseedはそのまま範囲内
        _, plan, _ = self.run_block()
        block = r1_value.read_source_block(plan["artifact_destinations"]["output"])
        self.assertEqual((block.block, block.split), ("TRAIN-1", "TRAIN"))
        self.assertEqual(len(block.rows), 2 * 22)


class CliTest(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        from lisjong_arena.heuristic_candidate_engine_aabb import __main__ as cli

        for command in (
            "riichi-first-source-plan",
            "riichi-first-source-run",
            "riichi-first-source-verify",
            "riichi-first-source-rehearsal-compare",
            "riichi-first-source-estimate",
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit) as stop:
                cli.main([command, "--help"])
            self.assertEqual(stop.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
