"""lisjong#257 measurement population generator (lisjong-arena#453).

Pins the pre-registered seed population, split and units, the Seed Registry
authorization (every mismatch fails closed before a game is played), the rust
backend requirement, the per-unit archive, the re-run that reuses completed
units after a later unit failed, and the generation record.  Hanchan execution
and the v1 writer (covered by test_generate_hand_belief_source_255) are replaced
by fakes.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from lisjong_arena import seed_registry
from lisjong_arena.policy_source_record import binding as runtime_identity

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import generate_hand_belief_measurement_257 as measurement  # noqa: E402

ARENA = "a" * 40
UNIT_2_SEED = 933085


def fake_play(seed: int):
    game = (seed, [{"row": seed}], [{"fact": seed}], [(0, 1)])
    return game, {"wall_seconds": 1.5, "cpu_seconds": 1.25}


def fake_write_source(output, *, splits, games, producer):
    output.mkdir(parents=False, exist_ok=False)
    manifest = {
        "splits": splits,
        "producer": producer,
        "files": {"decisions": {"rows": len(games)}},
    }
    for name in measurement.SOURCE_FILES:
        text = json.dumps(manifest) if name == "manifest.json" else f"{name} {splits}"
        (output / name).write_text(text, encoding="utf-8")
    return manifest


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": measurement.OWNER_ISSUE,
        "protocol": measurement.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": measurement.POPULATION,
        "split": measurement.SPLIT,
        "seeds": measurement.MEASUREMENT_SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": measurement.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-07T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


class PopulationTest(unittest.TestCase):
    def test_population_is_the_preregistered_range_and_split(self) -> None:
        measurement.check_population()
        self.assertEqual(measurement.MEASUREMENT_SEEDS, tuple(range(933000, 933400)))
        self.assertEqual(measurement.TRAIN_SEEDS, tuple(range(933000, 933160)))
        self.assertEqual(measurement.VALID_SEEDS, tuple(range(933160, 933240)))
        self.assertEqual(measurement.EVAL_SEEDS, tuple(range(933240, 933400)))

    def test_units_follow_the_preregistered_layout(self) -> None:
        for unit in range(4):
            splits = measurement.unit_splits(unit)
            self.assertEqual(
                splits,
                {
                    "train": list(range(933000 + 40 * unit, 933040 + 40 * unit)),
                    "valid": list(range(933160 + 20 * unit, 933180 + 20 * unit)),
                    "test": list(range(933240 + 40 * unit, 933280 + 40 * unit)),
                },
            )
        for unit in (-1, 4):
            with self.assertRaises(measurement.MeasurementSourceError):
                measurement.unit_splits(unit)

    def test_the_pilot_producer_still_refuses_measurement_seeds(self) -> None:
        with self.assertRaises(Exception):
            measurement.dev._seed_range("933000..933001")

    def test_the_allocation_is_free_in_the_ledger_and_domain(self) -> None:
        reserved_ledger()


class AuthorizeTest(unittest.TestCase):
    def test_matching_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        binding, authorized = measurement.authorize(
            ledger, record["allocation_identity"], arena_revision=ARENA
        )
        self.assertEqual(binding["allocation_identity"], record["allocation_identity"])
        self.assertEqual(
            seed_registry.seeds_from_membership(authorized["seed_membership"]),
            measurement.MEASUREMENT_SEEDS,
        )

    def test_every_mismatch_is_rejected(self) -> None:
        cases = {
            "owner_issue": "lisbun/lisjong#245",
            "protocol": "another-protocol",
            "population": "another-population",
            "split": "TEST",
            "seeds": tuple(range(933000, 933399)),
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                ledger, record = reserved_ledger(**{field: value})
                with self.assertRaises(measurement.MeasurementSourceError):
                    measurement.authorize(
                        ledger, record["allocation_identity"], arena_revision=ARENA
                    )

    def test_other_revision_retired_and_unknown_are_rejected(self) -> None:
        ledger, record = reserved_ledger()
        identity = record["allocation_identity"]
        retired = seed_registry.transition_allocation(
            ledger, identity, state=seed_registry.RETIRED
        )
        for document, name, revision in (
            (ledger, identity, "d" * 40),
            (retired, identity, ARENA),
            (ledger, "0" * 64, ARENA),
            (ledger, "not-a-digest", ARENA),
        ):
            with self.subTest(name=name, revision=revision):
                with self.assertRaises(measurement.MeasurementSourceError):
                    measurement.authorize(document, name, arena_revision=revision)


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger, record = reserved_ledger()
        self.identity = record["allocation_identity"]

    def run_with(
        self,
        name="first",
        *,
        backend="rust",
        ledger=None,
        identity=None,
        reuse=None,
        fail_at=None,
        workers=1,
        revision=ARENA,
        prefix="progress-",
    ):
        """Run into ``<root>/<name>``; returns (document or error, played seeds)."""
        ledger = ledger or self.ledger
        ledger_path = self.root / f"{name}-ledger.json"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        runtime = {"arena_revision": revision, "shanten_backend": {"name": backend}}
        played = []

        def play(seed):
            if seed == fail_at:
                raise RuntimeError("engine failure")
            played.append(seed)
            return fake_play(seed)

        arguments = Namespace(
            seed_ledger=ledger_path,
            allocation_identity=identity or self.identity,
            workers=workers,
            output=self.root / name / "generated",
            archive_dir=self.root / name / "archive",
            archive_prefix=prefix,
            reuse_dir=None if reuse is None else self.root / reuse / "archive",
        )
        pool = mock.MagicMock()
        pool.return_value.__enter__.return_value.map = lambda function, seeds: [
            function(seed) for seed in seeds
        ]
        with (
            mock.patch.object(
                runtime_identity, "runtime_binding", return_value=runtime
            ),
            mock.patch.object(measurement.dev, "write_source", fake_write_source),
            mock.patch.object(
                measurement.dev, "verify_source_coverage", return_value=100
            ),
            mock.patch.object(
                measurement.dev, "_arena_revision", return_value=revision
            ),
            mock.patch.object(measurement.dev, "_revision", return_value="b" * 40),
            mock.patch.object(measurement, "ProcessPoolExecutor", pool),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                return measurement.run(arguments, play=play), played
            except (measurement.MeasurementSourceError, RuntimeError) as error:
                return error, played

    def archive(self, name="first"):
        return sorted(path.name for path in (self.root / name / "archive").iterdir())

    def test_run_generates_four_units_over_every_seed_once(self) -> None:
        document, played = self.run_with()
        self.assertEqual(sorted(played), list(measurement.MEASUREMENT_SEEDS))
        self.assertEqual([unit["hanchan"] for unit in document["units"]], [100] * 4)
        self.assertEqual([unit["reused"] for unit in document["units"]], [False] * 4)
        self.assertEqual(
            document["units"][1]["splits"],
            {
                "train": [933040, 933079],
                "valid": [933180, 933199],
                "test": [933280, 933319],
            },
        )
        self.assertEqual(document["units"][0]["games"][0]["cpu_seconds"], 1.25)
        self.assertEqual(document["allocation"]["state"], seed_registry.RESERVED)
        generated = self.root / "first" / "generated"
        written = json.loads((generated / "generation.json").read_text("utf-8"))
        self.assertEqual(written, document)
        self.assertEqual(
            self.archive(),
            sorted(
                f"progress-unit-{unit}.{suffix}"
                for unit in range(4)
                for suffix in ("tar.zst", "complete.json")
            ),
        )

    def test_python_backend_and_bad_allocation_stop_before_any_game(self) -> None:
        other, record = reserved_ledger(population="another-population")
        for index, options in enumerate(
            (
                {"backend": "python"},
                {"ledger": other, "identity": record["allocation_identity"]},
            )
        ):
            with self.subTest(options=options):
                error, played = self.run_with(f"stop-{index}", **options)
                self.assertIsInstance(error, measurement.MeasurementSourceError)
                self.assertEqual(played, [])
                self.assertFalse((self.root / f"stop-{index}").exists())

    def test_existing_output_is_refused(self) -> None:
        (self.root / "first" / "generated").mkdir(parents=True)
        error, played = self.run_with()
        self.assertIsInstance(error, measurement.MeasurementSourceError)
        self.assertEqual(played, [])

    def test_a_failed_later_unit_keeps_the_completed_units_only(self) -> None:
        error, played = self.run_with(fail_at=UNIT_2_SEED)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(
            self.archive(),
            [
                "progress-unit-0.complete.json",
                "progress-unit-0.tar.zst",
                "progress-unit-1.complete.json",
                "progress-unit-1.tar.zst",
            ],
        )
        # No statement that the population exists.
        self.assertFalse(
            (self.root / "first" / "generated" / "generation.json").exists()
        )
        record = json.loads(
            (
                self.root / "first" / "archive" / "progress-unit-1.complete.json"
            ).read_text("utf-8")
        )
        self.assertEqual(record["allocation_identity"], self.identity)
        self.assertEqual(record["producer"]["arena_revision"], ARENA)
        self.assertEqual(record["workers"], 1)

    def test_a_second_run_generates_only_the_missing_units(self) -> None:
        self.run_with(fail_at=UNIT_2_SEED)
        document, played = self.run_with("second", reuse="first")
        missing = [
            seed
            for unit in (2, 3)
            for seeds in measurement.unit_splits(unit).values()
            for seed in seeds
        ]
        self.assertEqual(played, missing)
        self.assertEqual(
            [unit["reused"] for unit in document["units"]], [True, True, False, False]
        )
        self.assertEqual(len(self.archive("second")), 8)
        generated = self.root / "second" / "generated"
        self.assertTrue((generated / "generation.json").exists())
        for unit in range(4):
            first = self.root / "first" / "generated" / f"unit-{unit}" / "manifest.json"
            restored = generated / f"unit-{unit}" / "manifest.json"
            if unit < 2:
                self.assertEqual(restored.read_bytes(), first.read_bytes())
            self.assertTrue(restored.exists())

    def test_a_changed_or_foreign_completed_unit_is_refused(self) -> None:
        self.run_with(fail_at=UNIT_2_SEED)
        archive = self.root / "first" / "archive"
        record_path = archive / "progress-unit-0.complete.json"
        archive_path = archive / "progress-unit-0.tar.zst"
        original_record = record_path.read_bytes()
        original_archive = archive_path.read_bytes()

        def edited(**changes):
            record = json.loads(original_record)
            record.update(changes)
            return json.dumps(record).encode("utf-8")

        other_ledger, other = reserved_ledger(arena_revision="d" * 40)
        cases = {
            "archive bytes": {"archive": original_archive[:-1] + b"\0"},
            "record digest": {"record": edited(archive={"name": "x"})},
            "file digest": {"record": edited(files={})},
            "another unit": {"record": edited(unit=1)},
            "another allocation": {"record": edited(allocation_identity="0" * 64)},
            "missing archive": {"archive": None},
            "missing record": {"record": None},
            "not json": {"record": b"{"},
            "another worker count": {"options": {"workers": 2}},
            "another revision": {
                "options": {
                    "revision": "d" * 40,
                    "ledger": other_ledger,
                    "identity": other["allocation_identity"],
                }
            },
        }
        for index, (name, case) in enumerate(cases.items()):
            with self.subTest(name=name):
                for path, original, key in (
                    (record_path, original_record, "record"),
                    (archive_path, original_archive, "archive"),
                ):
                    content = case.get(key, original)
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
                error, played = self.run_with(
                    f"refused-{index}", reuse="first", **case.get("options", {})
                )
                self.assertIsInstance(error, measurement.MeasurementSourceError)
                self.assertEqual(played, [])
                self.assertFalse(
                    (
                        self.root / f"refused-{index}" / "generated" / "generation.json"
                    ).exists()
                )

    def test_a_bad_later_unit_stops_before_an_earlier_missing_unit_is_played(
        self,
    ) -> None:
        self.run_with()
        complete = self.root / "first" / "archive"
        cases = {
            "unit 0 absent, unit 1 bad": {"absent": (0,), "bad": 1},
            "unit 0 good, unit 1 absent, unit 2 bad": {"absent": (1,), "bad": 2},
        }
        for index, (name, case) in enumerate(cases.items()):
            with self.subTest(name=name):
                reuse = self.root / f"partial-{index}" / "archive"
                shutil.copytree(complete, reuse)
                for unit in case["absent"]:
                    for suffix in ("tar.zst", "complete.json"):
                        (reuse / f"progress-unit-{unit}.{suffix}").unlink()
                bad = reuse / f"progress-unit-{case['bad']}.tar.zst"
                bad.write_bytes(bad.read_bytes()[:-1] + b"\0")
                error, played = self.run_with(
                    f"two-phase-{index}", reuse=f"partial-{index}"
                )
                self.assertIsInstance(error, measurement.MeasurementSourceError)
                self.assertEqual(played, [])
                generated = self.root / f"two-phase-{index}" / "generated"
                self.assertFalse((generated / "generation.json").exists())


if __name__ == "__main__":
    unittest.main()
