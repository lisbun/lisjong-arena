"""Shared measurement-source allocation guard (lisjong-arena#463).

Synthetic presets only: no real population, no game and no live ledger.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lisjong_arena import measurement_allocation_guard as guard
from lisjong_arena import seed_registry

_ROOT = Path(__file__).resolve().parents[1]
ARENA = "a" * 40
SEEDS = tuple(range(990000, 990010))
PRESET = guard.AllocationPreset(
    owner_issue="lisbun/lisjong-arena#463",
    protocol="synthetic-guard-protocol-v1",
    seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
    population="synthetic-guard-10-hanchan",
    split="TRAIN4-VALID2-EVAL4",
    seeds=SEEDS,
    splits=(("train", SEEDS[:4]), ("valid", SEEDS[4:6]), ("test", SEEDS[6:])),
    split_sizes=(4, 2, 4),
    used_ranges=(range(989000, 990000), range(990010, 990020)),
)


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": PRESET.owner_issue,
        "protocol": PRESET.protocol,
        "seed_domain": PRESET.seed_domain,
        "purpose": "test",
        "population": PRESET.population,
        "split": PRESET.split,
        "seeds": PRESET.seeds,
        "arena_revision": ARENA,
        "protocol_revision": PRESET.protocol,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-08T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


class PresetTest(unittest.TestCase):
    def test_every_field_is_required_and_fixed(self) -> None:
        with self.assertRaises(TypeError):
            guard.AllocationPreset()
        for field in dataclasses.fields(guard.AllocationPreset):
            with self.subTest(field=field.name):
                self.assertIs(field.default, dataclasses.MISSING)
                self.assertIs(field.default_factory, dataclasses.MISSING)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PRESET.seeds = ()


class PopulationTest(unittest.TestCase):
    def rejected(self, **changes) -> None:
        with self.assertRaises(guard.AllocationGuardError):
            guard.check_population(dataclasses.replace(PRESET, **changes))

    def test_the_consistent_preset_passes(self) -> None:
        guard.check_population(PRESET)

    def test_a_missing_split_or_seed_is_rejected(self) -> None:
        self.rejected(splits=PRESET.splits[:2], split_sizes=(4, 2))
        self.rejected(splits=(), split_sizes=())
        self.rejected(
            splits=(("train", SEEDS[:4]), ("valid", SEEDS[4:5]), ("test", SEEDS[6:]))
        )

    def test_a_duplicated_split_or_seed_is_rejected(self) -> None:
        self.rejected(
            splits=(("train", SEEDS[:4]), ("train", SEEDS[4:6]), ("test", SEEDS[6:]))
        )
        self.rejected(
            splits=(("train", SEEDS[:5]), ("valid", SEEDS[4:6]), ("test", SEEDS[6:]))
        )
        repeated = SEEDS[:9] + SEEDS[:1]
        self.rejected(
            seeds=repeated,
            splits=(
                ("train", repeated[:4]),
                ("valid", repeated[4:6]),
                ("test", repeated[6:]),
            ),
        )

    def test_reordered_splits_and_other_lengths_are_rejected(self) -> None:
        self.rejected(
            splits=(("valid", SEEDS[4:6]), ("train", SEEDS[:4]), ("test", SEEDS[6:]))
        )
        self.rejected(
            splits=(("train", SEEDS[:5]), ("valid", SEEDS[5:6]), ("test", SEEDS[6:]))
        )
        self.rejected(split_sizes=(4, 2, 5))

    def test_malformed_seeds_are_rejected(self) -> None:
        self.rejected(seeds=(), splits=(("train", ()),), split_sizes=(0,))
        self.rejected(seeds=list(SEEDS))
        self.rejected(seeds=(True,), splits=(("train", (True,)),), split_sizes=(1,))

    def test_an_overlap_with_used_seeds_is_rejected(self) -> None:
        for used in (
            range(989995, 990001),
            range(990009, 990010),
            range(990004, 990005),
        ):
            with self.subTest(used=used):
                self.rejected(used_ranges=PRESET.used_ranges + (used,))


class AuthorizeTest(unittest.TestCase):
    def test_the_matching_fresh_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        binding, authorized = guard.authorize(
            ledger, record["allocation_identity"], PRESET, arena_revision=ARENA
        )
        self.assertEqual(binding["allocation_identity"], record["allocation_identity"])
        self.assertEqual(binding["seed_domain"], PRESET.seed_domain)
        self.assertEqual(authorized, record)
        self.assertEqual(authorized["state"], seed_registry.RESERVED)

    def test_every_allocation_mismatch_is_rejected(self) -> None:
        cases = {
            "owner_issue": "lisbun/lisjong#257",
            "protocol": "another-protocol",
            "population": "another-population",
            "split": "TEST",
            "seeds": SEEDS[:9],
            "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                ledger, record = reserved_ledger(**{field: value})
                with self.assertRaises(guard.AllocationGuardError):
                    guard.authorize(
                        ledger,
                        record["allocation_identity"],
                        PRESET,
                        arena_revision=ARENA,
                    )

    def test_another_or_dirty_revision_is_rejected(self) -> None:
        ledger, record = reserved_ledger()
        for revision in ("b" * 40, ARENA + "-dirty", None):
            with self.subTest(revision=revision):
                with self.assertRaises(guard.AllocationGuardError):
                    guard.authorize(
                        ledger,
                        record["allocation_identity"],
                        PRESET,
                        arena_revision=revision,
                    )

    def test_a_state_other_than_reserved_is_rejected(self) -> None:
        ledger, record = reserved_ledger()
        identity = record["allocation_identity"]
        for state in (seed_registry.COMMITTED, seed_registry.RETIRED):
            with self.subTest(state=state):
                moved = seed_registry.transition_allocation(
                    ledger, identity, state=state
                )
                with self.assertRaises(guard.AllocationGuardError):
                    guard.authorize(moved, identity, PRESET, arena_revision=ARENA)

    def test_an_unknown_or_malformed_identity_is_rejected(self) -> None:
        ledger, _ = reserved_ledger()
        for bad in ("0" * 64, "xyz", "A" * 64, 5, None):
            with self.subTest(identity=bad):
                with self.assertRaises(guard.AllocationGuardError):
                    guard.authorize(ledger, bad, PRESET, arena_revision=ARENA)

    def test_an_inconsistent_preset_is_rejected_before_the_ledger(self) -> None:
        ledger, record = reserved_ledger()
        overlapping = dataclasses.replace(PRESET, used_ranges=(range(990000, 990001),))
        with self.assertRaises(guard.AllocationGuardError):
            guard.authorize(
                ledger, record["allocation_identity"], overlapping, arena_revision=ARENA
            )


class FileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_the_live_ledger_is_read_or_refused(self) -> None:
        ledger, _ = reserved_ledger()
        path = self.directory / "ledger.json"
        # A live ledger need not be in the canonical serialization.
        path.write_text(
            seed_registry.canonical_json_text(ledger).replace("\n", "\r\n"),
            encoding="utf-8",
            newline="",
        )
        self.assertEqual(guard.load_live_ledger(path), ledger)
        path.write_text("{}", encoding="utf-8")
        for bad in (path, self.directory / "absent.json"):
            with self.subTest(path=bad.name):
                with self.assertRaises(guard.AllocationGuardError):
                    guard.load_live_ledger(bad)

    def test_written_text_is_complete_and_digested(self) -> None:
        path = self.directory / "record.json"
        guard.write_new(path, "first\n")
        guard.write_new(path, "second\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "second\n")
        self.assertEqual([p.name for p in self.directory.iterdir()], ["record.json"])
        # The digest is of the bytes on disk (text mode decides the newline).
        content = path.read_bytes()
        self.assertEqual(
            guard.file_digest(path),
            {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        )


@unittest.skipUnless(
    sys.platform != "win32" and shutil.which("bash"),
    "needs a POSIX bash (on Windows, PATH may resolve bash to the WSL launcher)",
)
class BootstrapSyntaxTest(unittest.TestCase):
    """Shell syntax only; the shared prologue has not been run on AWS (#463)."""

    def test_the_prologue_and_its_bootstrap_parse(self) -> None:
        for name in (
            "measurement-source-prologue.sh",
            "bootstrap-ron-legal-baseline-460.sh",
            "bootstrap-hand-belief-formal-test-464.sh",
        ):
            with self.subTest(name=name):
                checked = subprocess.run(
                    ["bash", "-n", f"scripts/aws/{name}"],
                    cwd=_ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_the_bootstrap_stops_without_the_prologue_input(self) -> None:
        checked = subprocess.run(
            ["bash", "scripts/aws/bootstrap-ron-legal-baseline-460.sh"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertIn("measurement-source-prologue.sh", checked.stderr)


if __name__ == "__main__":
    unittest.main()
