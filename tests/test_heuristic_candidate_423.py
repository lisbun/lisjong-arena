"""#423 lock, Rust fail-closed execution and strict bundle regression tests."""

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

import test_heuristic_candidate_aabb as fixtures

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.comparison import aggregate_policy_metrics
from lisjong_arena.heuristic_candidate_aabb import experiment, lock, result, rust423
from lisjong_arena.heuristic_candidate_aabb.__main__ import _spec_from_binding
from lisjong_arena.heuristic_candidate_aabb.protocol import (
    PROTOCOL_ID,
    SEED_DOMAIN,
    protocol_document,
    require_protocol_document,
)
from lisjong_arena.model import ComparisonPlan, ComparisonResult
from lisjong_arena.overall_champion_aabb.protocol import ParticipantBinding
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
)
from lisjong_arena.single_round_artifact import execution_provenance_to_dict


def participant(index):
    identity, factory = rust423.PAIR[index]
    return ParticipantBinding(
        family="heuristic",
        policy_identity=identity,
        factory_binding=f"lisjong_arena.policy_catalog:{factory}",
        implementation_source="lisjong",
        implementation_revision=rust423.REVISION,
    )


def provenance():
    return replace(
        fixtures.lock_provenance(),
        lisjong_revision=rust423.REVISION,
        lisjong_engine_revision="8735e89e1aea000ab59368d0368d476787827741",
    )


def process(pid=100):
    return {
        "backend": {
            "backend": "rust",
            "lisjong_revision": rust423.REVISION,
            "pid": pid,
            "native": {
                "module_file": "/native/__init__.py",
                "source_revision": rust423.REVISION,
                "api_version": 2,
                "probe_native_calls": 1,
            },
        },
        "wheel": {
            "file": rust423.WHEEL_FILENAME,
            "sha256": rust423.WHEEL_SHA256,
            "bytes": 234334,
            "installed_directory": "/native",
            "files": ["_lisjong_native/__init__.py"],
        },
        "provenance": execution_provenance_to_dict(provenance()),
    }


