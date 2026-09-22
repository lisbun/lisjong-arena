import json
import tempfile
import unittest
from pathlib import Path

from lisjong_arena import seed_registry as registry


ARENA_REVISION = "a" * 40
PROTOCOL_REVISION = "protocol-v1"
DOMAIN = registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN


def reserved(ledger, seeds, *, issue="lisbun/lisjong-arena#999", domain=DOMAIN):
    updated, record = registry.reserve_allocation(
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
        ledger = registry.new_ledger()
        self.assertEqual(registry.validate_ledger(ledger), ledger)
        self.assertEqual(
            registry.ledger_revision(ledger), registry.ledger_revision(ledger)
        )

    def test_range_and_explicit_membership_are_lossless(self):
        contiguous = registry.seed_membership_document((10, 11, 12))
        explicit = registry.seed_membership_document((12, 10, 14))
        self.assertEqual(contiguous, {"first": 10, "kind": "range", "last": 12})
        self.assertEqual(registry.seeds_from_membership(contiguous), (10, 11, 12))
        self.assertEqual(explicit, {"kind": "explicit", "seeds": [10, 12, 14]})
        self.assertEqual(registry.seeds_from_membership(explicit), (10, 12, 14))

    def test_duplicate_membership_and_same_domain_overlap_fail_closed(self):
        with self.assertRaises(registry.SeedRegistryError):
            registry.seed_membership_document((1, 1))
        ledger, _ = reserved(registry.new_ledger(), range(10, 20))
        for candidate in ((10,), (19, 20), range(15, 25)):
            with (
                self.subTest(candidate=tuple(candidate)),
                self.assertRaises(registry.SeedRegistryError),
            ):
                reserved(ledger, candidate, issue="lisbun/lisjong-arena#1000")

    def test_same_integer_is_allowed_in_a_different_seed_domain(self):
        ledger, _ = reserved(registry.new_ledger(), range(10, 20))
        updated, _ = reserved(
            ledger,
            range(10, 20),
            issue="lisbun/lisjong-arena#1000",
            domain=registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN,
        )
        self.assertEqual(len(updated["allocations"]), 2)

    def test_state_never_returns_to_free(self):
        ledger, record = reserved(registry.new_ledger(), range(20, 30))
        committed = registry.transition_allocation(
            ledger, record["allocation_identity"], state=registry.COMMITTED
        )
        retired = registry.transition_allocation(
            committed, record["allocation_identity"], state=registry.RETIRED
        )
        with self.assertRaises(registry.SeedRegistryError):
            registry.transition_allocation(
                retired, record["allocation_identity"], state=registry.COMMITTED
            )
        self.assertTrue(
            registry.collision_records(retired, seed_domain=DOMAIN, seeds=(25,))
        )

    def test_strict_readback_rejects_noncanonical_or_tampered_ledger(self):
        ledger, _ = reserved(registry.new_ledger(), range(30, 35))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            registry.write_ledger(path, ledger)
            self.assertEqual(registry.load_ledger(path), ledger)
            path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaises(registry.SeedRegistryError):
                registry.load_ledger(path)
            tampered = json.loads(registry.canonical_json_text(ledger))
            tampered["allocations"][0]["seed_membership"]["last"] = 36
            path.write_text(registry.canonical_json_text(tampered), encoding="utf-8")
            with self.assertRaises(registry.SeedRegistryError):
                registry.load_ledger(path)

    def test_binding_carries_identity_membership_domain_and_ledger_revision(self):
        ledger, record = reserved(registry.new_ledger(), range(40, 45))
        binding = registry.allocation_binding(ledger, record["allocation_identity"])
        self.assertEqual(binding["ledger_revision"], registry.ledger_revision(ledger))
        self.assertEqual(binding["seed_domain"], DOMAIN)
        registry.require_allocation_binding(
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
        with self.assertRaises(registry.SeedRegistryError):
            registry.require_allocation_binding(ledger, stale, seeds=range(40, 45))

    def test_branch_validation_detects_concurrent_main_reservation(self):
        base = registry.new_ledger()
        branch, _ = reserved(base, range(50, 60), issue="lisbun/lisjong-arena#1001")
        current_main, _ = reserved(
            base, range(55, 65), issue="lisbun/lisjong-arena#1002"
        )
        with self.assertRaises(registry.SeedRegistryError):
            registry.validate_branch_against_base(branch, current_main)

    def test_branch_validation_rejects_stale_deletion_and_state_regression(self):
        base, record = reserved(registry.new_ledger(), range(70, 75))
        with self.assertRaises(registry.SeedRegistryError):
            registry.validate_branch_against_base(registry.new_ledger(), base)
        committed = registry.transition_allocation(
            base, record["allocation_identity"], state=registry.COMMITTED
        )
        with self.assertRaises(registry.SeedRegistryError):
            registry.validate_branch_against_base(base, committed)

    def test_bootstrap_contains_history_and_the_332_candidate_is_reserved(self):
        ledger = registry.load_ledger()
        all_seeds = registry.allocated_seeds(ledger)
        self.assertTrue(set(range(647, 851)) <= all_seeds)
        self.assertTrue(set(range(1000, 1008)) <= all_seeds)
        self.assertTrue(set(range(2000, 2096)) <= all_seeds)
        self.assertTrue(set(range(50000, 52300)) <= all_seeds)
        collisions = registry.collision_records(
            ledger,
            seed_domain=registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            seeds=range(70000, 70020),
        )
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["owner_issue"], "lisbun/lisjong-arena#332")
        self.assertEqual(collisions[0]["state"], registry.RESERVED)
        record = registry.find_allocation(ledger, collisions[0]["allocation_identity"])
        self.assertEqual(record["split"], "QUALIFICATION")
        self.assertEqual(
            registry.seeds_from_membership(record["seed_membership"]),
            tuple(range(70000, 70020)),
        )

    def test_cli_reserve_collision_commit_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            registry.write_ledger(path, registry.new_ledger())
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
            self.assertEqual(registry.main(reserve_args), 0)
            ledger = registry.load_ledger(path)
            identity = ledger["allocations"][0]["allocation_identity"]
            self.assertEqual(registry.main(prefix + ["validate-ledger"]), 0)
            self.assertEqual(
                registry.main(
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
                registry.main(prefix + ["commit", identity]),
                0,
            )
            self.assertEqual(
                registry.find_allocation(registry.load_ledger(path), identity)["state"],
                registry.COMMITTED,
            )


if __name__ == "__main__":
    unittest.main()
