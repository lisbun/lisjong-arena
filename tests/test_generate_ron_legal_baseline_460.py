"""lisjong#262 stage C ron-legal baseline generator (lisjong-arena#460).

Synthetic fixtures only: no formal seed is played.  Pins the population and split,
the Seed Registry guard (every mismatch fails closed before a game), the per-seed
receipts and archives, the all-or-nothing generation record, and the
failure / tamper / missing-file rejections.  The real native label builder is
exercised on a fixed-wall engine fixture and skipped without the native scorer.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lisjong_arena import seed_registry

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_ron_legal_baseline_460 as baseline  # noqa: E402

ARENA = "a" * 40
RUNTIME = {
    "python": "3.14.fixture",
    "platform": "Linux x86_64",
    "shanten_backend": "rust",
    "native_source_revision": "b" * 40,
    "scoring_api_version": 1,
}
PRODUCER = {
    "arena_revision": ARENA,
    "lisjong_revision": "b" * 40,
    "lisjong_engine_revision": "c" * 40,
    "policy": "PlacementAwareSpeedCallPolicy",
}
FAILING_SEED = 936100


def fake_play(seed):
    return ((seed, [], [], []), [], [], [])


def failing_play(seed):
    if seed == FAILING_SEED:
        raise baseline.BaselineSourceError("fixture failure")
    return fake_play(seed)


def fake_write(directory, games, splits, producer, rules):
    seed = games[0][0][0]
    for relative in baseline.SOURCE_FILES:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative} {seed} {splits}\n", encoding="utf-8")
    return {
        "coverage": [
            {
                "seed": seed,
                "round_id": str(ordinal),
                "transitions": 5,
                "reactions": 2,
                "decisions": 3,
                "selectors": 4,
            }
            for ordinal in (1, 2)
        ]
    }


def fake_check(directory):
    return {"decisions": 6, "labelled_decisions": 6}


def fake_runtime():
    return dict(RUNTIME)


def fake_producer(revision=ARENA):
    return dict(PRODUCER, arena_revision=revision)


def stages(**changes):
    values = dict(
        play=fake_play,
        write=fake_write,
        check=fake_check,
        runtime=fake_runtime,
        producer=fake_producer,
    )
    values.update(changes)
    return baseline.Stages(**values)


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": baseline.OWNER_ISSUE,
        "protocol": baseline.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": baseline.POPULATION,
        "split": baseline.SPLIT,
        "seeds": baseline.MEASUREMENT_SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": baseline.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-08T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


class PopulationTest(unittest.TestCase):
    def test_population_is_the_fixed_range_and_split(self) -> None:
        baseline.check_population()
        self.assertEqual(baseline.MEASUREMENT_SEEDS, tuple(range(936000, 936400)))
        self.assertEqual(baseline.TRAIN_SEEDS, tuple(range(936000, 936160)))
        self.assertEqual(baseline.VALID_SEEDS, tuple(range(936160, 936240)))
        self.assertEqual(baseline.EVAL_SEEDS, tuple(range(936240, 936400)))
        self.assertEqual(baseline.REQUIRED_WORKERS, 32)

    def test_split_names_follow_the_manifest_vocabulary(self) -> None:
        self.assertEqual(baseline.seed_split(936000), "train")
        self.assertEqual(baseline.seed_split(936159), "train")
        self.assertEqual(baseline.seed_split(936160), "valid")
        self.assertEqual(baseline.seed_split(936239), "valid")
        self.assertEqual(baseline.seed_split(936240), "test")
        self.assertEqual(baseline.seed_split(936399), "test")
        self.assertEqual(
            baseline.seed_splits(936200),
            {"train": [], "valid": [936200], "test": []},
        )
        for seed in (935001, 936400):
            with self.assertRaises(baseline.BaselineSourceError):
                baseline.seed_split(seed)

    def test_used_seeds_are_never_reused(self) -> None:
        for seeds in (
            range(933000, 933400),
            range(934000, 934200),
            range(935000, 935002),
        ):
            self.assertTrue(
                all(
                    any(seed in used for used in baseline.USED_RANGES) for seed in seeds
                )
            )
        with mock.patch.object(baseline, "USED_RANGES", (range(936399, 936500),)):
            with self.assertRaises(baseline.BaselineSourceError):
                baseline.check_population()

    def test_the_allocation_is_free_in_the_bootstrap_ledger(self) -> None:
        reserved_ledger()


class AuthorizeTest(unittest.TestCase):
    def test_matching_fresh_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        binding, authorized = baseline.authorize(
            ledger, record["allocation_identity"], arena_revision=ARENA
        )
        self.assertEqual(binding["allocation_identity"], record["allocation_identity"])
        self.assertEqual(authorized["state"], seed_registry.RESERVED)

    def test_every_mismatch_is_rejected(self) -> None:
        cases = {
            "owner_issue": "lisbun/lisjong#257",
            "protocol": "another-protocol",
            "population": "another-population",
            "split": "TEST",
            "seeds": tuple(range(936000, 936399)),
            "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                ledger, record = reserved_ledger(**{field: value})
                with self.assertRaises(baseline.BaselineSourceError):
                    baseline.authorize(
                        ledger, record["allocation_identity"], arena_revision=ARENA
                    )

    def test_revision_state_and_identity_are_rejected(self) -> None:
        ledger, record = reserved_ledger()
        identity = record["allocation_identity"]
        for revision in ("b" * 40, ARENA + "-dirty"):
            with self.subTest(revision=revision):
                with self.assertRaises(baseline.BaselineSourceError):
                    baseline.authorize(ledger, identity, arena_revision=revision)
        for state in (seed_registry.COMMITTED, seed_registry.RETIRED):
            with self.subTest(state=state):
                moved = seed_registry.transition_allocation(
                    ledger, identity, state=state
                )
                with self.assertRaises(baseline.BaselineSourceError):
                    baseline.authorize(moved, identity, arena_revision=ARENA)
        for bad in ("0" * 64, "xyz", 5):
            with self.subTest(identity=bad):
                with self.assertRaises(baseline.BaselineSourceError):
                    baseline.authorize(ledger, bad, arena_revision=ARENA)


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        ledger, record = reserved_ledger()
        self.identity = record["allocation_identity"]
        self.ledger_path = self.directory / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, ledger)
        self.output = self.directory / "output"
        self.archive = self.directory / "archive"

    def run_generation(self, *, workers=1, **changes):
        with contextlib.redirect_stdout(io.StringIO()):
            return baseline.run(
                seed_ledger=self.ledger_path,
                allocation_identity=self.identity,
                workers=workers,
                output=changes.pop("output", self.output),
                archive_dir=changes.pop("archive_dir", self.archive),
                stages=changes.pop("stages", stages()),
            )

    def collected(self) -> Path:
        shutil.copyfile(
            self.output / baseline.GENERATION_FILENAME,
            self.archive / baseline.GENERATION_FILENAME,
        )
        return self.archive


class RunTest(RunCase):
    def test_generation_covers_every_seed_once_with_a_verified_record(self) -> None:
        document = self.run_generation()
        self.assertEqual(
            document["totals"],
            {
                "hanchan": 400,
                "by_split": {"train": 160, "valid": 80, "test": 160},
                "decisions": 400 * 6,
                "rounds": 800,
                "reactions": 1600,
            },
        )
        self.assertEqual(
            [e["seed"] for e in document["entries"]],
            list(baseline.MEASUREMENT_SEEDS),
        )
        self.assertEqual(document["allocation"]["state"], seed_registry.RESERVED)
        self.assertEqual(document["producer"], PRODUCER)
        self.assertTrue((self.output / baseline.PLAN_FILENAME).exists())
        for seed in (936000, 936399):
            archive, receipt = baseline.archive_names(seed)
            self.assertTrue((self.archive / archive).exists())
            self.assertTrue((self.archive / receipt).exists())
            self.assertTrue((self.output / f"seed-{seed}" / "ron").is_dir())
        verified = baseline.verify_collected(self.collected())
        self.assertEqual(verified["totals"], document["totals"])

    def test_worker_processes_produce_the_same_record(self) -> None:
        document = self.run_generation(workers=2)
        self.assertEqual(document["workers"], 2)
        self.assertEqual(document["totals"]["hanchan"], 400)
        baseline.verify_collected(self.collected())

    def test_a_revision_or_allocation_mismatch_stops_before_any_game(self) -> None:
        played = []

        def spying_play(seed):
            played.append(seed)
            return fake_play(seed)

        for changes in (
            {"producer": lambda: fake_producer("b" * 40)},
            {"producer": lambda: fake_producer(ARENA + "-dirty")},
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(baseline.BaselineSourceError):
                    self.run_generation(stages=stages(play=spying_play, **changes))
        self.identity = "0" * 64
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation(stages=stages(play=spying_play))
        self.assertEqual(played, [])
        self.assertFalse(self.output.exists())

    def test_existing_output_and_existing_seed_files_are_refused(self) -> None:
        self.output.mkdir()
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation()
        self.output.rmdir()
        self.archive.mkdir()
        (self.archive / baseline.archive_names(936000)[1]).write_text("{}")
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation()
        self.assertFalse(self.output.exists())

    def test_cli_requires_exactly_32_workers(self) -> None:
        argv = [
            "run",
            "--seed-ledger",
            str(self.ledger_path),
            "--allocation-identity",
            self.identity,
            "--output",
            str(self.output),
            "--archive-dir",
            str(self.archive),
        ]
        for workers in ("1", "31", "33"):
            with self.subTest(workers=workers):
                self.assertEqual(baseline.main([*argv, "--workers", workers]), 1)
        self.assertFalse(self.output.exists())


class FailureTest(RunCase):
    def test_a_failing_seed_stops_the_run_and_the_next_run_is_refused(self) -> None:
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation(stages=stages(play=failing_play))
        self.assertFalse((self.output / baseline.GENERATION_FILENAME).exists())
        names = {p.name for p in self.archive.iterdir()}
        self.assertIn(baseline.archive_names(936000)[1], names)
        self.assertNotIn(baseline.archive_names(FAILING_SEED)[1], names)
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation(output=self.directory / "second", stages=stages())

    def test_a_failing_seed_stops_the_run_with_worker_processes(self) -> None:
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation(workers=2, stages=stages(play=failing_play))
        self.assertFalse((self.output / baseline.GENERATION_FILENAME).exists())

    def test_a_worker_with_another_runtime_fails_the_run(self) -> None:
        changed = dict(RUNTIME, native_source_revision="d" * 40)
        calls = iter([dict(RUNTIME), changed])
        with self.assertRaises(baseline.BaselineSourceError):
            self.run_generation(stages=stages(runtime=lambda: next(calls)))
        self.assertFalse((self.output / baseline.GENERATION_FILENAME).exists())

    def test_wrong_seed_or_reader_counts_fail_the_run(self) -> None:
        for changes in (
            {"play": lambda seed: ((seed + 1, [], [], []), [], [], [])},
            {"check": lambda directory: {"decisions": 6, "labelled_decisions": 5}},
        ):
            with self.subTest(changes=list(changes)):
                self.setUp()
                with self.assertRaises(baseline.BaselineSourceError):
                    self.run_generation(stages=stages(**changes))
                self.assertFalse((self.output / baseline.GENERATION_FILENAME).exists())


class VerifyTest(RunCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = Path(tempfile.mkdtemp())
        ledger, record = reserved_ledger()
        path = cls.template / "ledger.json"
        seed_registry.write_ledger(path, ledger)
        with contextlib.redirect_stdout(io.StringIO()):
            baseline.run(
                seed_ledger=path,
                allocation_identity=record["allocation_identity"],
                workers=1,
                output=cls.template / "output",
                archive_dir=cls.template / "archive",
                stages=stages(),
            )
        shutil.copyfile(
            cls.template / "output" / baseline.GENERATION_FILENAME,
            cls.template / "archive" / baseline.GENERATION_FILENAME,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.template, ignore_errors=True)

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.collected_directory = self.directory / "collected"
        shutil.copytree(self.template / "archive", self.collected_directory)

    def receipt_path(self, seed):
        return self.collected_directory / baseline.archive_names(seed)[1]

    def edit_receipt(self, seed, edit):
        path = self.receipt_path(seed)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        edit(receipt)
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def assert_rejected(self):
        with self.assertRaises(baseline.BaselineSourceError):
            baseline.verify_collected(self.collected_directory)

    def test_the_untouched_collection_verifies(self) -> None:
        baseline.verify_collected(self.collected_directory)

    def test_missing_files_are_rejected(self) -> None:
        for name in (
            baseline.archive_names(936050)[0],
            baseline.archive_names(936399)[1],
            baseline.GENERATION_FILENAME,
        ):
            with self.subTest(name=name):
                (self.collected_directory / name).rename(self.directory / "moved")
                self.assert_rejected()
                (self.directory / "moved").rename(self.collected_directory / name)

    def test_extra_or_partial_files_are_rejected(self) -> None:
        for name in (
            baseline.archive_names(936400)[0],
            ".partial-" + baseline.archive_names(936000)[0],
            baseline.ARCHIVE_PREFIX + "unit-0.tar.zst",
        ):
            with self.subTest(name=name):
                stray = self.collected_directory / name
                stray.write_bytes(b"x")
                self.assert_rejected()
                stray.unlink()

    def test_unrelated_files_beside_the_seed_files_are_allowed(self) -> None:
        (self.collected_directory / "progress.txt").write_text("log")
        (self.collected_directory / "environment").mkdir()
        baseline.verify_collected(self.collected_directory)

    def test_a_corrupt_archive_is_rejected(self) -> None:
        path = self.collected_directory / baseline.archive_names(936123)[0]
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assert_rejected()

    def test_receipt_that_disagrees_is_rejected(self) -> None:
        edits = {
            "seed": lambda r: r.update(seed=936001),
            "split": lambda r: r.update(split="valid"),
            "allocation": lambda r: r.update(allocation_identity="0" * 64),
            "producer": lambda r: r["producer"].update(arena_revision="b" * 40),
            "runtime": lambda r: r["runtime"].update(python="other"),
            "protocol": lambda r: r.update(protocol="another"),
            "decisions": lambda r: r.update(decisions=7),
            "labelled": lambda r: r.update(labelled_decisions=5),
            "file hash": lambda r: r["files"]["ron/ron_facts.jsonl"].update(
                sha256="0" * 64
            ),
            "archive hash": lambda r: r["archive"].update(sha256="0" * 64),
            "coverage seed": lambda r: r["coverage"][0].update(seed=936001),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                self.edit_receipt(936077, edit)
                self.assert_rejected()
                shutil.copyfile(
                    self.template / "archive" / baseline.archive_names(936077)[1],
                    self.receipt_path(936077),
                )

    def test_a_receipt_with_another_seeds_archive_is_rejected(self) -> None:
        source = self.collected_directory / baseline.archive_names(936011)[0]
        target = self.collected_directory / baseline.archive_names(936012)[0]
        shutil.copyfile(source, target)
        self.assert_rejected()

    def test_generation_record_must_match_the_files(self) -> None:
        path = self.collected_directory / baseline.GENERATION_FILENAME
        document = json.loads(path.read_text(encoding="utf-8"))
        for edit in (
            lambda d: d["totals"].update(decisions=1),
            lambda d: d["entries"].pop(),
            lambda d: d.update(split="TRAIN"),
            lambda d: d.update(population="another"),
            lambda d: d["entries"][3]["archive"].update(bytes=1),
        ):
            changed = json.loads(json.dumps(document))
            edit(changed)
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assert_rejected()
        path.write_text("not json", encoding="utf-8")
        self.assert_rejected()


class NativeTest(unittest.TestCase):
    def test_real_readers_and_label_builder_accept_a_fixed_wall_seed(self) -> None:
        from lisjong.belief.ron_legal_ground_truth import require_scoring_backend

        try:
            require_scoring_backend()
        except Exception as error:
            if os.environ.get("LISJONG_REQUIRE_NATIVE") == "1":
                raise
            self.skipTest(f"native scoring unavailable: {error}")
        from test_ron_legal_source_producer import fixture

        seed = 936000
        (_, decisions, hands, keys), facts, history, coverage = fixture(riichi=True)[
            0
        ].finish()

        def retag(rows, field=None):
            if field is None:
                return [{**row, "key": {**row["key"], "seed": seed}} for row in rows]
            return [{**row, "seed": seed} for row in rows]

        game = (
            (seed, retag(decisions), retag(hands), keys),
            retag(facts),
            retag(history, "seed"),
            retag(coverage, "seed"),
        )
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            archive = root / "archive"
            archive.mkdir()
            task = {
                "seed": seed,
                "conditions": {
                    "owner_issue": baseline.OWNER_ISSUE,
                    "protocol": baseline.PROTOCOL,
                    "population": baseline.POPULATION,
                    "allocation_identity": "e" * 64,
                    "producer": PRODUCER,
                    "runtime": RUNTIME,
                },
                "source_dir": str(root / "seed"),
                "archive_dir": str(archive),
            }
            real = stages(
                play=lambda requested: game,
                write=baseline.REAL_STAGES.write,
                check=baseline.REAL_STAGES.check,
            )
            baseline.generate_seed(task, real)
            receipt = json.loads(
                (archive / baseline.archive_names(seed)[1]).read_text(encoding="utf-8")
            )
            baseline.check_receipt(receipt, seed, task["conditions"])
            baseline.check_archive(archive / receipt["archive"]["name"], seed, receipt)
            self.assertGreater(receipt["decisions"], 0)
            self.assertEqual(receipt["decisions"], receipt["labelled_decisions"])


if __name__ == "__main__":
    unittest.main()