def synthetic(plan):
    rows = tuple(
        replace(
            row,
            policy_identity=rust423.PAIR[
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


def spawned_block_fixture(plan, wheel, provenance_value):
    # Real spawn transport; game/native work stays synthetic in CI.
    with (
        mock.patch.object(rust423, "verify_process", return_value=process(os.getpid())),
        mock.patch.object(rust423, "run_comparison", side_effect=synthetic),
        mock.patch.object(rust423, "native_call_count", side_effect=(20, 30)),
    ):
        return rust423._run_block(plan, wheel, provenance_value)


class Event423Tests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger, allocation = reserve_allocation(
            new_ledger(),
            owner_issue="lisbun/lisjong-arena#423",
            protocol=PROTOCOL_ID,
            seed_domain=SEED_DOMAIN,
            purpose="synthetic test only",
            population="heuristic-candidate-aabb-423",
            split="FORMAL-EVAL",
            seeds=fixtures.SEEDS,
            arena_revision=fixtures.ARENA_REVISION,
            protocol_revision=PROTOCOL_ID,
            provenance_reference="fixture",
            allocation_timestamp="2026-09-28T00:00:00Z",
        )
        self.allocation = allocation_binding(
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
            allocation_binding=self.allocation,
            event=423,
            wheel_path=self.root / rust423.WHEEL_FILENAME,
        )
        arguments.update(overrides)
        with (
            fixtures.merged_main_execution(provenance()),
            mock.patch.object(rust423, "verify_process", return_value=process()),
        ):
            return lock.build_lock_document(**arguments)

    def test_event_contract_and_historical_protocol(self):
        doc = self.build()
        self.assertEqual(lock.parse_lock_document(doc), doc)
        self.assertEqual(doc["lock_version"], 2)
        for event in (375, 423):
            self.assertEqual(
                require_protocol_document(
                    protocol_document(fixtures.SEEDS, event=event), "protocol"
                ),
                fixtures.SEEDS,
            )
        self.assertEqual(
            protocol_document(fixtures.SEEDS)["seed_allocation"]["owner_issue"],
            "lisbun/lisjong-arena#375",
        )

    def test_wrong_allocation_participant_and_missing_wheel_fail(self):
        old_ledger, old_binding = fixtures.ledger_with_allocation()
        for overrides in (
            {"seed_ledger": old_ledger, "allocation_binding": old_binding},
            {"candidate": participant(1)},
            {"wheel_path": None},
            {"event": 999},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(**overrides)

    def test_rehashed_lock_cannot_change_native_contract_or_dependencies(self):
        original = self.build()
        for change in (
            lambda d: d.pop("rust_execution"),
            lambda d: d["rust_execution"].update(wheel_sha256="0" * 64),
            lambda d: d.update(lock_version=1),
            lambda d: d["provenance"].update(lisjong_engine_revision="d" * 40),
            lambda d: d["participants"]["candidate"].update(
                implementation_revision="e" * 40
            ),
        ):
            doc = copy.deepcopy(original)
            change(doc)
            doc["lock_identity"] = lock.document_identity(
                {k: v for k, v in doc.items() if k != "lock_identity"}
            )
            with self.assertRaises(lock.HeuristicCandidateLockError):
                lock.parse_lock_document(doc)

    def test_evidence_rejects_missing_stale_or_unbound_worker_records(self):
        doc = self.build()
        good = evidence(doc, self.comparison)
        self.assertEqual(
            rust423.require_evidence(doc, good, self.comparison.seat_results), good
        )
        mutations = (
            lambda e: e["blocks"].pop(),
            lambda e: e["blocks"].reverse(),
            lambda e: e["blocks"][0].update(game_native_calls=0),
            lambda e: e["blocks"][0].update(rows_identity="0" * 64),
            lambda e: e["blocks"][0]["process"]["backend"].update(pid=100),
            lambda e: e["blocks"][0]["process"]["backend"]["native"].update(
                source_revision="e" * 40
            ),
            lambda e: e["blocks"][0]["process"]["wheel"].update(sha256="f" * 64),
            lambda e: e["blocks"][0]["process"]["provenance"].update(
                lisjong_arena_revision="e" * 40
            ),
        )
        for mutation in mutations:
            bad = copy.deepcopy(good)
            mutation(bad)
            with self.assertRaises(ValueError):
                rust423.require_evidence(doc, bad, self.comparison.seat_results)

    def test_worker_verifies_before_games_and_rechecks_after(self):
        plan = replace(self.plan, seeds=(fixtures.SEEDS[0],))
        with (
            mock.patch.object(
                rust423, "verify_process", side_effect=RuntimeError("bad wheel")
            ),
            mock.patch.object(rust423, "run_comparison") as run,
        ):
            with self.assertRaises(lock.HeuristicCandidateLockError):
                rust423._run_block(
                    plan, "wheel", execution_provenance_to_dict(provenance())
                )
            run.assert_not_called()
        with (
            mock.patch.object(
                rust423, "verify_process", return_value=process(200)
            ) as verify,
            mock.patch.object(rust423, "run_comparison", side_effect=synthetic),
            mock.patch.object(rust423, "native_call_count", side_effect=(20, 30)),
        ):
            comparison, record = rust423._run_block(
                plan, "wheel", execution_provenance_to_dict(provenance())
            )
            self.assertEqual(record["game_native_calls"], 10)
            self.assertEqual(
                record["rows_identity"], rust423._rows_identity(comparison.seat_results)
            )
            self.assertEqual(verify.call_count, 2)

    def test_executor_canonicalizes_completion_and_terminates_on_failure(self):
        doc = self.build()
        pool = mock.MagicMock()
        pool.__enter__.return_value = pool

        def submit(function, plan, wheel, provenance):
            future = Future()
            comparison = synthetic(plan)
            record = evidence(doc, comparison)["blocks"][0]
            future.set_result((comparison, record))
            return future

        pool.submit.side_effect = submit
        with (
            mock.patch.object(
                rust423, "ProcessPoolExecutor", return_value=pool
            ) as executor,
            mock.patch.object(rust423, "verify_process", return_value=process()),
            mock.patch.object(
                rust423,
                "as_completed",
                side_effect=lambda futures: reversed(list(futures)),
            ),
        ):
            result_value, record = rust423.execute_rust(self.plan, lock=doc)
            self.assertEqual(result_value, self.comparison)
            self.assertEqual(len(record["blocks"]), 100)
            self.assertEqual(
                executor.call_args.kwargs["mp_context"].get_start_method(), "spawn"
            )
        failed = Future()
        failed.set_exception(RuntimeError("worker failed"))
        pool.submit.side_effect = None
        pool.submit.return_value = failed
        with (
            mock.patch.object(rust423, "ProcessPoolExecutor", return_value=pool),
            mock.patch.object(rust423, "verify_process", return_value=process()),
            self.assertRaises(RuntimeError),
        ):
            rust423.execute_rust(self.plan, lock=doc)
        pool.terminate_workers.assert_called_once()

    def test_real_spawn_transports_verified_block_and_policy_factories(self):
        plan = replace(self.plan, seeds=(fixtures.SEEDS[0],))
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            comparison, record = pool.submit(
                spawned_block_fixture,
                plan,
                "wheel",
                execution_provenance_to_dict(provenance()),
            ).result(timeout=30)
        self.assertNotEqual(record["process"]["backend"]["pid"], os.getpid())
        self.assertEqual(comparison, synthetic(plan))

    def test_bundle_roundtrip_and_rehashed_evidence_tamper(self):
        doc = self.build()
        lock_path = self.root / "lock.json"
        lock.save_lock_document(doc, lock_path)
        proof = evidence(doc, self.comparison)
        cp = replace(
            fixtures.comparison_provenance(), lisjong_revision=rust423.REVISION
        )
        with (
            fixtures.merged_main_execution(provenance()),
            mock.patch.object(
                rust423, "execute_rust", return_value=(self.comparison, proof)
            ),
            mock.patch.object(
                fixtures.artifact_module,
                "_collect_execution_provenance",
                return_value=cp,
            ),
        ):
            outcome = experiment.run_candidate_evaluation(
                lock_path=lock_path,
                candidate_spec=self.plan.policy_a,
                incumbent_spec=self.plan.policy_b,
                seed_ledger=self.ledger,
            )
        self.assertEqual(outcome.candidate_result["result_version"], 2)
        self.assertEqual(
            outcome.candidate_result["protocol"]["seed_allocation"]["owner_issue"],
            "lisbun/lisjong-arena#423",
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


if __name__ == "__main__":
    unittest.main()
