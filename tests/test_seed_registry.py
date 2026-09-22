import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisjong_arena import seed_registry

ARENA_REVISION = "a" * 40
PROTOCOL_REVISION = "protocol-v1"
DOMAIN = seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN


def reserved(ledger, seeds, *, issue="lisbun/lisjong-arena#999", domain=DOMAIN):
    updated, record = seed_registry.reserve_allocation(
        ledger,
        owner_issue=issue,
        protocol="test-protocol",
        seed_domain=domain,
        purpose="test allocation",
        population="test-population",
        split="TEST",
        seeds=seeds,
        arena_revision=ARENA_REVISION,
        protocol_revision=PROTOCOL_REVISION,
        provenance_reference="synthetic test fixture",
        allocation_timestamp="2026-09-22T00:00:00Z",
    )
    return updated, record


class SeedRegistryTest(unittest.TestCase):
    def test_empty_ledger_is_valid_and_deterministic(self):
        ledger = seed_registry.new_ledger()
        self.assertEqual(seed_registry.validate_ledger(ledger), ledger)
        self.assertEqual(
            seed_registry.ledger_revision(ledger), seed_registry.ledger_revision(ledger)
        )

    def test_range_and_explicit_membership_are_lossless(self):
        contiguous = seed_registry.seed_membership_document((10, 11, 12))
        explicit = seed_registry.seed_membership_document((12, 10, 14))
        self.assertEqual(contiguous, {"first": 10, "kind": "range", "last": 12})
        self.assertEqual(seed_registry.seeds_from_membership(contiguous), (10, 11, 12))
        self.assertEqual(explicit, {"kind": "explicit", "seeds": [10, 12, 14]})
        self.assertEqual(seed_registry.seeds_from_membership(explicit), (10, 12, 14))

    def test_duplicate_membership_and_same_domain_overlap_fail_closed(self):
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.seed_membership_document((1, 1))
        ledger, _ = reserved(seed_registry.new_ledger(), range(10, 20))
        for candidate in ((10,), (19, 20), range(15, 25)):
            with (
                self.subTest(candidate=tuple(candidate)),
                self.assertRaises(seed_registry.SeedRegistryError),
            ):
                reserved(ledger, candidate, issue="lisbun/lisjong-arena#1000")

    def test_same_integer_is_allowed_in_a_different_seed_domain(self):
        ledger, _ = reserved(seed_registry.new_ledger(), range(10, 20))
        updated, _ = reserved(
            ledger,
            range(10, 20),
            issue="lisbun/lisjong-arena#1000",
            domain=seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN,
        )
        self.assertEqual(len(updated["allocations"]), 2)

    def test_legacy_quarantine_blocks_new_explicit_domain_reuse(self):
        ledger = seed_registry.load_ledger()
        for domain in (
            seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN,
            seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        ):
            with self.subTest(domain=domain):
                collisions = seed_registry.collision_records(
                    ledger, seed_domain=domain, seeds=(0,)
                )
                self.assertTrue(collisions)
                self.assertTrue(
                    any(record["owner_issue"] is None for record in collisions)
                )
                with self.assertRaises(seed_registry.SeedRegistryError):
                    reserved(
                        ledger,
                        (0,),
                        issue="lisbun/lisjong-arena#1000",
                        domain=domain,
                    )

        with self.assertRaises(seed_registry.SeedRegistryError):
            reserved(
                seed_registry.new_ledger(),
                (90000,),
                domain=seed_registry.LEGACY_SEED_DOMAIN,
            )

    def test_state_never_returns_to_free(self):
        ledger, record = reserved(seed_registry.new_ledger(), range(20, 30))
        committed = seed_registry.transition_allocation(
            ledger, record["allocation_identity"], state=seed_registry.COMMITTED
        )
        retired = seed_registry.transition_allocation(
            committed, record["allocation_identity"], state=seed_registry.RETIRED
        )
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.transition_allocation(
                retired, record["allocation_identity"], state=seed_registry.COMMITTED
            )
        self.assertTrue(
            seed_registry.collision_records(retired, seed_domain=DOMAIN, seeds=(25,))
        )

    def test_strict_readback_rejects_noncanonical_or_tampered_ledger(self):
        ledger, _ = reserved(seed_registry.new_ledger(), range(30, 35))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            seed_registry.write_ledger(path, ledger)
            self.assertEqual(seed_registry.load_ledger(path), ledger)
            path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaises(seed_registry.SeedRegistryError):
                seed_registry.load_ledger(path)
            tampered = json.loads(seed_registry.canonical_json_text(ledger))
            tampered["allocations"][0]["seed_membership"]["last"] = 36
            path.write_text(
                seed_registry.canonical_json_text(tampered), encoding="utf-8"
            )
            with self.assertRaises(seed_registry.SeedRegistryError):
                seed_registry.load_ledger(path)

    def test_atomic_replace_failure_preserves_previous_ledger(self):
        original = seed_registry.new_ledger()
        updated, _ = reserved(original, range(35, 40))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            seed_registry.write_ledger(path, original)
            previous = path.read_bytes()
            with (
                patch.object(
                    seed_registry.os,
                    "replace",
                    side_effect=OSError("synthetic replace failure"),
                ),
                self.assertRaisesRegex(OSError, "synthetic replace failure"),
            ):
                seed_registry.write_ledger(path, updated)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.glob(".ledger.json.*.tmp")), [])

    def test_binding_carries_identity_membership_domain_and_ledger_revision(self):
        ledger, record = reserved(seed_registry.new_ledger(), range(40, 45))
        binding = seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )
        self.assertEqual(
            binding["ledger_revision"], seed_registry.ledger_revision(ledger)
        )
        self.assertEqual(binding["seed_domain"], DOMAIN)
        seed_registry.require_allocation_binding(
            ledger,
            binding,
            seeds=range(40, 45),
            owner_issue="lisbun/lisjong-arena#999",
            protocol="test-protocol",
            population="test-population",
            split="TEST",
        )
        stale = dict(binding)
        stale["ledger_revision"] = "0" * 64
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.require_allocation_binding(ledger, stale, seeds=range(40, 45))

    def test_branch_validation_detects_concurrent_main_reservation(self):
        base = seed_registry.new_ledger()
        branch, _ = reserved(base, range(50, 60), issue="lisbun/lisjong-arena#1001")
        current_main, _ = reserved(
            base, range(55, 65), issue="lisbun/lisjong-arena#1002"
        )
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.validate_branch_against_base(branch, current_main)

    def test_branch_validation_rejects_stale_deletion_and_state_regression(self):
        base, record = reserved(seed_registry.new_ledger(), range(70, 75))
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.validate_branch_against_base(seed_registry.new_ledger(), base)
        committed = seed_registry.transition_allocation(
            base, record["allocation_identity"], state=seed_registry.COMMITTED
        )
        with self.assertRaises(seed_registry.SeedRegistryError):
            seed_registry.validate_branch_against_base(base, committed)

    def test_bootstrap_contains_history_and_the_332_candidate_is_reserved(self):
        ledger = seed_registry.load_ledger()

        historical_ranges = (
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 0, 2499),
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 10000, 12499),
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 20000, 22899),
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 23000, 23199),
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 35000, 37905),
            (seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, 50000, 52299),
            (seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN, 2000, 2095),
            (seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN, 60000, 60099),
        )
        for domain, first, last in historical_ranges:
            with self.subTest(domain=domain, first=first, last=last):
                covered = set()
                for record in seed_registry.collision_records(
                    ledger, seed_domain=domain, seeds=range(first, last + 1)
                ):
                    covered.update(record["seeds"])
                    self.assertEqual(record["state"], seed_registry.RETIRED)
                self.assertEqual(covered, set(range(first, last + 1)))

        all_seeds = seed_registry.allocated_seeds(ledger)
        self.assertTrue(set(range(1000, 1008)) <= all_seeds)

        self.assertFalse(
            seed_registry.collision_records(
                ledger,
                seed_domain=seed_registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN,
                seeds=range(60000, 60100),
            )
        )

        collisions = seed_registry.collision_records(
            ledger,
            seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            seeds=range(70000, 70020),
        )
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["owner_issue"], "lisbun/lisjong-arena#332")
        self.assertEqual(collisions[0]["state"], seed_registry.RESERVED)
        record = seed_registry.find_allocation(
            ledger, collisions[0]["allocation_identity"]
        )
        self.assertEqual(record["split"], "QUALIFICATION")
        self.assertEqual(
            seed_registry.seeds_from_membership(record["seed_membership"]),
            tuple(range(70000, 70020)),
        )

    def test_cli_reserve_collision_commit_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            seed_registry.write_ledger(path, seed_registry.new_ledger())
            prefix = ["--ledger", str(path)]
            reserve_args = prefix + [
                "reserve",
                "--owner-issue",
                "lisbun/lisjong-arena#999",
                "--protocol",
                "test-protocol",
                "--seed-domain",
                DOMAIN,
                "--purpose",
                "cli test",
                "--population",
                "cli-population",
                "--split",
                "TEST",
                "--seeds",
                "80..84",
                "--arena-revision",
                ARENA_REVISION,
                "--protocol-revision",
                PROTOCOL_REVISION,
                "--provenance-reference",
                "synthetic fixture",
                "--allocation-timestamp",
                "2026-09-22T00:00:00Z",
            ]
            self.assertEqual(seed_registry.main(reserve_args), 0)
            ledger = seed_registry.load_ledger(path)
            identity = ledger["allocations"][0]["allocation_identity"]
            self.assertEqual(seed_registry.main(prefix + ["validate-ledger"]), 0)
            self.assertEqual(
                seed_registry.main(
                    prefix
                    + [
                        "check",
                        "--seed-domain",
                        DOMAIN,
                        "--seeds",
                        "84..85",
                    ]
                ),
                2,
            )
            self.assertEqual(
                seed_registry.main(prefix + ["commit", identity]),
                0,
            )
            self.assertEqual(
                seed_registry.find_allocation(
                    seed_registry.load_ledger(path), identity
                )["state"],
                seed_registry.COMMITTED,
            )


if __name__ == "__main__":
    unittest.main()
