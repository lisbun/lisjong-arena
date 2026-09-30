"""#436 admission, event dispatch and evidence; no live seed/game execution."""

from __future__ import annotations

import copy
import multiprocessing
import os
import unittest
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import test_heuristic_candidate_423 as previous
import test_heuristic_candidate_aabb as fixtures
from lisjong.policies import OneShantenDefensePlacementAwareSpeedCallPolicy

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena._parallel_execution import check_policy_spec_serializable
from lisjong_arena.comparison import aggregate_policy_metrics
from lisjong_arena.heuristic_candidate_aabb import (
    experiment,
    lock,
    result,
    rust423,
    rust436,
)
from lisjong_arena.heuristic_candidate_aabb.__main__ import _spec_from_binding
from lisjong_arena.heuristic_candidate_aabb.protocol import PROTOCOL_ID, SEED_DOMAIN
from lisjong_arena.model import ComparisonPlan, ComparisonResult
from lisjong_arena.overall_champion_aabb.protocol import ParticipantBinding
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
)
from lisjong_arena.single_round_artifact import execution_provenance_to_dict


def participant(index):
    identity, factory = rust436.PAIR[index]
    return ParticipantBinding(
        family="heuristic",
        policy_identity=identity,
        factory_binding=f"lisjong_arena.policy_catalog:{factory}",
        implementation_source="lisjong",
        implementation_revision=rust436.REVISION,
    )


def provenance():
    return replace(previous.provenance(), lisjong_revision=rust436.REVISION)


def process(pid=100):
    record = previous.process(pid)
    record["backend"]["lisjong_revision"] = rust436.REVISION
    record["backend"]["native"].update(source_revision=rust436.REVISION, api_version=3)
    record["wheel"]["sha256"] = rust436.WHEEL_SHA256
    record["provenance"] = execution_provenance_to_dict(provenance())
    return record


def synthetic(plan):
    rows = tuple(
        replace(
            row,
            policy_identity=rust436.PAIR[
                0 if row.policy_identity == fixtures.CANDIDATE_IDENTITY else 1
            ][0],
        )
        for row in fixtures.seat_rows(lambda seed: fixtures.CANDIDATE_ADVANTAGE)
        if row.seed in plan.seeds
    )
    return ComparisonResult(
        plan=plan,
        seat_results=rows,
        metrics_a=aggregate_policy_metrics(plan.policy_a.identity, rows),
        metrics_b=aggregate_policy_metrics(plan.policy_b.identity, rows),
    )


def evidence(document, comparison):
    return {
        "contract": document["rust_execution"],
        "parent": process(),
        "blocks": [
            {
                "seed": seed,
                "process": process(200 + index % 2),
                "game_native_calls": 10,
                "rows_identity": rust423._rows_identity(
                    row for row in comparison.seat_results if row.seed == seed
                ),
            }
            for index, seed in enumerate(comparison.plan.seeds)
        ],
    }


def spawned_block(plan, wheel, provenance_value):
    with (
        mock.patch.object(rust436, "verify_process", return_value=process(os.getpid())),
        mock.patch.object(
            rust423, "verify_process", side_effect=AssertionError("old event")
        ),
        mock.patch.object(rust423, "run_comparison", side_effect=synthetic),
        mock.patch.object(rust423, "native_call_count", side_effect=(20, 30)),
    ):
        return rust423._run_block(plan, wheel, provenance_value, 436)


