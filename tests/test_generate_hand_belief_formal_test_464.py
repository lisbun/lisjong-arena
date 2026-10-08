"""lisjong#258 / #259 shared formal-test population generator (lisjong-arena#464).

Pins the fixed test-only population and its two units, the Seed Registry
authorization through the shared guard (every mismatch fails closed before a
game is played), the rust backend requirement, the per-unit archive, the
generation record, what lisjong's population / producer checks say about the
written sources, and the byte comparison with a reference source.  Hanchan
execution is replaced by fixture games; the v1 writer is the real one.
"""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lisjong_arena import seed_registry

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import generate_hand_belief_formal_test_464 as formal  # noqa: E402

ARENA = "a" * 40
PRODUCER = {
    "arena_revision": ARENA,
    "lisjong_engine_revision": "c" * 40,
    "lisjong_revision": "b" * 40,
    "policy": "PlacementAwareSpeedCallPolicy",
}
REGISTERED_257 = {
    "arena_revision": "07554c24bfcabb2ded6cf81991b9ea58a20bc74d",
    "lisjong_engine_revision": "8735e89e1aea000ab59368d0368d476787827741",
    "lisjong_revision": "e6346ed2bb9e992138c05c4be367bd6a05ed00bc",
    "policy": "PlacementAwareSpeedCallPolicy",
}


def fixture_game(seed: int, mark: str = "fixture"):
    """One in-scope decision of seat 0 with the three opponent hands."""
    key = {"seed": seed, "seat": 0, "sequence": 1}
    decisions = [{"key": key, "mark": mark}]
    facts = [
        {
            "key": key,
            "opponents": [{"seat": seat, "sequence": 1} for seat in (1, 2, 3)],
        }
    ]
    return seed, decisions, facts, [(0, 1)]


def fake_play(seed: int):
    return fixture_game(seed), {"wall_seconds": 1.5, "cpu_seconds": 1.25}


def runtime(backend="rust", revision=ARENA):
    return {"arena_revision": revision, "shanten_backend": {"name": backend}}


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": formal.OWNER_ISSUE,
        "protocol": formal.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": formal.POPULATION,
        "split": formal.SPLIT,
        "seeds": formal.TEST_SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": formal.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-08T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


class PopulationTest(unittest.TestCase):
    def test_population_is_the_fixed_test_only_range(self) -> None:
        formal.check_population()
        self.assertEqual(formal.TEST_SEEDS, tuple(range(934000, 934200)))
        self.assertEqual(formal.REQUIRED_WORKERS, 32)
        preset = formal.allocation_preset()
        self.assertEqual(
            preset.splits, (("train", ()), ("valid", ()), ("test", formal.TEST_SEEDS))
        )
        self.assertEqual(preset.split_sizes, (0, 0, 200))

    def test_units_are_two_consecutive_hundreds_of_test_seeds(self) -> None:
        self.assertEqual(
            formal.unit_splits(0),
            {"train": [], "valid": [], "test": list(range(934000, 934100))},
        )
        self.assertEqual(
            formal.unit_splits(1),
            {"train": [], "valid": [], "test": list(range(934100, 934200))},
        )
        for unit in (-1, 2):
            with self.assertRaises(formal.FormalTestSourceError):
                formal.unit_splits(unit)

    def test_used_seeds_are_never_reused(self) -> None:
        for seeds in (
            range(933000, 933400),
            range(935000, 935002),
            range(936000, 936400),
        ):
            self.assertTrue(
                all(any(seed in used for used in formal.USED_RANGES) for seed in seeds)
            )
        with mock.patch.object(formal, "USED_RANGES", (range(934199, 934300),)):
            with self.assertRaises(formal.FormalTestSourceError):
                formal.check_population()

    def test_a_changed_split_or_unit_layout_is_rejected(self) -> None:
        with mock.patch.object(formal, "TEST_SEEDS", tuple(range(934000, 934199))):
            with self.assertRaises(formal.FormalTestSourceError):
                formal.check_population()
        with mock.patch.object(formal, "UNIT_SIZE", 99):
            with self.assertRaises(formal.FormalTestSourceError):
                formal.check_population()
        with mock.patch.object(formal, "REFERENCE_SEEDS", range(933000, 934001)):
            with self.assertRaises(formal.FormalTestSourceError):
                formal.check_population()

    def test_the_pilot_producer_still_refuses_these_seeds(self) -> None:
        with self.assertRaises(Exception):
            formal.dev._seed_range("934000..934001")

    def test_the_allocation_is_free_in_the_bootstrap_ledger(self) -> None:
        reserved_ledger()


