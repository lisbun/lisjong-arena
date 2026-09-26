"""#385 L0.3 Step F paired-strength protocol / lock / result tests.

実engine・torch・gitは起動しない。live probe（Arena HEAD、installed revision、
artifact loader、artifact digest）と単一game境界を差し替える。
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lisjong.learning import (
    CONSTANT_RESIDUAL_RUNTIME_IDENTITY,
    outcome_q_runtime_identity,
)

from lisjong_arena.l03_paired_strength import execution, lock, protocol, result
from lisjong_arena.l03_paired_strength.experiment import run_paired_strength
from lisjong_arena.l03_paired_strength.lock import PairedStrengthLockError
from lisjong_arena.l03_paired_strength.protocol import (
    PairedStrengthProtocolError,
    game_schedule,
)
from lisjong_arena.l03_paired_strength.result import PairedStrengthResultError
from lisjong_arena.seed_registry import (
    allocation_binding,
    new_ledger,
    reserve_allocation,
    transition_allocation,
)

HEAD = "a" * 40
ENVIRONMENT = {
    "lisjong": protocol.LISJONG_REVISION,
    "lisjong-engine": protocol.LISJONG_ENGINE_REVISION,
    "python": "3.14.7",
    "python_implementation": "CPython",
    "torch": "2.13.0+cpu",
}


class _Runtime:
    artifact_identity = protocol.CANDIDATE_ARTIFACT_IDENTITY
    identity = protocol.CANDIDATE_RUNTIME_IDENTITY


def _loader(path: Path) -> object:
    return _Runtime()


def _reserve(ledger, *, seeds=protocol.ORDERED_SEEDS, **overrides):
    options = {
        "owner_issue": protocol.OWNER_ISSUE,
        "protocol": protocol.PROTOCOL_ID,
        "seed_domain": protocol.SEED_DOMAIN,
        "purpose": "#385 paired strength",
        "population": protocol.ALLOCATION_POPULATION,
        "split": protocol.ALLOCATION_SPLIT,
        "seeds": seeds,
        "arena_revision": HEAD,
        "protocol_revision": protocol.PROTOCOL_ID,
        "provenance_reference": "https://github.com/lisbun/lisjong-arena/issues/385",
        "allocation_timestamp": "2026-09-26T00:00:00Z",
    }
    options.update(overrides)
    return reserve_allocation(ledger, **options)


def _ledger_and_binding(**overrides):
    ledger, record = _reserve(new_ledger(), **overrides)
    return ledger, allocation_binding(ledger, record["allocation_identity"])


def _fake_record(assignment, *, candidate_bonus=0):
    points = [100, 0, -30, -70]
    if assignment.arm == protocol.CANDIDATE_ARM and candidate_bonus:
        focal = assignment.focal_seat
        other = (focal + 1) % 4
        points[focal] += candidate_bonus
        points[other] -= candidate_bonus
    ranks = [0, 0, 0, 0]
    for rank, seat in enumerate(
        sorted(range(4), key=lambda seat: (-points[seat], seat)), start=1
    ):
        ranks[seat] = rank
    return {
        **assignment.to_document(),
        "final_points": points,
        "final_raw_scores": [40000, 25000, 20000, 15000],
        "focal_deal_ins": 0,
        "focal_riichi_deposits": 1,
        "focal_wins": 1,
        "kyoku_count": 8,
        "match_end_reason": "final_round",
        "ranks": ranks,
    }


class _Probes:
    """lockのlive probeを差し替えるcontext manager。"""

    def __init__(self, *, head=HEAD, environment=None, digests=None):
        self.patches = [
            mock.patch.object(lock, "_arena_head", return_value=head),
            mock.patch.object(
                lock,
                "_installed_environment",
                return_value=dict(environment or ENVIRONMENT),
            ),
            mock.patch.object(
                lock,
                "artifact_file_digests",
                return_value=dict(digests or protocol.CANDIDATE_ARTIFACT_FILE_SHA256),
            ),
        ]

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


class ProtocolTest(unittest.TestCase):
    def test_frozen_runtime_identities_match_lisjong_contract(self):
        self.assertEqual(
            outcome_q_runtime_identity(protocol.CANDIDATE_ARTIFACT_IDENTITY),
            protocol.CANDIDATE_RUNTIME_IDENTITY,
        )
        self.assertEqual(
            CONSTANT_RESIDUAL_RUNTIME_IDENTITY, protocol.BASELINE_RUNTIME_IDENTITY
        )

    def test_schedule_is_balanced_paired_and_ordered(self):
        schedule = game_schedule()
        self.assertEqual(len(schedule), protocol.HANCHAN_COUNT)
        self.assertEqual(protocol.HANCHAN_COUNT, 8_000)
        self.assertEqual([a.game_ordinal for a in schedule], list(range(8_000)))
        self.assertEqual(schedule[0].seed, 931_000)
        self.assertEqual(schedule[-1].seed, 931_999)
        focal_counts = {seat: 0 for seat in protocol.FOCAL_SEATS}
        for index in range(0, len(schedule), 2):
            candidate, baseline = schedule[index], schedule[index + 1]
            self.assertEqual(candidate.arm, protocol.CANDIDATE_ARM)
            self.assertEqual(baseline.arm, protocol.BASELINE_ARM)
            self.assertEqual(candidate.seed, baseline.seed)
            self.assertEqual(candidate.focal_seat, baseline.focal_seat)
            focal_counts[candidate.focal_seat] += 1
        self.assertEqual(set(focal_counts.values()), {protocol.SEED_BLOCK_COUNT})
        first_block = schedule[: protocol.HANCHAN_PER_BLOCK]
        self.assertEqual(
            [(a.focal_seat, a.arm) for a in first_block],
            [(seat, arm) for seat in (0, 1, 2, 3) for arm in protocol.ARMS],
        )
        self.assertEqual({a.seed for a in first_block}, {931_000})

    def test_population_is_disjoint_from_diagnostic_seeds(self):
        self.assertFalse(
            set(protocol.ORDERED_SEEDS) & set(protocol.EXCLUDED_DIAGNOSTIC_SEEDS)
        )

    def test_protocol_document_drift_is_rejected(self):
        document = protocol.protocol_document()
        protocol.require_protocol_document(copy.deepcopy(document), "protocol")
        mutated = copy.deepcopy(document)
        mutated["budget"]["seed_block_count"] = 1_001
        with self.assertRaises(PairedStrengthProtocolError):
            protocol.require_protocol_document(mutated, "protocol")
        mutated = copy.deepcopy(document)
        mutated["extra"] = True
        with self.assertRaises(PairedStrengthProtocolError):
            protocol.require_protocol_document(mutated, "protocol")


class LockTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.result_path = self.root / "result.json"

    def _build(self, ledger, binding, **kwargs):
        return lock.build_lock_document(
            artifact_path=self.root / "artifact",
            seed_ledger=ledger,
            allocation_binding=binding,
            result_destination=self.result_path,
            loader=kwargs.pop("loader", _loader),
        )

    def test_lock_binds_contract_and_strict_readback_reproduces_it(self):
        ledger, binding = _ledger_and_binding()
        with _Probes():
            document = self._build(ledger, binding)
            path = lock.save_lock_document(document, self.root / "lock.json")
            loaded = lock.load_lock_document(path)
            lock.require_live_target(
                loaded,
                artifact_path=self.root / "artifact",
                seed_ledger=ledger,
                loader=_loader,
            )
        self.assertFalse(loaded["result_exposed"])
        self.assertEqual(loaded["protocol"], protocol.protocol_document())
        self.assertEqual(loaded["execution_target"]["revision"], HEAD)
        self.assertEqual(loaded["seed_allocation"]["binding"], binding)
        self.assertEqual(
            loaded["candidate_artifact"]["artifact_identity"],
            protocol.CANDIDATE_ARTIFACT_IDENTITY,
        )
        self.assertEqual(loaded["protocol"]["parent_issue"], protocol.PARENT_ISSUE)

    def test_committed_allocation_still_satisfies_the_lock(self):
        ledger, binding = _ledger_and_binding()
        with _Probes():
            document = self._build(ledger, binding)
            committed = transition_allocation(
                ledger, binding["allocation_identity"], state="COMMITTED"
            )
            lock.require_live_target(
                document,
                artifact_path=self.root / "artifact",
                seed_ledger=committed,
                loader=_loader,
            )

    def test_retired_allocation_is_rejected(self):
        ledger, binding = _ledger_and_binding()
        with _Probes():
            document = self._build(ledger, binding)
            retired = transition_allocation(
                ledger, binding["allocation_identity"], state="RETIRED"
            )
            with self.assertRaisesRegex(PairedStrengthLockError, "not active"):
                lock.require_live_target(
                    document,
                    artifact_path=self.root / "artifact",
                    seed_ledger=retired,
                    loader=_loader,
                )

    def test_allocation_ownership_revision_and_membership_fail_closed(self):
        cases = {
            "owner": {"owner_issue": "lisbun/lisjong-arena#375"},
            "population": {"population": "other"},
            "split": {"split": "FORMAL-EVAL"},
            "revision": {"arena_revision": "b" * 40},
        }
        for name, override in cases.items():
            with self.subTest(name), _Probes():
                ledger, binding = _ledger_and_binding(**override)
                with self.assertRaises(PairedStrengthLockError):
                    self._build(ledger, binding)
        with _Probes():
            ledger, binding = _ledger_and_binding(seeds=range(931_000, 931_500))
            with self.assertRaises(PairedStrengthLockError):
                self._build(ledger, binding)

    def test_population_overlapping_another_allocation_domain_is_rejected(self):
        ledger, _ = _reserve(
            new_ledger(),
            seed_domain="riichienv-4p-red-half-hanchan-v1",
            owner_issue="lisbun/lisjong-arena#1",
            population="elsewhere",
            seeds=[931_500],
        )
        ledger, record = _reserve(ledger)
        binding = allocation_binding(ledger, record["allocation_identity"])
        with _Probes(), self.assertRaisesRegex(PairedStrengthLockError, "disjoint"):
            self._build(ledger, binding)

    def test_environment_and_artifact_drift_fail_closed(self):
        ledger, binding = _ledger_and_binding()
        drifts = {
            "lisjong": {**ENVIRONMENT, "lisjong": "c" * 40},
            "engine": {**ENVIRONMENT, "lisjong-engine": "c" * 40},
            "torch": {**ENVIRONMENT, "torch": "2.12.0"},
        }
        for name, environment in drifts.items():
            with self.subTest(name), _Probes(environment=environment):
                with self.assertRaises(PairedStrengthLockError):
                    self._build(ledger, binding)
        bad_digests = {**protocol.CANDIDATE_ARTIFACT_FILE_SHA256}
        bad_digests["weights.f32"] = "0" * 64
        with (
            _Probes(digests=bad_digests),
            self.assertRaisesRegex(PairedStrengthLockError, "bytes"),
        ):
            self._build(ledger, binding)

        class WrongRuntime(_Runtime):
            identity = "0" * 64

        with _Probes(), self.assertRaisesRegex(PairedStrengthLockError, "runtime"):
            self._build(ledger, binding, loader=lambda path: WrongRuntime())

    def test_live_target_drift_after_lock_is_rejected(self):
        ledger, binding = _ledger_and_binding()
        with _Probes():
            document = self._build(ledger, binding)
        with _Probes(environment={**ENVIRONMENT, "python": "3.14.8"}):
            with self.assertRaisesRegex(PairedStrengthLockError, "environment"):
                lock.require_live_target(
                    document,
                    artifact_path=self.root / "artifact",
                    seed_ledger=ledger,
                    loader=_loader,
                )

    def test_tampered_or_exposed_lock_is_rejected(self):
        ledger, binding = _ledger_and_binding()
        with _Probes():
            document = self._build(ledger, binding)
        exposed = {**document, "result_exposed": True}
        with self.assertRaisesRegex(PairedStrengthLockError, "result_exposed"):
            lock.parse_lock_document(exposed)
        tampered = copy.deepcopy(document)
        tampered["artifact_destinations"]["result"] = str(self.root / "other.json")
        with self.assertRaisesRegex(PairedStrengthLockError, "identity"):
            lock.parse_lock_document(tampered)
        tampered = copy.deepcopy(document)
        tampered["schedule_sha256"] = "0" * 64
        with self.assertRaisesRegex(PairedStrengthLockError, "schedule"):
            lock.parse_lock_document(tampered)
        missing = {k: v for k, v in document.items() if k != "seed_allocation"}
        with self.assertRaises(PairedStrengthLockError):
            lock.parse_lock_document(missing)

    def test_existing_result_destination_is_rejected(self):
        ledger, binding = _ledger_and_binding()
        self.result_path.write_text("{}", encoding="utf-8")
        with _Probes(), self.assertRaisesRegex(PairedStrengthLockError, "write-once"):
            self._build(ledger, binding)

    def test_artifact_file_digests_require_exact_file_set(self):
        artifact = self.root / "artifact"
        artifact.mkdir()
        (artifact / "manifest.json").write_bytes(b"{}")
        with self.assertRaisesRegex(PairedStrengthLockError, "exactly"):
            lock.artifact_file_digests(artifact)
        (artifact / "weights.f32").write_bytes(b"\x00")
        self.assertEqual(
            lock.artifact_file_digests(artifact)["weights.f32"],
            hashlib.sha256(b"\x00").hexdigest(),
        )


class ExecutionTest(unittest.TestCase):
    def test_execute_schedule_returns_validated_records_in_order(self):
        schedule = game_schedule()[:16]
        seen = []
        records = execution.execute_schedule(
            schedule,
            run_game=_fake_record,
            max_workers=1,
            progress_callback=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual([r["game_ordinal"] for r in records], list(range(16)))
        self.assertEqual(seen[-1], (16, 16))

    def test_any_failed_game_invalidates_the_event(self):
        def failing(assignment):
            if assignment.game_ordinal == 5:
                raise RuntimeError("engine failure")
            return _fake_record(assignment)

        with self.assertRaisesRegex(
            execution.PairedStrengthExecutionError, "STOP / INVALID"
        ):
            execution.execute_schedule(
                game_schedule()[:8], run_game=failing, max_workers=1
            )

    def test_invalid_record_invalidates_the_event(self):
        def mismatched(assignment):
            record = _fake_record(assignment)
            record["seed"] += 1
            return record

        def unbalanced(assignment):
            record = _fake_record(assignment)
            record["final_points"] = [1, 0, 0, 0]
            return record

        def bad_ranks(assignment):
            record = _fake_record(assignment)
            record["ranks"] = [1, 1, 2, 4]
            return record

        for run_game in (mismatched, unbalanced, bad_ranks):
            with self.subTest(run_game.__name__):
                with self.assertRaises(execution.PairedStrengthExecutionError):
                    execution.execute_schedule(
                        game_schedule()[:2], run_game=run_game, max_workers=1
                    )


class ResultTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.ledger, binding = _ledger_and_binding()
        with _Probes():
            document = lock.build_lock_document(
                artifact_path=self.root / "artifact",
                seed_ledger=self.ledger,
                allocation_binding=binding,
                result_destination=self.root / "result.json",
                loader=_loader,
            )
        self.lock_path = lock.save_lock_document(document, self.root / "lock.json")
        self.lock = document

    def _records(self, bonus_for_block):
        return tuple(
            _fake_record(a, candidate_bonus=bonus_for_block(a.block_index))
            for a in game_schedule()
        )

    def test_positive_paired_effect_is_improved(self):
        records = self._records(lambda block: 20 + block % 7)
        document = result.build_result_document(
            lock_document=self.lock, records=records
        )
        self.assertEqual(document["classification"]["label"], protocol.IMPROVED_LABEL)
        primary = document["primary"]
        self.assertEqual(primary["summary"]["block_count"], 1_000)
        self.assertEqual(primary["valid_paired_units"], 4_000)
        self.assertGreater(primary["summary"]["interval_lower"], 0)
        self.assertEqual(document["secondary"]["overrides_primary"], False)

    def test_zero_or_noisy_effect_is_not_established(self):
        for name, bonus in {
            "zero": lambda block: 0,
            "noisy": lambda block: 30 if block % 2 else -30,
            "negative": lambda block: -20 - block % 5,
        }.items():
            with self.subTest(name):
                document = result.build_result_document(
                    lock_document=self.lock, records=self._records(bonus)
                )
                self.assertEqual(
                    document["classification"]["label"], protocol.NOT_ESTABLISHED_LABEL
                )
        zero = result.build_result_document(
            lock_document=self.lock, records=self._records(lambda block: 0)
        )
        self.assertEqual(
            zero["secondary"]["identical_final_points_paired_units"], 4_000
        )

    def test_primary_uses_focal_seat_final_points_per_seed_block(self):
        records = self._records(lambda block: 10 if block == 0 else 0)
        blocks, _ = result.derive_seed_blocks(result.validate_records(records))
        self.assertEqual(blocks[0].delta, 10)
        self.assertEqual(blocks[0].seed, 931_000)
        self.assertEqual({block.delta for block in blocks[1:]}, {0})

    def test_incomplete_or_reordered_records_are_invalid(self):
        records = list(self._records(lambda block: 0))
        with self.assertRaises(PairedStrengthResultError):
            result.build_result_document(lock_document=self.lock, records=records[:-2])
        swapped = records[:]
        swapped[0], swapped[1] = swapped[1], swapped[0]
        with self.assertRaises(PairedStrengthResultError):
            result.build_result_document(lock_document=self.lock, records=swapped)

    def test_run_writes_verified_write_once_result(self):
        records = {
            a.game_ordinal: _fake_record(a, candidate_bonus=15) for a in game_schedule()
        }
        with _Probes():
            outcome = run_paired_strength(
                lock_path=self.lock_path,
                artifact_path=self.root / "artifact",
                seed_ledger=self.ledger,
                max_workers=1,
                run_game=lambda assignment: records[assignment.game_ordinal],
                loader=_loader,
            )
            self.assertEqual(
                outcome.result["classification"]["label"], protocol.IMPROVED_LABEL
            )
            verified = result.verify_result(
                lock_path=self.lock_path, result_path=outcome.result_path
            )
            self.assertEqual(
                verified["result_identity"], outcome.result["result_identity"]
            )
            with self.assertRaises(PairedStrengthLockError):
                run_paired_strength(
                    lock_path=self.lock_path,
                    artifact_path=self.root / "artifact",
                    seed_ledger=self.ledger,
                    max_workers=1,
                    run_game=lambda assignment: records[assignment.game_ordinal],
                    loader=_loader,
                )

    def test_verify_rejects_tampered_statistics(self):
        records = self._records(lambda block: 0)
        document = result.build_result_document(
            lock_document=self.lock, records=records
        )
        document["classification"] = {
            "kind": protocol.IMPROVED_KIND,
            "label": protocol.IMPROVED_LABEL,
        }
        path = self.root / "result.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(PairedStrengthResultError):
            result.verify_result(lock_path=self.lock_path, result_path=path)

    def test_failed_execution_writes_no_result(self):
        def failing(assignment):
            raise RuntimeError("boom")

        with _Probes(), self.assertRaises(execution.PairedStrengthExecutionError):
            run_paired_strength(
                lock_path=self.lock_path,
                artifact_path=self.root / "artifact",
                seed_ledger=self.ledger,
                max_workers=1,
                run_game=failing,
                loader=_loader,
            )
        self.assertFalse((self.root / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
