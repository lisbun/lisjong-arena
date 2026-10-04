"""lisjong#245 formal test-source generator (scripts/generate_riichi_wait_formal_source_245.py).

Pins the protocol seed population, the Seed Registry authorization (every mismatch
fails closed), the all-or-nothing generation (missing / repeated / unexpected seeds,
worker failure), the empty-train / empty-valid manifest and the generation record.
Hanchan execution is replaced by small fake games.
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
import generate_riichi_wait_formal_source_245 as formal  # noqa: E402

ARENA = "a" * 40
LISJONG = "b" * 40
ENGINE = "c" * 40


def fake_game(seed: int):
    decisions = [
        {"key": {"seat": 0, "seed": seed, "sequence": n}, "row": n} for n in range(3)
    ]
    facts = [
        {"key": {"seat": 0, "seed": seed, "sequence": n}, "fact": n} for n in range(3)
    ]
    stats = {"wall_seconds": 1.5, "cpu_seconds": 1.25, "maxrss_kb": 1000}
    return seed, decisions, facts, stats


def failing_play(seed: int):
    if seed == 932003:
        raise RuntimeError("engine failure")
    return fake_game(seed)


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": formal.OWNER_ISSUE,
        "protocol": formal.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": formal.POPULATION,
        "split": formal.SPLIT,
        "seeds": formal.FORMAL_SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": formal.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-04T00:00:00.000000Z",
    }
    parameters.update(overrides)
    ledger, record = seed_registry.reserve_allocation(
        seed_registry.load_ledger(), **parameters
    )
    return ledger, record


class PopulationTest(unittest.TestCase):
    def test_formal_population_is_the_preregistered_range(self) -> None:
        formal.check_formal_population()
        self.assertEqual(formal.FORMAL_SEEDS, tuple(range(932000, 932100)))

    def test_formal_population_excludes_s1_and_development_seeds(self) -> None:
        seeds = set(formal.FORMAL_SEEDS)
        self.assertFalse(seeds & formal.S1_SEEDS)
        self.assertFalse(seeds & set(formal.DEVELOPMENT_RANGE))
        self.assertTrue(set(range(931100, 931140)) <= set(formal.DEVELOPMENT_RANGE))

    def test_smoke_accepts_only_development_seeds(self) -> None:
        self.assertEqual(formal.smoke_seeds("931100..931102"), (931100, 931101, 931102))
        self.assertEqual(formal.smoke_seeds("931139"), (931139,))
        for spec in (
            "932000",
            "932000..932099",
            "931099..931100",
            "931139..931140",
            "931200..931201",
            "931105..931101",
            "x..y",
        ):
            with self.subTest(spec=spec), self.assertRaises(formal.FormalSourceError):
                formal.smoke_seeds(spec)

    def test_the_allocation_is_free_in_the_live_ledger_and_domain(self) -> None:
        # The planned reservation does not collide with the bootstrap authority.
        reserved_ledger()


class AuthorizeTest(unittest.TestCase):
    def authorize(self, ledger, identity, revision=ARENA):
        return formal.authorize(ledger, identity, arena_revision=revision)

    def test_matching_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        binding, authorized = self.authorize(ledger, record["allocation_identity"])
        self.assertEqual(binding["allocation_identity"], record["allocation_identity"])
        self.assertEqual(authorized["state"], seed_registry.RESERVED)
        self.assertEqual(
            seed_registry.seeds_from_membership(authorized["seed_membership"]),
            formal.FORMAL_SEEDS,
        )

    def test_committed_allocation_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        ledger = seed_registry.transition_allocation(
            ledger, record["allocation_identity"], state=seed_registry.COMMITTED
        )
        self.authorize(ledger, record["allocation_identity"])

    def test_every_mismatch_is_rejected(self) -> None:
        cases = {
            "owner issue": {"owner_issue": "lisbun/lisjong#246"},
            "protocol": {"protocol": "another-protocol-v1"},
            "population": {"population": "another-population"},
            "split": {"split": "TRAIN"},
            "seed domain": {
                "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN
            },
            "fewer seeds": {"seeds": range(932000, 932099)},
            "shifted seeds": {"seeds": range(932001, 932101)},
            "more seeds": {"seeds": range(932000, 932101)},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                ledger, record = reserved_ledger(**overrides)
                with self.assertRaises(formal.FormalSourceError):
                    self.authorize(ledger, record["allocation_identity"])

    def test_arena_revision_must_be_the_executing_checkout(self) -> None:
        ledger, record = reserved_ledger(arena_revision="d" * 40)
        with self.assertRaisesRegex(formal.FormalSourceError, "arena_revision"):
            self.authorize(ledger, record["allocation_identity"])

    def test_retired_allocation_is_rejected(self) -> None:
        ledger, record = reserved_ledger()
        ledger = seed_registry.transition_allocation(
            ledger, record["allocation_identity"], state=seed_registry.RETIRED
        )
        with self.assertRaisesRegex(formal.FormalSourceError, "not authorized"):
            self.authorize(ledger, record["allocation_identity"])

    def test_unknown_or_malformed_identity_is_rejected(self) -> None:
        ledger, _ = reserved_ledger()
        for identity in ("0" * 64, "abc", "A" * 64, "", None):
            with (
                self.subTest(identity=identity),
                self.assertRaises(formal.FormalSourceError),
            ):
                self.authorize(ledger, identity)


class GenerateGamesTest(unittest.TestCase):
    def test_all_seeds_return_once_in_seed_order(self) -> None:
        seeds = (932002, 932000, 932001)
        games = formal.generate_games(seeds, 1, play=fake_game)
        self.assertEqual([game[0] for game in games], [932000, 932001, 932002])

    def test_progress_is_reported_per_hanchan(self) -> None:
        messages = []
        formal.generate_games((1, 2), 1, play=fake_game, progress=messages.append)
        self.assertEqual(len(messages), 2)

    def test_missing_unexpected_and_repeated_seeds_are_rejected(self) -> None:
        wrong = {
            "missing": lambda seed: fake_game(seed + 100),
            "repeated": lambda seed: fake_game(1),
        }
        for name, play in wrong.items():
            with (
                self.subTest(name),
                self.assertRaisesRegex(
                    formal.FormalSourceError, "returned seeds differ"
                ),
            ):
                formal.generate_games((1, 2), 1, play=play)

    def test_rows_of_another_seed_are_rejected(self) -> None:
        def play(seed):
            _, decisions, facts, stats = fake_game(seed + 1)
            return seed, decisions, facts, stats

        with self.assertRaisesRegex(formal.FormalSourceError, "another seed"):
            formal.generate_games((1,), 1, play=play)

    def test_decisions_and_label_facts_must_match(self) -> None:
        def play(seed):
            _, decisions, facts, stats = fake_game(seed)
            return seed, decisions, facts[:-1], stats

        with self.assertRaisesRegex(formal.FormalSourceError, "differ"):
            formal.generate_games((1,), 1, play=play)

    def test_requested_seeds_must_be_distinct(self) -> None:
        for seeds in ((), (1, 1)):
            with self.subTest(seeds=seeds), self.assertRaises(formal.FormalSourceError):
                formal.generate_games(seeds, 1, play=fake_game)

    def test_a_failed_worker_fails_the_whole_run(self) -> None:
        with self.assertRaisesRegex(formal.FormalSourceError, "932003"):
            formal.generate_games(tuple(range(932000, 932008)), 2, play=failing_play)

    def test_workers_run_in_parallel_processes(self) -> None:
        games = formal.generate_games(tuple(range(932000, 932006)), 3, play=fake_game)
        self.assertEqual([game[0] for game in games], list(range(932000, 932006)))


class WriteSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seeds = (932000, 932001)
        self.games = [fake_game(seed) for seed in self.seeds]
        self.producer = {
            "arena_revision": ARENA,
            "lisjong_engine_revision": ENGINE,
            "lisjong_revision": LISJONG,
            "policy": "PlacementAwareSpeedCallPolicy",
        }

    def test_manifest_has_empty_train_and_valid(self) -> None:
        digests = formal.write_source(
            self.root / "out", self.seeds, self.games, self.producer
        )
        manifest = json.loads(
            (self.root / "out" / "source" / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(
            manifest["splits"], {"train": [], "valid": [], "test": [932000, 932001]}
        )
        self.assertEqual(manifest["files"]["decisions"]["rows"], 6)
        self.assertEqual(
            set(digests), {"manifest.json", "decisions.jsonl", "label_facts.jsonl"}
        )

    def test_existing_output_is_refused(self) -> None:
        (self.root / "out").mkdir()
        with self.assertRaises(FileExistsError):
            formal.write_source(
                self.root / "out", self.seeds, self.games, self.producer
            )

    def test_rows_outside_the_requested_seeds_are_rejected(self) -> None:
        games = [fake_game(932000), fake_game(932005)]
        with self.assertRaisesRegex(formal.FormalSourceError, "outside"):
            formal.write_source(self.root / "out", self.seeds, games, self.producer)


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        ledger, self.record = reserved_ledger()
        self.ledger_path = self.root / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, ledger)
        self.ledger = ledger
        self.backend = "rust"
        for patcher in (
            mock.patch.object(
                runtime_identity, "runtime_binding", side_effect=self.runtime
            ),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runtime(self, project):
        return {
            "arena_revision": ARENA,
            "dependencies": {"lisjong": LISJONG, "lisjong-engine": ENGINE},
            "lisjong_source_digest": "d" * 64,
            "shanten_backend": {"name": self.backend, "native": None},
            "python": "3.14.0",
            "riichienv": "0.4.10",
        }

    def arguments(self, **overrides):
        values = {
            "mode": "formal",
            "seed_ledger": self.ledger_path,
            "allocation_identity": self.record["allocation_identity"],
            "workers": 1,
            "output": self.root / "out",
            "progress": self.root / "progress.txt",
            "seeds": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_formal_run_writes_source_and_generation_record(self) -> None:
        document = formal.run(self.arguments(), play=fake_game)
        out = self.root / "out"
        manifest = json.loads((out / "source" / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["splits"]["test"], list(formal.FORMAL_SEEDS))
        self.assertEqual(manifest["splits"]["train"], [])
        self.assertEqual(manifest["producer"]["arena_revision"], ARENA)
        self.assertEqual(manifest["producer"]["lisjong_revision"], LISJONG)
        self.assertEqual(manifest["producer"]["lisjong_engine_revision"], ENGINE)
        written = json.loads((out / "generation.json").read_text("utf-8"))
        self.assertEqual(written, json.loads(json.dumps(document)))
        self.assertEqual(written["mode"], "formal")
        self.assertEqual(
            written["allocation"]["allocation_identity"],
            self.record["allocation_identity"],
        )
        self.assertEqual(
            written["ledger_revision"], seed_registry.ledger_revision(self.ledger)
        )
        self.assertEqual(len(written["games"]), 100)
        self.assertEqual(
            written["seeds"], {"first": 932000, "last": 932099, "count": 100}
        )
        self.assertEqual(
            len((self.root / "progress.txt").read_text().splitlines()), 102
        )

    def test_formal_run_requires_the_rust_backend(self) -> None:
        self.backend = "python"
        with self.assertRaisesRegex(formal.FormalSourceError, "rust"):
            formal.run(self.arguments(), play=fake_game)
        self.assertFalse((self.root / "out").exists())

    def test_formal_run_requires_an_authorized_allocation(self) -> None:
        with self.assertRaises(formal.FormalSourceError):
            formal.run(self.arguments(allocation_identity="0" * 64), play=fake_game)
        self.assertFalse((self.root / "out").exists())

    def test_formal_run_requires_the_allocation_revision_to_match(self) -> None:
        ledger, record = reserved_ledger(arena_revision="e" * 40)
        seed_registry.write_ledger(self.root / "other.json", ledger)
        with self.assertRaisesRegex(formal.FormalSourceError, "arena_revision"):
            formal.run(
                self.arguments(
                    seed_ledger=self.root / "other.json",
                    allocation_identity=record["allocation_identity"],
                ),
                play=fake_game,
            )

    def test_failed_generation_writes_nothing(self) -> None:
        def play(seed):
            if seed == 932050:
                raise RuntimeError("boom")
            return fake_game(seed)

        with self.assertRaises(RuntimeError):
            formal.run(self.arguments(), play=play)
        self.assertFalse((self.root / "out").exists())

    def test_existing_output_is_refused_before_generation(self) -> None:
        (self.root / "out").mkdir()
        play = mock.Mock(side_effect=AssertionError("must not generate"))
        with self.assertRaisesRegex(formal.FormalSourceError, "overwrite"):
            formal.run(self.arguments(), play=play)

    def test_smoke_run_uses_development_seeds_without_a_registry_binding(self) -> None:
        self.backend = "python"
        document = formal.run(
            self.arguments(mode="smoke", seeds="931100..931103"), play=fake_game
        )
        manifest = json.loads(
            (self.root / "out" / "source" / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(manifest["splits"]["test"], [931100, 931101, 931102, 931103])
        self.assertEqual(document["mode"], "smoke")
        self.assertNotIn("allocation", document)
        self.assertNotIn("ledger_revision", document)

    def test_smoke_run_refuses_formal_seeds(self) -> None:
        with self.assertRaises(formal.FormalSourceError):
            formal.run(
                self.arguments(mode="smoke", seeds="932000..932003"), play=fake_game
            )
        self.assertFalse((self.root / "out").exists())

    def test_check_allocation_needs_no_game_or_runtime_binding(self) -> None:
        arguments = [
            "check-allocation",
            "--seed-ledger",
            str(self.ledger_path),
            "--allocation-identity",
            self.record["allocation_identity"],
            "--arena-revision",
            ARENA,
        ]
        with mock.patch("builtins.print") as printed:
            self.assertEqual(formal.main(arguments), 0)
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["state"], seed_registry.RESERVED)
        self.assertEqual(
            report["allocation_binding"]["allocation_identity"],
            self.record["allocation_identity"],
        )
        arguments[-1] = "f" * 40
        self.assertEqual(formal.main(arguments), 1)

    def test_a_ledger_with_crlf_line_endings_is_accepted(self) -> None:
        text = self.ledger_path.read_text(encoding="utf-8")
        crlf = self.root / "crlf.json"
        crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        formal.run(self.arguments(seed_ledger=crlf), play=fake_game)
        self.assertEqual(
            json.loads((self.root / "out" / "generation.json").read_text())[
                "ledger_revision"
            ],
            seed_registry.ledger_revision(self.ledger),
        )

    def test_main_reports_a_stop_invalid_error(self) -> None:
        code = formal.main(
            [
                "smoke",
                "--seeds",
                "932000",
                "--workers",
                "1",
                "--output",
                str(self.root / "main-out"),
            ]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