class AuthorizeTest(unittest.TestCase):
    def test_matching_fresh_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        binding, authorized = formal.authorize(
            ledger, record["allocation_identity"], arena_revision=ARENA
        )
        self.assertEqual(binding["allocation_identity"], record["allocation_identity"])
        self.assertEqual(authorized["state"], seed_registry.RESERVED)
        self.assertEqual(
            seed_registry.seeds_from_membership(authorized["seed_membership"]),
            formal.TEST_SEEDS,
        )

    def test_every_allocation_mismatch_is_rejected(self) -> None:
        cases = {
            "owner_issue": "lisbun/lisjong#257",
            "protocol": "hand-belief-accuracy-baseline-measurement-v1",
            "population": "another-population",
            "split": "TRAIN-VALID-EVAL",
            "seeds": tuple(range(934000, 934199)),
            "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                ledger, record = reserved_ledger(**{field: value})
                with self.assertRaises(formal.FormalTestSourceError):
                    formal.authorize(
                        ledger, record["allocation_identity"], arena_revision=ARENA
                    )

    def test_revision_state_and_identity_are_rejected(self) -> None:
        ledger, record = reserved_ledger()
        identity = record["allocation_identity"]
        for revision in ("d" * 40, ARENA + "-dirty"):
            with self.subTest(revision=revision):
                with self.assertRaises(formal.FormalTestSourceError):
                    formal.authorize(ledger, identity, arena_revision=revision)
        for state in (seed_registry.COMMITTED, seed_registry.RETIRED):
            with self.subTest(state=state):
                moved = seed_registry.transition_allocation(
                    ledger, identity, state=state
                )
                with self.assertRaises(formal.FormalTestSourceError):
                    formal.authorize(moved, identity, arena_revision=ARENA)
        for bad in ("0" * 64, "not-a-digest"):
            with self.subTest(identity=bad):
                with self.assertRaises(formal.FormalTestSourceError):
                    formal.authorize(ledger, bad, arena_revision=ARENA)


class RunCase(unittest.TestCase):
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
        fail_at=None,
        producer_revision=ARENA,
        runtime_revision=ARENA,
    ):
        """Run into ``<root>/<name>``; returns (document or error, played seeds)."""
        ledger_path = self.root / f"{name}-ledger.json"
        ledger_path.write_text(json.dumps(ledger or self.ledger), encoding="utf-8")
        played = []

        def play(seed):
            if seed == fail_at:
                raise RuntimeError("engine failure")
            played.append(seed)
            return fake_play(seed)

        with (
            mock.patch.object(
                formal, "current_runtime", lambda: runtime(backend, runtime_revision)
            ),
            mock.patch.object(
                formal,
                "current_producer",
                lambda: {**PRODUCER, "arena_revision": producer_revision},
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                document = formal.run(
                    seed_ledger=ledger_path,
                    allocation_identity=identity or self.identity,
                    workers=1,
                    output=self.root / name / "generated",
                    archive_dir=self.root / name / "archive",
                    play=play,
                )
            except (formal.FormalTestSourceError, RuntimeError) as error:
                return error, played
        return document, played

    def archive(self, name="first"):
        return sorted(path.name for path in (self.root / name / "archive").iterdir())


class RunTest(RunCase):
    def test_run_generates_both_units_over_every_seed_once(self) -> None:
        document, played = self.run_with()
        self.assertEqual(played, list(formal.TEST_SEEDS))
        self.assertEqual([unit["hanchan"] for unit in document["units"]], [100, 100])
        self.assertEqual(
            [unit["seeds"] for unit in document["units"]],
            [[934000, 934099], [934100, 934199]],
        )
        self.assertEqual(
            document["splits"], {"train": [], "valid": [], "test": [934000, 934199]}
        )
        self.assertEqual(document["seeds"]["count"], 200)
        self.assertEqual(document["producer"], PRODUCER)
        self.assertEqual(document["allocation"]["state"], seed_registry.RESERVED)
        self.assertEqual(document["units"][0]["games"][0]["cpu_seconds"], 1.25)
        generated = self.root / "first" / "generated"
        written = json.loads((generated / "generation.json").read_text("utf-8"))
        self.assertEqual(written, document)
        self.assertEqual(
            self.archive(),
            sorted(
                f"progress-unit-{unit}.{suffix}"
                for unit in range(2)
                for suffix in ("tar.zst", "complete.json")
            ),
        )

    def test_each_unit_record_matches_its_archive_and_source(self) -> None:
        document, _ = self.run_with()
        archive_dir = self.root / "first" / "archive"
        for unit in range(2):
            with self.subTest(unit=unit):
                archive_name, record_name = formal.archive_names(unit)
                record = json.loads((archive_dir / record_name).read_text("utf-8"))
                self.assertEqual(record, document["units"][unit])
                self.assertEqual(
                    record["archive"],
                    {
                        "name": archive_name,
                        **formal.guard.file_digest(archive_dir / archive_name),
                    },
                )
                for field, value in (
                    ("owner_issue", formal.OWNER_ISSUE),
                    ("protocol", formal.PROTOCOL),
                    ("population", formal.POPULATION),
                    ("allocation_identity", self.identity),
                    ("producer", PRODUCER),
                    ("workers", 1),
                ):
                    self.assertEqual(record[field], value)
                with tarfile.open(archive_dir / archive_name, "r:zst") as archive:
                    self.assertEqual(
                        archive.getnames(),
                        [f"unit-{unit}/{name}" for name in formal.SOURCE_FILES],
                    )
                source = self.root / "first" / "generated" / f"unit-{unit}"
                for name in formal.SOURCE_FILES:
                    self.assertEqual(
                        record["files"][name], formal.guard.file_digest(source / name)
                    )

    def test_a_precondition_failure_stops_before_any_game(self) -> None:
        other, record = reserved_ledger(population="another-population")
        committed = seed_registry.transition_allocation(
            self.ledger, self.identity, state=seed_registry.COMMITTED
        )
        cases = (
            {"backend": "python"},
            {"ledger": other, "identity": record["allocation_identity"]},
            {"ledger": committed},
            {"producer_revision": ARENA + "-dirty"},
            {"producer_revision": "d" * 40},
            {"runtime_revision": "d" * 40},
        )
        for index, options in enumerate(cases):
            with self.subTest(options=sorted(options)):
                error, played = self.run_with(f"stop-{index}", **options)
                self.assertIsInstance(error, formal.FormalTestSourceError)
                self.assertEqual(played, [])
                self.assertFalse((self.root / f"stop-{index}").exists())

    def test_existing_output_and_existing_unit_files_are_refused(self) -> None:
        (self.root / "first" / "generated").mkdir(parents=True)
        error, played = self.run_with()
        self.assertIsInstance(error, formal.FormalTestSourceError)
        self.assertEqual(played, [])
        for name in (
            "progress-unit-1.complete.json",
            ".partial-progress-unit-0.tar.zst",
        ):
            with self.subTest(name=name):
                archive = self.root / name / "archive"
                archive.mkdir(parents=True)
                (archive / name).write_text("{}", encoding="utf-8")
                error, played = self.run_with(name)
                self.assertIsInstance(error, formal.FormalTestSourceError)
                self.assertEqual(played, [])
                self.assertFalse((self.root / name / "generated").exists())

    def test_a_failed_unit_leaves_no_generation_record_and_no_rerun(self) -> None:
        error, played = self.run_with(fail_at=934150)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(played, list(range(934000, 934150)))
        self.assertFalse(
            (self.root / "first" / "generated" / "generation.json").exists()
        )
        self.assertEqual(
            self.archive(), ["progress-unit-0.complete.json", "progress-unit-0.tar.zst"]
        )
        # Nothing is resumed or reused: the same directories are refused.
        error, played = self.run_with()
        self.assertIsInstance(error, formal.FormalTestSourceError)
        self.assertEqual(played, [])

    def test_cli_requires_exactly_32_workers_and_reports_a_stop(self) -> None:
        for workers in ("1", "31", "64"):
            with self.subTest(workers=workers):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = formal.main(
                        [
                            "run",
                            "--seed-ledger",
                            str(self.root / "ledger.json"),
                            "--allocation-identity",
                            self.identity,
                            "--workers",
                            workers,
                            "--output",
                            str(self.root / "cli"),
                            "--archive-dir",
                            str(self.root / "cli-archive"),
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("STOP / INVALID: workers must be 32", stderr.getvalue())
                self.assertFalse((self.root / "cli").exists())

    def test_cli_check_allocation_plays_no_game(self) -> None:
        ledger_path = self.root / "ledger.json"
        ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
        arguments = [
            "check-allocation",
            "--seed-ledger",
            str(ledger_path),
            "--allocation-identity",
            self.identity,
            "--arena-revision",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(formal.main([*arguments, ARENA]), 0)
        checked = json.loads(stdout.getvalue())
        self.assertEqual(checked["allocation"]["allocation_identity"], self.identity)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(formal.main([*arguments, "d" * 40]), 1)


class LisjongCheckTest(RunCase):
    """What lisjong's own checks say about the generated sources (manifest only)."""

    def setUp(self) -> None:
        super().setUp()
        from lisjong.learning import hand_belief_accuracy

        self.accuracy = hand_belief_accuracy
        self.run_with()
        self.sources = [self.root / "first" / "generated" / f"unit-{k}" for k in (0, 1)]
        self.expected = {"train": [], "valid": [], "eval": list(formal.TEST_SEEDS)}

    def test_the_population_check_accepts_the_two_units(self) -> None:
        identity = self.accuracy.check_population(self.sources, self.expected)
        self.assertEqual(identity["producer"], PRODUCER)

    def test_a_missing_unit_is_rejected_by_the_population_check(self) -> None:
        with self.assertRaises(Exception):
            self.accuracy.check_population(self.sources[:1], self.expected)

    def test_the_producer_check_needs_this_producer_to_be_registered(self) -> None:
        identity = self.accuracy.check_population(self.sources, self.expected)
        # Registered as the generating producer: accepted.
        self.accuracy.check_producer(identity, dict(PRODUCER))
        # Against the lisjong#257 producer of the #258 / #259 selections: the
        # Arena, lisjong and lisjong-engine revisions all differ, so lisjong
        # rejects the population until it registers this producer.
        with self.assertRaisesRegex(
            Exception, "arena_revision, lisjong_engine_revision, lisjong_revision"
        ):
            self.accuracy.check_producer(identity, REGISTERED_257)


class CompareReferenceTest(unittest.TestCase):
    SEEDS = [933000, 933001, 933240]

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.references = [self.reference("unit-a", [933000, 933001], [933240])]

    def reference(self, name, train, test, producer=REGISTERED_257):
        directory = self.root / name
        formal.dev.write_source(
            directory,
            splits={"train": train, "valid": [], "test": test},
            games=[fixture_game(seed) for seed in train + test],
            producer=producer,
        )
        return directory

    def compare(self, seeds=None, *, references=None, play=fake_play, name="report"):
        return formal.compare_reference(
            references=references or self.references,
            seeds=self.SEEDS if seeds is None else seeds,
            workers=1,
            output=self.root / f"{name}.json",
            play=play,
            runtime=lambda: runtime("python"),
            producer=lambda: dict(PRODUCER),
        )

    def test_identical_rows_are_reported_with_both_producers(self) -> None:
        document = self.compare()
        self.assertTrue(document["identical"])
        self.assertEqual(document["reference_producer"], REGISTERED_257)
        self.assertEqual(document["current_producer"], PRODUCER)
        self.assertEqual(
            document["differing_producer_fields"],
            ["arena_revision", "lisjong_engine_revision", "lisjong_revision"],
        )
        self.assertEqual(document["runtime"]["shanten_backend"], {"name": "python"})
        self.assertEqual([row["seed"] for row in document["seeds"]], self.SEEDS)
        for row in document["seeds"]:
            self.assertTrue(row["identical"])
            self.assertEqual(row["reference"], row["regenerated"])
            self.assertEqual(row["reference"]["decisions"]["rows"], 1)
        written = json.loads((self.root / "report.json").read_text("utf-8"))
        self.assertEqual(written, document)

    def test_a_differing_seed_is_reported_and_fails_the_comparison(self) -> None:
        def play(seed):
            mark = "changed" if seed == 933001 else "fixture"
            return fixture_game(seed, mark), {}

        document = self.compare(play=play)
        self.assertFalse(document["identical"])
        self.assertEqual(
            [row["identical"] for row in document["seeds"]], [True, False, True]
        )
        changed = document["seeds"][1]
        self.assertNotEqual(
            changed["reference"]["decisions"], changed["regenerated"]["decisions"]
        )
        self.assertEqual(
            changed["reference"]["hand_facts"], changed["regenerated"]["hand_facts"]
        )

    def test_seeds_may_come_from_several_references(self) -> None:
        second = self.reference("unit-b", [933002], [])
        document = self.compare([933001, 933002], references=[*self.references, second])
        self.assertTrue(document["identical"])

    def test_only_used_reference_seeds_are_replayed(self) -> None:
        played = []

        def play(seed):
            played.append(seed)
            return fake_play(seed)

        for index, seeds in enumerate(
            ([], [934000], [932099], [933400], [933000, 933000], [936000])
        ):
            with self.subTest(seeds=seeds):
                with self.assertRaises(formal.FormalTestSourceError):
                    self.compare(seeds, play=play, name=f"bad-{index}")
                self.assertFalse((self.root / f"bad-{index}.json").exists())
        self.assertEqual(played, [])

    def test_a_bad_reference_stops_before_any_game(self) -> None:
        played = []

        def play(seed):
            played.append(seed)
            return fake_play(seed)

        other = self.reference("other", [933002], [], producer=PRODUCER)
        duplicate = self.reference("duplicate", [933000], [])
        unrelated = self.reference("unrelated", [933100], [])
        changed = self.reference("changed", [933003], [])
        with open(changed / "decisions.jsonl", "ab") as stream:
            stream.write(b"\n")
        cases = {
            "missing-seed": ([933000, 933050], self.references),
            "two-producers": ([933000, 933002], [*self.references, other]),
            "seed-twice": ([933000], [*self.references, duplicate]),
            "unused-reference": ([933000], [*self.references, unrelated]),
            "changed-file": ([933003], [changed]),
            "no-manifest": ([933000], [self.root / "absent"]),
        }
        for name, (seeds, references) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(formal.FormalTestSourceError):
                    self.compare(seeds, references=references, play=play, name=name)
                self.assertFalse((self.root / f"{name}.json").exists())
        self.assertEqual(played, [])

    def test_an_existing_report_is_not_overwritten(self) -> None:
        self.compare()
        with self.assertRaises(formal.FormalTestSourceError):
            self.compare()

    def test_cli_exit_code_follows_the_comparison(self) -> None:
        arguments = [
            "compare-reference",
            "--reference",
            str(self.references[0]),
            "--seeds",
            "933000..933001",
            "--output",
        ]

        def changed(seed):
            return fixture_game(seed, "changed"), {}

        for name, play, expected in (("same", fake_play, 0), ("other", changed, 1)):
            with (
                self.subTest(name=name),
                mock.patch.object(formal, "current_runtime", lambda: runtime("python")),
                mock.patch.object(formal, "current_producer", lambda: dict(PRODUCER)),
                mock.patch.object(formal, "_play_one", play),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = formal.main([*arguments, str(self.root / f"{name}.json")])
                self.assertEqual(code, expected)


if __name__ == "__main__":
    unittest.main()
