"""#591 リーチ下1向聴の押し引き候補の半荘開発比較（plan・stage・集計・verify、合成値）。

実engine・実Policyの代わりに、spawn workerで#513のfixture半荘を返す``fake_play*``を使う。
Kは500の代わりに3、stageは10 blockの代わりに2 blockへ差し替える（module定数をpatch。
workerはこれらを参照しない）。合成値であり、強さの結果ではない。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _engine_aabb_protocol_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_response_hanchan as rr,
)
from lisjong_arena.heuristic_candidate_engine_aabb.development import DEVELOPMENT_SEEDS
from lisjong_arena.heuristic_candidate_engine_aabb.errors import EngineAabbRecordError
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN

SEED_C = 514_003
SEEDS = (fx.SEED_A, fx.SEED_B, SEED_C)


def allocation(*, seeds=SEEDS, split=None, owner=None, population=None):
    return seed_registry.build_allocation_record(
        owner_issue=owner or rr.OWNER_ISSUE,
        protocol="arena-heuristic-candidate-engine-aabb-half-v1",
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population=population or rr.POPULATION,
        split=split or rr.SPLIT,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-11-15T00:00:00Z",
    )


def ledger_for(record):
    document = seed_registry.new_ledger()
    document["allocations"] = [record]
    return seed_registry.validate_ledger(document)


def measurement_plan(**overrides):
    record = overrides.pop("record", None) or allocation()
    document = ledger_for(record)
    arguments = {
        "mode": rr.MODE_MEASUREMENT,
        "ordered_seeds": SEEDS,
        "allocation_binding": seed_registry.allocation_binding(
            document, record["allocation_identity"]
        ),
        "timeout_seconds": fx.TIMEOUT_SECONDS,
        "wheel_path": fx.WHEEL_PATH,
        "environment": fx.ENVIRONMENT,
        "arena_revision": fx.ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return rr.assemble_plan(**arguments), document


def rehearsal_plan():
    return rr.assemble_plan(
        mode=rr.MODE_REHEARSAL,
        ordered_seeds=DEVELOPMENT_SEEDS,
        allocation_binding=None,
        timeout_seconds=fx.TIMEOUT_SECONDS,
        wheel_path=fx.WHEEL_PATH,
        environment=fx.ENVIRONMENT,
        arena_revision=fx.ARENA_REVISION,
    )


class _Case(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("MEASUREMENT_K", len(SEEDS)),
            ("MEASUREMENT_STAGE_BLOCKS", 2),
        ):
            patcher = mock.patch.object(rr, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "run"
        self.root.mkdir()

    def save(self, plan):
        return rr.save_plan(plan, self.tmp / "plan.json")

    def run_stage(self, plan_path, stage, document=None, **overrides):
        ledger_path = self.tmp / "ledger.json"
        if document is not None and not ledger_path.exists():
            ledger_path.write_text(canonical_json_text(document), encoding="utf-8")
        arguments = {
            "ledger_path": ledger_path,
            "play": fx.fake_play,
            "environment": fx.ENVIRONMENT,
            "head": fx.ARENA_REVISION,
        }
        arguments.update(overrides)
        return rr.run_stage(plan_path, stage, self.root, **arguments)


class PlanTest(_Case):
    def test_plan_binds_fixed_pair_allocation_and_rule(self):
        plan, _ = measurement_plan()
        self.assertEqual(plan["evaluation_kind"], "DEVELOPMENT")
        self.assertEqual(
            [plan["participants"][role]["policy_identity"] for role in ("A", "B")],
            [rr.CANDIDATE_IDENTITY, "placement-aware-speed-call"],
        )
        self.assertEqual(plan["decision_rule"], rr.DECISION_RULE)
        self.assertEqual(plan["ordered_seeds"], list(SEEDS))
        self.assertEqual(rr.stage_count(plan), 2)
        self.assertEqual(rr.stage_seeds(plan, 0), SEEDS[:2])
        self.assertEqual(rr.stage_seeds(plan, 1), SEEDS[2:])
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.stage_seeds(plan, 2)

    def test_allocation_must_be_the_591_development_allocation(self):
        for override in (
            {"split": "FORMAL-EVAL"},
            {"owner": "lisbun/lisjong-arena#1"},
            {"population": "heuristic-candidate-engine-aabb-other"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(rr.RiichiResponseHanchanError):
                    measurement_plan(record=allocation(**override))

    def test_seed_count_and_membership_are_fixed(self):
        with self.assertRaises(rr.RiichiResponseHanchanError):
            measurement_plan(ordered_seeds=SEEDS[:2])
        with self.assertRaises(EngineAabbRecordError):
            measurement_plan(ordered_seeds=(fx.SEED_A, fx.SEED_B, 999_999))

    def test_rehearsal_uses_known_development_seeds_without_allocation(self):
        plan = rehearsal_plan()
        self.assertIsNone(plan["allocation_binding"])
        self.assertEqual(plan["ordered_seeds"], list(DEVELOPMENT_SEEDS))
        self.assertEqual(rr.stage_count(plan), 2)
        record = allocation()
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.assemble_plan(
                mode=rr.MODE_REHEARSAL,
                ordered_seeds=DEVELOPMENT_SEEDS,
                allocation_binding=seed_registry.allocation_binding(
                    ledger_for(record), record["allocation_identity"]
                ),
                timeout_seconds=fx.TIMEOUT_SECONDS,
                wheel_path=fx.WHEEL_PATH,
                environment=fx.ENVIRONMENT,
                arena_revision=fx.ARENA_REVISION,
            )

    def test_tampered_plan_is_rejected(self):
        plan, _ = measurement_plan()
        for key, value in (
            ("decision_rule", {**rr.DECISION_RULE, rr.SUPPORTED: "mean D > 0"}),
            ("ordered_seeds", list(reversed(SEEDS))),
            ("purpose", "formal result"),
            ("mode", rr.MODE_REHEARSAL),
        ):
            with self.subTest(key=key):
                with self.assertRaises(EngineAabbRecordError):
                    rr.parse_plan({**plan, key: value})
        swapped = {
            **plan,
            "participants": {
                "A": plan["participants"]["B"],
                "B": plan["participants"]["A"],
            },
        }
        with self.assertRaises(EngineAabbRecordError):
            rr.parse_plan(swapped)

    def test_live_target_must_match_the_plan(self):
        plan, document = measurement_plan()
        rr.require_live_target(
            plan, ledger=document, environment=fx.ENVIRONMENT, head=fx.ARENA_REVISION
        )
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.require_live_target(
                plan, ledger=document, environment=fx.ENVIRONMENT, head="f" * 40
            )
        other = {**fx.ENVIRONMENT, "python": {"implementation": "X", "version": "0"}}
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.require_live_target(
                plan, ledger=document, environment=other, head=fx.ARENA_REVISION
            )
        retired = seed_registry.new_ledger()
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.require_live_target(
                plan,
                ledger=seed_registry.validate_ledger(retired),
                environment=fx.ENVIRONMENT,
                head=fx.ARENA_REVISION,
            )


class RunAndSummaryTest(_Case):
    def test_stages_run_in_any_order_and_summary_is_rederived(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        # stageは独立に実行できる（順不同でも集計はplanの順序になる）
        second = self.run_stage(path, 1, document)
        first = self.run_stage(path, 0, document)
        self.assertEqual(first["seeds"], list(SEEDS[:2]))
        self.assertEqual(second["seeds"], list(SEEDS[2:]))
        summary = rr.write_summary(path, self.root)
        self.assertEqual(summary["hanchan_count"], 18)
        self.assertEqual([block["seed"] for block in summary["blocks"]], list(SEEDS))
        self.assertTrue(summary["not_a_formal_result"])
        statistics = summary["statistics"]
        self.assertEqual(statistics["k"], 3)
        self.assertNotIn("classification", statistics)
        self.assertEqual(summary["status"], rr.verdict_of(statistics))
        self.assertIn("secondary_metrics", summary)
        self.assertEqual(rr.verify_summary(path, self.root), summary)
        with self.assertRaises(FileExistsError):
            rr.write_summary(path, self.root)

    def test_missing_stage_blocks_the_summary(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        self.run_stage(path, 0, document)
        with self.assertRaises(rr.RiichiResponseHanchanError):
            rr.write_summary(path, self.root)
        self.assertFalse((self.root / rr.SUMMARY_FILENAME).exists())

    def test_stage_is_write_once(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        self.run_stage(path, 0, document)
        with self.assertRaises(FileExistsError):
            self.run_stage(path, 0, document)

    def test_failed_hanchan_writes_nothing(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        with self.assertRaises(RunStopped):
            self.run_stage(path, 0, document, play=fx.fake_play_policy_error)
        self.assertFalse(rr.stage_directory(self.root, 0).exists())
        with self.assertRaises(RunStopped):
            self.run_stage(path, 0, document, play=fx.fake_play_other_environment)
        self.assertFalse(rr.stage_directory(self.root, 0).exists())

    def test_stage_requires_the_live_target(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        with self.assertRaises(rr.RiichiResponseHanchanError):
            self.run_stage(path, 0, document, head="f" * 40)
        self.assertFalse(rr.stage_directory(self.root, 0).exists())

    def test_tampered_or_swapped_stage_is_rejected(self):
        plan, document = measurement_plan()
        path = self.save(plan)
        self.run_stage(path, 0, document)
        self.run_stage(path, 1, document)
        marker_path = rr.stage_directory(self.root, 1) / rr.STAGE_FILENAME
        original = marker_path.read_text(encoding="utf-8")
        marker = json.loads(original)
        marker_path.write_text(
            canonical_json_text({**marker, "raw_manifest_sha256": "0" * 64}),
            encoding="utf-8",
        )
        with self.assertRaises(EngineAabbRecordError):
            rr.build_summary(plan, self.root)
        marker_path.write_text(original, encoding="utf-8")
        rr.build_summary(plan, self.root)
        # stage 1のdirectoryをstage 0の位置へ置くと、scheduleとmarkerが一致しない
        zero, one = (rr.stage_directory(self.root, index) for index in (0, 1))
        shutil.rmtree(zero)
        shutil.copytree(one, zero)
        with self.assertRaises(EngineAabbRecordError):
            rr.build_summary(plan, self.root)

    def test_rehearsal_reports_no_statistics_or_verdict(self):
        plan = rehearsal_plan()
        path = self.save(plan)
        for stage in range(rr.stage_count(plan)):
            self.run_stage(path, stage)
        summary = rr.write_summary(path, self.root)
        self.assertIsNone(summary["statistics"])
        self.assertEqual(summary["status"], rr.REHEARSAL_STATUS)
        self.assertEqual(summary["hanchan_count"], 12)
        self.assertEqual(rr.verify_summary(path, self.root), summary)


class VerdictRuleTest(unittest.TestCase):
    def test_rule_is_the_preregistered_one(self):
        cases = (
            ({"mean_d": 0.5, "lower": 0.1}, rr.SUPPORTED),
            ({"mean_d": 0.5, "lower": 0.0}, rr.INCONCLUSIVE),
            ({"mean_d": 0.5, "lower": -0.2}, rr.INCONCLUSIVE),
            ({"mean_d": 0.0, "lower": -0.2}, rr.NOT_SUPPORTED),
            ({"mean_d": -0.3, "lower": -0.9}, rr.NOT_SUPPORTED),
        )
        for statistics, expected in cases:
            with self.subTest(statistics=statistics):
                self.assertEqual(rr.verdict_of(statistics), expected)


if __name__ == "__main__":
    unittest.main()
