"""lisjong#257 measurement population generator (lisjong-arena#453).

Pins the pre-registered seed population, split and units, the Seed Registry
authorization (every mismatch fails closed before a game is played), the rust
backend requirement and the generation record.  Hanchan execution and the v1
writer (covered by test_generate_hand_belief_source_255) are replaced by fakes.
"""

import json
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


def fake_play(seed: int):
    game = (seed, [{"row": seed}], [{"fact": seed}], [(0, 1)])
    return game, {"wall_seconds": 1.5, "cpu_seconds": 1.25}


def fake_write_source(output, *, splits, games, producer):
    output.mkdir(parents=False, exist_ok=False)
    for name in measurement.SOURCE_FILES:
        (output / name).write_text(name, encoding="utf-8")
    return {"splits": splits, "files": {"decisions": {"rows": len(games)}}}


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
    def run_with(self, directory, *, backend="rust", ledger=None, identity=None):
        if ledger is None:
            ledger, record = reserved_ledger()
            identity = record["allocation_identity"]
        ledger_path = Path(directory) / "ledger.json"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        runtime = {"arena_revision": ARENA, "shanten_backend": {"name": backend}}
        played = []

        def play(seed):
            played.append(seed)
            return fake_play(seed)

        arguments = Namespace(
            seed_ledger=ledger_path,
            allocation_identity=identity,
            workers=1,
            output=Path(directory) / "out",
        )
        with (
            mock.patch.object(
                runtime_identity, "runtime_binding", return_value=runtime
            ),
            mock.patch.object(measurement.dev, "write_source", fake_write_source),
            mock.patch.object(
                measurement.dev, "verify_source_coverage", return_value=100
            ),
            mock.patch.object(measurement.dev, "_arena_revision", return_value=ARENA),
            mock.patch.object(measurement.dev, "_revision", return_value="b" * 40),
        ):
            try:
                return measurement.run(arguments, play=play), played
            except measurement.MeasurementSourceError as error:
                return error, played

    def test_run_generates_four_units_over_every_seed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document, played = self.run_with(directory)
            self.assertEqual(sorted(played), list(measurement.MEASUREMENT_SEEDS))
            self.assertEqual([unit["hanchan"] for unit in document["units"]], [100] * 4)
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
            written = json.loads(
                (Path(directory) / "out" / "generation.json").read_text("utf-8")
            )
            self.assertEqual(written, document)
            for unit in range(4):
                self.assertTrue(
                    (
                        Path(directory) / "out" / f"unit-{unit}" / "manifest.json"
                    ).exists()
                )

    def test_python_backend_and_bad_allocation_stop_before_any_game(self) -> None:
        other, record = reserved_ledger(population="another-population")
        for options in (
            {"backend": "python"},
            {"ledger": other, "identity": record["allocation_identity"]},
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as d:
                error, played = self.run_with(d, **options)
                self.assertIsInstance(error, measurement.MeasurementSourceError)
                self.assertEqual(played, [])
                self.assertFalse((Path(d) / "out").exists())

    def test_existing_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "out").mkdir()
            error, played = self.run_with(directory)
            self.assertIsInstance(error, measurement.MeasurementSourceError)
            self.assertEqual(played, [])


if __name__ == "__main__":
    unittest.main()