class Event436Tests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger, allocation = reserve_allocation(
            new_ledger(),
            owner_issue="lisbun/lisjong-arena#436",
            protocol=PROTOCOL_ID,
            seed_domain=SEED_DOMAIN,
            purpose="synthetic test only",
            population="heuristic-candidate-aabb-436",
            split="FORMAL-EVAL",
            seeds=fixtures.SEEDS,
            arena_revision=fixtures.ARENA_REVISION,
            protocol_revision=PROTOCOL_ID,
            provenance_reference="fixture",
            allocation_timestamp="2026-10-01T00:00:00Z",
        )
        self.binding = allocation_binding(
            self.ledger, allocation["allocation_identity"]
        )
        self.plan = ComparisonPlan(
            policy_a=_spec_from_binding(participant(0)),
            policy_b=_spec_from_binding(participant(1)),
            seeds=fixtures.SEEDS,
            game_mode="4p-red-half",
            max_steps=10000,
        )
        self.comparison = synthetic(self.plan)

    def build(self, **overrides):
        arguments = dict(
            destinations={
                "comparison_artifact": self.root / "comparison.json",
                "candidate_result": self.root / "result.json",
            },
            candidate=participant(0),
            incumbent=participant(1),
            seeds=fixtures.SEEDS,
            max_workers=2,
            seed_ledger=self.ledger,
            allocation_binding=self.binding,
            event=436,
            wheel_path=self.root / rust436.WHEEL_FILENAME,
        )
        arguments.update(overrides)
        with (
            fixtures.merged_main_execution(provenance()),
            mock.patch.object(rust436, "verify_process", return_value=process()),
        ):
            return lock.build_lock_document(**arguments)

    def test_candidate_factory_is_fresh_and_spawn_safe(self):
        spec = POLICY_CATALOG[rust436.PAIR[0][0]]
        self.assertIs(
            type(spec.factory()), OneShantenDefensePlacementAwareSpeedCallPolicy
        )
        self.assertIsNot(spec.factory(), spec.factory())
        self.assertIs(_spec_from_binding(participant(0)).factory, spec.factory)
        check_policy_spec_serializable(spec)

    def test_frozen_identity_and_live_admission(self):
        doc = self.build()
        self.assertEqual(lock.locked_event(doc), 436)
        self.assertEqual(doc["lock_version"], 2)
        self.assertEqual(rust436.REVISION, "e6346ed2bb9e992138c05c4be367bd6a05ed00bc")
        self.assertEqual(
            rust436.WHEEL_SHA256,
            "b14e53fea4161cb81c7912ead2c9d1206c2b95a1b19eb4999c6c2b7a056fdbe6",
        )
        self.assertIs(rust423.event_module(423), rust423)
        self.assertIs(rust423.event_module(436), rust436)
        with fixtures.merged_main_execution(provenance()):
            lock.require_live_execution_target(doc, seed_ledger=self.ledger)
        for override in (
            {"event": 423},
            {"candidate": previous.participant(0)},
            {"wheel_path": None},
            {"candidate": participant(1)},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.build(**override)

    def test_rehashed_contract_and_old_native_evidence_fail(self):
        doc = self.build()
        for mutate in (
            lambda d: d["rust_execution"].update(wheel_sha256=rust423.WHEEL_SHA256),
            lambda d: d["rust_execution"].update(native_api_version=2),
            lambda d: d["provenance"].update(lisjong_revision=rust423.REVISION),
            lambda d: d.pop("rust_execution"),
        ):
            bad = copy.deepcopy(doc)
            mutate(bad)
            bad["lock_identity"] = lock.document_identity(
                {k: v for k, v in bad.items() if k != "lock_identity"}
            )
            with self.assertRaises(ValueError):
                lock.parse_lock_document(bad)
        good = evidence(doc, self.comparison)
        rust436.require_evidence(doc, good, self.comparison.seat_results)
        for mutate in (
            lambda e: e["blocks"].pop(),
            lambda e: e["blocks"].reverse(),
            lambda e: e["blocks"][0].update(process=previous.process(200)),
            lambda e: e["blocks"][0].update(game_native_calls=0),
            lambda e: e["blocks"][0].update(rows_identity="0" * 64),
        ):
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.assertRaises(ValueError):
                rust436.require_evidence(doc, bad, self.comparison.seat_results)

    def test_worker_identity_failure_prevents_games(self):
        with (
            mock.patch.object(
                rust436, "verify_process", side_effect=ValueError("wheel")
            ),
            mock.patch.object(rust423, "run_comparison") as games,
        ):
            with self.assertRaises(lock.HeuristicCandidateLockError):
                rust423._run_block(self.plan, "wheel", {}, 436)
            games.assert_not_called()

    def test_executor_dispatches_event_to_each_block(self):
        doc = self.build()
        pool = mock.MagicMock()
        pool.__enter__.return_value = pool

        def submit(function, plan, wheel, provenance_value, event):
            self.assertEqual(event, 436)
            self.assertIs(function, rust423._run_block)
            comparison = synthetic(plan)
            future = Future()
            future.set_result((comparison, evidence(doc, comparison)["blocks"][0]))
            return future

        pool.submit.side_effect = submit
        with (
            mock.patch.object(rust423, "ProcessPoolExecutor", return_value=pool),
            mock.patch.object(rust436, "verify_process", return_value=process()),
            mock.patch.object(
                rust423, "as_completed", side_effect=lambda fs: reversed(list(fs))
            ),
        ):
            comparison, proof = rust436.execute_rust(self.plan, lock=doc)
        self.assertEqual(comparison, self.comparison)
        self.assertEqual([b["seed"] for b in proof["blocks"]], list(fixtures.SEEDS))

    def test_real_spawn_transports_new_candidate_and_event(self):
        plan = replace(self.plan, seeds=(fixtures.SEEDS[0],))
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            comparison, record = pool.submit(
                spawned_block, plan, "wheel", execution_provenance_to_dict(provenance())
            ).result(timeout=30)
        self.assertEqual(comparison, synthetic(plan))
        self.assertNotEqual(record["process"]["backend"]["pid"], os.getpid())
        self.assertEqual(record["game_native_calls"], 10)

    def test_bundle_uses_436_executor_and_rejects_rehashed_tamper(self):
        doc = self.build()
        lock_path = self.root / "lock.json"
        lock.save_lock_document(doc, lock_path)
        with (
            fixtures.merged_main_execution(provenance()),
            mock.patch.object(
                rust436,
                "execute_rust",
                return_value=(self.comparison, evidence(doc, self.comparison)),
            ),
            mock.patch.object(
                fixtures.artifact_module,
                "_collect_execution_provenance",
                return_value=replace(
                    fixtures.comparison_provenance(), lisjong_revision=rust436.REVISION
                ),
            ),
        ):
            outcome = experiment.run_candidate_evaluation(
                lock_path=lock_path,
                candidate_spec=self.plan.policy_a,
                incumbent_spec=self.plan.policy_b,
                seed_ledger=self.ledger,
            )
        result.verify_candidate_bundle(
            lock_path=lock_path,
            comparison_path=outcome.comparison_artifact_path,
            result_path=outcome.candidate_result_path,
        )
        bad = copy.deepcopy(outcome.candidate_result)
        bad["rust_execution"]["blocks"][0]["game_native_calls"] = 0
        bad["result_identity"] = lock.document_identity(
            {k: v for k, v in bad.items() if k != "result_identity"}
        )
        outcome.candidate_result_path.write_text(canonical_json_text(bad))
        with self.assertRaises(result.HeuristicCandidateResultError):
            result.verify_candidate_bundle(
                lock_path=lock_path,
                comparison_path=outcome.comparison_artifact_path,
                result_path=outcome.candidate_result_path,
            )
