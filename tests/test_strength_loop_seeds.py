"""#480 preparation boundaries; synthetic ledgers, no authority mutations/games."""

import copy
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from lisjong_arena import seed_registry as registry
from lisjong_arena import strength_loop_seeds as loop
from lisjong_arena._artifact_io import ArtifactValidationError, canonical_json_text


def spec():
    return {
        "schema": loop.SCHEMA,
        "run_id": "synthetic-001",
        "arena_revision": "a" * 40,
        "authority_commit": "b" * 40,
        "protocol": "synthetic-development-v1",
        "protocol_revision": "synthetic-v1",
        "provenance_reference": "synthetic test, not an execution approval",
        "pools": {"S1": [10, 11, 12], "S2": [20, 21, 22], "S3": [30, 31, 32]},
        "counts": {"S1": 2, "S2": 2},
        "formal_min_remaining": 2,
    }


def reserve(ledger, request):
    fields = {key: value for key, value in request.items() if key != "operation"}
    fields["seeds"] = registry.parse_seed_spec(fields["seeds"])
    return registry.reserve_allocation(
        ledger, **fields, allocation_timestamp="2026-10-24T00:00:00Z"
    )


def fixture():
    source = registry.new_ledger()
    plan = loop.prepare(spec(), source)
    current = source
    ids = {}
    for stage, request in plan["requests"].items():
        current, record = reserve(current, request)
        ids[stage] = record["allocation_identity"]
    return source, current, ids


class LoopSeedsTest(unittest.TestCase):
    def test_deterministic_disjoint_requests_leave_ledger_untouched(self):
        ledger = registry.new_ledger()
        before = copy.deepcopy(ledger)
        plan = loop.prepare(spec(), ledger)
        self.assertEqual(ledger, before)
        self.assertEqual(plan, loop.prepare(spec(), ledger))
        self.assertEqual(plan["status"], "PREPARED_NOT_RESERVED")
        self.assertEqual(set(plan["requests"]), {"S1", "S2"})
        self.assertEqual(plan["requests"]["S1"]["seeds"], "10,11")
        self.assertEqual(plan["requests"]["S2"]["seeds"], "20,21")
        self.assertEqual(plan["available_counts"]["S3"], 3)
        for request in plan["requests"].values():
            self.assertEqual(request["owner_issue"], loop.OWNER)

    def test_every_allocation_state_consumes_capacity(self):
        empty = registry.new_ledger()
        request = loop.prepare(spec(), empty)["requests"]["S1"]
        request["seeds"] = "10"
        ledger, row = reserve(empty, request)
        for state in registry.STATES:
            current = (
                ledger
                if state == registry.RESERVED
                else registry.transition_allocation(
                    ledger, row["allocation_identity"], state=state
                )
            )
            self.assertEqual(
                loop.prepare(spec(), current)["requests"]["S1"]["seeds"], "11,12"
            )

    def test_legacy_quarantine_blocks_but_unrelated_domain_does_not(self):
        for domain, expected in (
            (registry.LEGACY_SEED_DOMAIN, "11,12"),
            (registry.RIICHIENV_SINGLE_ROUND_SEED_DOMAIN, "10,11"),
        ):
            ledger = registry.new_ledger()
            row = registry.build_allocation_record(
                owner_issue=None,
                protocol="synthetic",
                seed_domain=domain,
                purpose="synthetic",
                population="synthetic",
                split=None,
                seeds=[10],
                state=registry.RETIRED,
                arena_revision=None,
                protocol_revision=None,
                provenance_reference="synthetic",
                allocation_timestamp=None,
            )
            ledger["allocations"].append(row)
            self.assertEqual(
                loop.prepare(spec(), ledger)["requests"]["S1"]["seeds"], expected
            )

    def test_malformed_spec_and_cross_stage_overlap_fail_closed(self):
        cases = []
        for name, value in (
            ("schema", "v999"),
            ("arena_revision", "main"),
            ("authority_commit", "latest"),
            ("formal_min_remaining", True),
            ("formal_min_remaining", 0),
        ):
            value_spec = spec()
            value_spec[name] = value
            cases.append(value_spec)
        for stage, values in (
            ("S1", [10, 10]),
            ("S2", [10, 20]),
            ("S3", [20, 30]),
            ("S1", [True]),
            ("S1", []),
        ):
            value_spec = spec()
            value_spec["pools"][stage] = values
            cases.append(value_spec)
        value_spec = spec()
        value_spec["counts"]["S3"] = 1
        cases.append(value_spec)
        value_spec = spec()
        value_spec["counts"]["S1"] = False
        cases.append(value_spec)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                loop.prepare(value, registry.new_ledger())

    def test_capacity_threshold_and_development_exhaustion(self):
        value = spec()
        value["formal_min_remaining"] = 3
        loop.prepare(value, registry.new_ledger())  # equality passes
        value["formal_min_remaining"] = 4
        with self.assertRaisesRegex(ArtifactValidationError, "formal seed capacity"):
            loop.prepare(value, registry.new_ledger())
        value = spec()
        value["counts"]["S2"] = 4
        with self.assertRaisesRegex(ArtifactValidationError, "S2 development"):
            loop.prepare(value, registry.new_ledger())

    def test_reservations_bind_and_committed_or_retired_cannot_start_again(self):
        source, current, ids = fixture()
        result = loop.bind(spec(), source, current, ids)
        self.assertEqual(result["status"], "RESERVED_NOT_EXECUTED")
        self.assertEqual(set(result["bindings"]), {"S1", "S2"})
        for state in (registry.COMMITTED, registry.RETIRED):
            updated = registry.transition_allocation(current, ids["S1"], state=state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                loop.bind(spec(), source, updated, ids)

    def test_competing_owner_or_revised_request_cannot_be_adopted(self):
        source, current, ids = fixture()
        for field, value in (
            ("run_id", "other"),
            ("arena_revision", "c" * 40),
            ("protocol_revision", "changed"),
            ("provenance_reference", "changed"),
        ):
            changed = spec()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                loop.bind(changed, source, current, ids)
        with self.assertRaises(ValueError):
            loop.bind(spec(), source, current, {"S1": ids["S2"], "S2": ids["S1"]})
        with self.assertRaises(ValueError):
            loop.bind(spec(), current, source, ids)

    def test_formal_capacity_is_rechecked_after_workflow_reservations(self):
        source, current, ids = fixture()
        request = loop.prepare(spec(), source)["requests"]["S1"]
        request.update(seeds="30,31", owner_issue="lisbun/lisjong-arena#999")
        current, _ = reserve(current, request)
        with self.assertRaisesRegex(ArtifactValidationError, "formal seed capacity"):
            loop.bind(spec(), source, current, ids)

    def test_cli_immutable_output_and_stop_does_not_publish(self):
        with (
            tempfile.TemporaryDirectory() as td,
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            root = Path(td)
            request = root / "spec.json"
            ledger = root / "ledger.json"
            output = root / "out.json"
            request.write_text(canonical_json_text(spec()), encoding="utf-8")
            registry.write_ledger(ledger, registry.new_ledger())
            args = [
                "prepare",
                "--spec",
                str(request),
                "--planning-ledger",
                str(ledger),
                "--output",
                str(output),
            ]
            self.assertEqual(loop.main(args), 0)
            before = output.read_bytes()
            self.assertEqual(loop.main(args), 2)
            self.assertEqual(output.read_bytes(), before)
            output.unlink()
            changed = spec()
            changed["formal_min_remaining"] = 4
            request.write_text(canonical_json_text(changed), encoding="utf-8")
            self.assertEqual(loop.main(args), 2)
            self.assertFalse(output.exists())
