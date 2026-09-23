"""#362 C2 operator driver (scripts/aws/generate_l03_c2_362.py).

The driver is operational tooling that runs outside the frozen checkout. These
tests pin that parallel execution is operational only: output is reassembled in
canonical game ordinal order, a failed game publishes nothing, and a real
parallel run is byte-identical to the unchanged sequential producer. Only
test-only 900000-range seeds are used.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from _focal_outcome_source_fixtures import binding, source_contract

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import source as producer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "aws"))
import generate_l03_c2_362 as driver  # noqa: E402

SMOKE_SEEDS = (900000, 900001)


def _thread_pool(max_workers, mp_context=None):
    return ThreadPoolExecutor(max_workers=max_workers)


def _population(count):
    seeds = list(range(900000, 900000 + count))
    games = [(seed, "TRAIN") for seed in seeds]
    return games, {"TRAIN": binding(seeds)}


class GenerateParallelOrderingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.destination = Path(directory.name) / "source"

    def _run(self, write_game, count=6, workers=3):
        games, bindings = _population(count)
        captured = {}

        def build_manifest(**kwargs):
            captured["summaries"] = kwargs["game_summaries"]
            return {"manifest": True}

        def verify(path):
            captured["verified"] = Path(path).name
            return "verified"

        with (
            patch.object(driver, "ProcessPoolExecutor", _thread_pool),
            patch.object(producer, "run_focal_game", lambda **kw: kw),
            patch.object(producer, "write_game", write_game),
            patch.object(producer, "build_manifest", build_manifest),
            patch.object(driver, "write_document", lambda path, doc: None),
            patch.object(producer, "verify_focal_outcome_source", verify),
        ):
            result = driver.generate_parallel(
                self.destination,
                population_role=producer.SCIENTIFIC_ROLE,
                games=games,
                allocation_bindings=bindings,
                source_contract=source_contract(),
                workers=workers,
            )
        return result, captured

    def test_summaries_are_reassembled_in_canonical_ordinal_order(self):
        completion = []
        lock = threading.Lock()

        def write_game(path, execution, *, game_ordinal, seed, split):
            time.sleep(0.02 * (6 - game_ordinal))  # later ordinals finish first
            with lock:
                completion.append(game_ordinal)
            return {"game_ordinal": game_ordinal, "seed": seed, "split": split}

        result, captured = self._run(write_game)
        self.assertEqual(result, "verified")
        self.assertNotEqual(completion, sorted(completion))
        self.assertEqual(
            [summary["game_ordinal"] for summary in captured["summaries"]],
            list(range(6)),
        )
        self.assertEqual(
            [summary["seed"] for summary in captured["summaries"]],
            list(range(900000, 900006)),
        )
        self.assertEqual(captured["verified"], ".source.partial")
        self.assertTrue(self.destination.is_dir())

    def test_focal_seat_is_derived_from_the_global_ordinal(self):
        seats = {}

        def write_game(path, execution, *, game_ordinal, seed, split):
            seats[game_ordinal] = execution["focal_seat"]
            return {"game_ordinal": game_ordinal}

        self._run(write_game)
        self.assertEqual(
            {k: int(v) for k, v in seats.items()}, {i: i % 4 for i in range(6)}
        )

    def test_one_failed_game_publishes_nothing(self):
        def write_game(path, execution, *, game_ordinal, seed, split):
            if game_ordinal == 2:
                raise RuntimeError("worker failure")
            return {"game_ordinal": game_ordinal}

        with self.assertRaisesRegex(RuntimeError, "worker failure"):
            self._run(write_game)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_name(".source.partial").exists())

    def test_refuses_existing_destination_and_invalid_workers(self):
        games, bindings = _population(2)
        kwargs = dict(
            population_role=producer.SCIENTIFIC_ROLE,
            games=games,
            allocation_bindings=bindings,
            source_contract=source_contract(),
        )
        for workers in (0, 33, True):
            with self.assertRaises(driver.C2DriverError):
                driver.generate_parallel(self.destination, workers=workers, **kwargs)
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            driver.generate_parallel(self.destination, workers=2, **kwargs)


class C2PopulationTest(unittest.TestCase):
    """The frozen 400 / 100 shape bound to the live authority (test ledger only)."""

    def setUp(self):
        ledger = json.loads(
            (Path(seed_registry.__file__).with_name("seed-ledger.json")).read_text(
                encoding="utf-8"
            )
        )
        self.request = {}
        self.snapshots = {}
        for split, first, last in (
            ("TRAIN", 900000, 900399),
            ("SELECT", 900400, 900499),
        ):
            ledger, record = seed_registry.reserve_allocation(
                ledger,
                owner_issue=driver.OWNER_ISSUE,
                protocol=driver.PROTOCOL,
                seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
                purpose=f"test-only {split}",
                population=driver.POPULATION,
                split=split,
                seeds=list(range(first, last + 1)),
                arena_revision=driver.FROZEN_ARENA_REVISION,
                protocol_revision=driver.FROZEN_ARENA_REVISION,
                provenance_reference="test-only",
                allocation_timestamp="2026-09-23T00:00:00Z",
            )
            self.snapshots[split] = ledger
            self.request[split] = {
                "binding": seed_registry.allocation_binding(
                    ledger, record["allocation_identity"]
                ),
                "first": first,
                "last": last,
            }
        self.ledger = ledger

    def test_train_then_select_in_one_global_sequence(self):
        games, bindings = driver.c2_population(self.request, self.ledger)
        self.assertEqual(len(games), 500)
        self.assertEqual([split for _, split in games[:400]], ["TRAIN"] * 400)
        self.assertEqual([split for _, split in games[400:]], ["SELECT"] * 100)
        self.assertEqual([seed for seed, _ in games], list(range(900000, 900500)))
        self.assertEqual(set(bindings), {"TRAIN", "SELECT"})

    def test_resized_or_swapped_population_fails_closed(self):
        resized = json.loads(json.dumps(self.request))
        resized["SELECT"]["last"] = 900498
        swapped = {"TRAIN": self.request["SELECT"], "SELECT": self.request["TRAIN"]}
        missing = {"TRAIN": self.request["TRAIN"]}
        for request in (resized, swapped, missing):
            with self.assertRaises(driver.C2DriverError):
                driver.c2_population(request, self.ledger)

    def test_build_request_uses_each_authorizing_snapshot(self):
        allocations = {
            split: entry["binding"]["allocation_identity"]
            for split, entry in self.request.items()
        }
        request = driver.build_request(self.snapshots, allocations)
        self.assertEqual(request, self.request)
        driver.c2_population(request, self.ledger)

    def test_foreign_or_mismatched_binding_fails_closed(self):
        foreign = json.loads(json.dumps(self.request))
        foreign["SELECT"]["binding"] = binding(range(900400, 900500))
        with self.assertRaises(driver.C2DriverError):
            driver.c2_population(foreign, self.ledger)


class FrozenSourceContractTest(unittest.TestCase):
    def test_non_frozen_arena_revision_fails_closed(self):
        project = Path(producer.__file__).resolve().parents[3] / "pyproject.toml"
        contract = dict(source_contract(), arena_revision="f" * 40)
        with patch.object(producer, "build_source_contract", return_value=contract):
            with self.assertRaisesRegex(driver.C2DriverError, "frozen revision"):
                driver._frozen_source_contract(project)

    def test_producer_outside_the_checkout_fails_closed(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(driver.C2DriverError, "frozen checkout"):
                driver._frozen_source_contract(Path(other) / "pyproject.toml")


class OperatorScriptSyntaxTest(unittest.TestCase):
    def test_scripts_parse(self):
        aws = Path(__file__).resolve().parents[1] / "scripts" / "aws"
        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            script = aws / "run-l03-c2-362.ps1"
            command = (
                "$errors = $null; [System.Management.Automation.Language.Parser]"
                f"::ParseFile('{script}', [ref]$null, [ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
            )
            completed = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if bash is not None:
            completed = subprocess.run(
                [bash, "-n", str(aws / "bootstrap-l03-c2-362.sh")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


class SequentialParallelEquivalenceTest(unittest.TestCase):
    """Real RiichiEnv: spawn-parallel output is byte-identical to the producer."""

    def test_parallel_source_is_byte_identical_to_sequential(self):
        games = [(SMOKE_SEEDS[0], "TRAIN"), (SMOKE_SEEDS[1], "SELECT")]
        bindings = {
            "TRAIN": binding([SMOKE_SEEDS[0]]),
            "SELECT": binding([SMOKE_SEEDS[1]]),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = dict(
                population_role=producer.SCIENTIFIC_ROLE,
                games=games,
                allocation_bindings=bindings,
                source_contract=source_contract(),
            )
            sequential = producer.generate_focal_outcome_source(
                root / "sequential", **common
            )
            parallel = driver.generate_parallel(root / "parallel", workers=2, **common)
            self.assertEqual(parallel.identity, sequential.identity)
            files = sorted(
                p.relative_to(root / "sequential")
                for p in (root / "sequential").rglob("*")
                if p.is_file()
            )
            self.assertEqual(
                files,
                sorted(
                    p.relative_to(root / "parallel")
                    for p in (root / "parallel").rglob("*")
                    if p.is_file()
                ),
            )
            for relative in files:
                self.assertEqual(
                    (root / "parallel" / relative).read_bytes(),
                    (root / "sequential" / relative).read_bytes(),
                    relative,
                )


if __name__ == "__main__":
    unittest.main()
