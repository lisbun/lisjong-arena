from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lisjong_arena import aws_operational_calibration, seed_registry
from lisjong_arena.offense_foundation import instrumentation, operational_calibration
from lisjong_arena.offense_foundation.__main__ import _parse_seed_spec, main
from lisjong_arena.offense_foundation.semantics import OffenseError

_SEEDS = tuple(range(908_000, 908_016))


def _ledger(**overrides):
    values = {
        "owner_issue": "lisbun/lisjong-arena#340",
        "protocol": operational_calibration.CALIBRATION_PROTOCOL,
        "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        "purpose": "synthetic calibration population",
        "population": operational_calibration.CALIBRATION_POPULATION,
        "split": None,
        "seeds": list(_SEEDS),
        "arena_revision": "a" * 40,
        "protocol_revision": "synthetic-v1",
        "provenance_reference": "synthetic fixture",
        "allocation_timestamp": "2026-09-22T00:00:00Z",
    }
    values.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.new_ledger(), **values)


class InstrumentationCapabilityTest(unittest.TestCase):
    def test_the_generation_path_satisfies_the_lighter_production_requirement(self):
        described = instrumentation.describe_generation_instrumentation()
        self.assertEqual("PASS", described["status"])
        self.assertFalse(described["per_seed_durable_receipt_supported"])
        self.assertEqual(
            "atomic-operational-progress", described["durable_evidence_level"]
        )
        self.assertEqual(
            "atomic-operational-progress",
            described["required_durable_evidence_level"],
        )
        self.assertIsNone(described["follow_up"])
        self.assertIn("full", described["limitation"])
        self.assertIn("locked phase", described["limitation"])

    def test_the_calibration_path_keeps_the_stronger_receipt_requirement(self):
        described = instrumentation.describe_calibration_instrumentation()
        self.assertEqual("PASS", described["status"])
        self.assertTrue(described["per_seed_durable_receipt_supported"])
        self.assertEqual(
            "per-seed-durable-receipt", described["durable_evidence_level"]
        )
        self.assertEqual(
            "per-seed-durable-receipt",
            described["required_durable_evidence_level"],
        )
        self.assertEqual(
            instrumentation.GENERATION_INSTRUMENTATION_IDENTITY,
            described["instrumentation_identity"],
        )

    def test_declared_levels_are_recognized_by_the_admission_core(self):
        for level in (
            instrumentation.GENERATION_DURABLE_EVIDENCE_LEVEL,
            instrumentation.PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL,
            instrumentation.CALIBRATION_DURABLE_EVIDENCE_LEVEL,
            instrumentation.CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL,
        ):
            self.assertIn(level, aws_operational_calibration.DURABLE_EVIDENCE_LEVELS)
        levels = aws_operational_calibration.DURABLE_EVIDENCE_LEVELS
        self.assertEqual(
            levels.index(instrumentation.GENERATION_DURABLE_EVIDENCE_LEVEL),
            levels.index(
                instrumentation.PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL
            ),
        )
        self.assertLess(
            levels.index(
                instrumentation.PRODUCTION_REQUIRED_DURABLE_EVIDENCE_LEVEL
            ),
            levels.index(
                instrumentation.CALIBRATION_REQUIRED_DURABLE_EVIDENCE_LEVEL
            ),
        )

    def test_the_cli_probe_is_machine_readable(self):
        import contextlib
        import io

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(0, main(["durable-evidence"]))
        self.assertEqual(
            instrumentation.describe_generation_instrumentation(),
            json.loads(stream.getvalue()),
        )


class CalibrationAllocationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, ledger):
        path = self.root / f"ledger-{id(ledger)}.json"
        seed_registry.write_ledger(path, ledger)
        return path

    def test_a_dedicated_allocation_resolves(self):
        ledger, record = _ledger()
        binding = seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )
        resolved = operational_calibration.require_calibration_allocation(
            list(_SEEDS), binding=binding, seed_ledger_path=self._write(ledger)
        )
        self.assertEqual(record["allocation_identity"], resolved["allocation_identity"])

    def test_a_fabricated_binding_is_rejected(self):
        ledger, record = _ledger()
        binding = dict(
            seed_registry.allocation_binding(ledger, record["allocation_identity"])
        )
        binding["allocation_identity"] = "9" * 64
        with self.assertRaises(OffenseError):
            operational_calibration.require_calibration_allocation(
                list(_SEEDS), binding=binding, seed_ledger_path=self._write(ledger)
            )

    def test_a_scientific_population_is_rejected(self):
        ledger, record = _ledger(
            population="offense-foundation", protocol="offense-foundation-v1"
        )
        binding = seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )
        with self.assertRaises(OffenseError):
            operational_calibration.require_calibration_allocation(
                list(_SEEDS), binding=binding, seed_ledger_path=self._write(ledger)
            )

    def test_a_split_bearing_allocation_is_rejected(self):
        ledger, record = _ledger(split="QUALIFICATION")
        binding = seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )
        with self.assertRaises(OffenseError):
            operational_calibration.require_calibration_allocation(
                list(_SEEDS), binding=binding, seed_ledger_path=self._write(ledger)
            )

    def test_seeds_must_be_exactly_the_allocated_membership(self):
        ledger, record = _ledger()
        binding = seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )
        with self.assertRaises(OffenseError):
            operational_calibration.require_calibration_allocation(
                list(_SEEDS[:-1]),
                binding=binding,
                seed_ledger_path=self._write(ledger),
            )


class BoundedCalibrationTest(unittest.TestCase):
    """The bounds that apply before any hanchan is executed."""

    def _run(self, **overrides):
        values = {
            "seeds": list(_SEEDS),
            "qualification_path": "unused.json",
            "scratch_dir": "unused-scratch",
            "receipt_dir": "unused-receipts",
            "run_id": "bounded",
            "workload_identity": "offense-foundation-332-phase-a-p2",
            "workers": 1,
        }
        values.update(overrides)
        return operational_calibration.run_calibration(**values)

    def test_an_empty_or_duplicated_population_is_rejected(self):
        for seeds in ([], [1, 1]):
            with self.subTest(seeds=seeds):
                with self.assertRaises(OffenseError):
                    self._run(seeds=seeds)

    def test_a_calibration_batch_is_bounded(self):
        self.assertEqual(256, operational_calibration.MAX_CALIBRATION_UNITS)
        with self.assertRaises(OffenseError):
            self._run(
                seeds=list(range(1, operational_calibration.MAX_CALIBRATION_UNITS + 2))
            )

    def test_worker_bounds_are_enforced(self):
        for workers in (0, 33, len(_SEEDS) + 1):
            with self.subTest(workers=workers):
                with self.assertRaises(OffenseError):
                    self._run(workers=workers)


class SeedSpecTest(unittest.TestCase):
    def test_range_and_explicit_forms(self):
        self.assertEqual([5, 6, 7], _parse_seed_spec("5-7"))
        self.assertEqual([5], _parse_seed_spec("5-5"))
        self.assertEqual([9, 4], _parse_seed_spec("9,4"))
        with self.assertRaises(ValueError):
            _parse_seed_spec("7-5")


if __name__ == "__main__":
    unittest.main()
