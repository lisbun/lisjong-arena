"""#372 engine C0 / C2 runner (scripts/l03_engine_source_372.py).

population planningはtest ledgerだけでlive authorityへのbindingを検証し、
生成は実lisjong-engine対局1 hanchan（test専用seed）で既存producerを通す。
consumer readbackのsizing / support判定はsummary dictで固定する。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _engine_focal_outcome_source_fixtures import source_contract

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import engine_source

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import l03_engine_source_372 as runner  # noqa: E402

OWNER = "lisbun/lisjong-arena#372"
REVISION = "0" * 40
# test専用seed。focal seat 0で3 kyoku（bankruptcy）で終わる（#370 integration test）。
SMOKE_SEED = 900203


def _reserve(ledger, split, seeds, *, owner=OWNER, revision=REVISION):
    return seed_registry.reserve_allocation(
        ledger,
        owner_issue=owner,
        protocol="test-only-protocol",
        seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        purpose=f"test-only {split}",
        population="test-only-population",
        split=split,
        seeds=list(seeds),
        arena_revision=revision,
        protocol_revision="test-only",
        provenance_reference="test-only",
        allocation_timestamp="2026-09-25T00:00:00Z",
    )


def _plan(role, allocations, live, **overrides):
    kwargs = {"owner_issue": OWNER, "arena_revision": REVISION, **overrides}
    return runner.plan_population(
        population_role=role, allocations=allocations, live_ledger=live, **kwargs
    )


class PlanPopulationTest(unittest.TestCase):
    def test_calibration_binds_the_authorizing_snapshot(self):
        authorizing, record = _reserve(
            seed_registry.new_ledger(), "CALIBRATION", range(900300, 900316)
        )
        # 後続の無関係な予約でlive ledgerが進んでもbindingはauthorizing snapshotのまま。
        live, _ = _reserve(authorizing, "TRAIN", range(900400, 900410))
        identity = record["allocation_identity"]
        games, bindings, records = _plan(
            "CALIBRATION", [("CALIBRATION", identity, authorizing)], live
        )
        self.assertEqual(
            games, [(seed, "CALIBRATION") for seed in range(900300, 900316)]
        )
        self.assertEqual(
            bindings["CALIBRATION"],
            seed_registry.allocation_binding(authorizing, identity),
        )
        self.assertNotEqual(
            bindings["CALIBRATION"]["ledger_revision"],
            seed_registry.ledger_revision(live),
        )
        self.assertEqual(records["CALIBRATION"]["allocation_identity"], identity)
        engine_source.validate_population("CALIBRATION", games, bindings)

    def test_scientific_orders_train_before_select(self):
        ledger, select = _reserve(
            seed_registry.new_ledger(), "SELECT", range(900500, 900504)
        )
        ledger, train = _reserve(ledger, "TRAIN", range(900600, 900608))
        games, bindings, _ = _plan(
            "SCIENTIFIC",
            [
                ("SELECT", select["allocation_identity"], ledger),
                ("TRAIN", train["allocation_identity"], ledger),
            ],
            ledger,
        )
        self.assertEqual(
            games,
            [(seed, "TRAIN") for seed in range(900600, 900608)]
            + [(seed, "SELECT") for seed in range(900500, 900504)],
        )
        engine_source.validate_population("SCIENTIFIC", games, bindings)

    def test_unauthorized_or_mismatched_allocations_fail_closed(self):
        ledger, calibration = _reserve(
            seed_registry.new_ledger(), "CALIBRATION", range(900300, 900316)
        )
        ledger, other = _reserve(
            ledger, "CALIBRATION", range(900700, 900716), owner="lisbun/x#1"
        )
        ledger, stale = _reserve(
            ledger, "CALIBRATION", range(900800, 900816), revision="1" * 40
        )
        ledger, train = _reserve(ledger, "TRAIN", range(900900, 900916))
        ledger, short = _reserve(ledger, "CALIBRATION", range(900920, 900935))
        ledger, long = _reserve(ledger, "CALIBRATION", range(900940, 900957))
        ledger, excluded = _reserve(ledger, "CALIBRATION", range(910390, 910406))
        allocation = ("CALIBRATION", calibration["allocation_identity"], ledger)
        cases = {
            "wrong owner": (
                "CALIBRATION",
                [("CALIBRATION", other["allocation_identity"], ledger)],
                "not authorized",
            ),
            "revision": (
                "CALIBRATION",
                [("CALIBRATION", stale["allocation_identity"], ledger)],
                "arena_revision",
            ),
            "split": (
                "CALIBRATION",
                [("CALIBRATION", train["allocation_identity"], ledger)],
                "not authorized",
            ),
            "diagnostic range": (
                "CALIBRATION",
                [("CALIBRATION", excluded["allocation_identity"], ledger)],
                "excluded diagnostic",
            ),
            "duplicate": ("CALIBRATION", [allocation, allocation], "duplicate"),
            "calibration short": (
                "CALIBRATION",
                [("CALIBRATION", short["allocation_identity"], ledger)],
                "exactly 16",
            ),
            "calibration long": (
                "CALIBRATION",
                [("CALIBRATION", long["allocation_identity"], ledger)],
                "exactly 16",
            ),
            "missing split": ("SCIENTIFIC", [allocation], "requires exactly"),
            "role": ("DIAGNOSTIC", [allocation], "unsupported"),
        }
        for name, (role, allocations, pattern) in cases.items():
            with (
                self.subTest(name),
                self.assertRaisesRegex(runner.RunnerError, pattern),
            ):
                _plan(role, allocations, ledger)

    def test_retired_allocation_fails_closed(self):
        ledger, record = _reserve(
            seed_registry.new_ledger(), "CALIBRATION", range(900300, 900316)
        )
        identity = record["allocation_identity"]
        retired = seed_registry.transition_allocation(
            ledger, identity, state=seed_registry.RETIRED
        )
        with self.assertRaisesRegex(runner.RunnerError, "not active"):
            _plan("CALIBRATION", [("CALIBRATION", identity, ledger)], retired)


class GenerateTest(unittest.TestCase):
    def test_real_engine_generation_writes_source_and_record(self):
        ledger, record = _reserve(
            seed_registry.new_ledger(), "CALIBRATION", [SMOKE_SEED]
        )
        identity = record["allocation_identity"]
        games = [(SMOKE_SEED, "CALIBRATION")]
        bindings = {"CALIBRATION": seed_registry.allocation_binding(ledger, identity)}
        records = {"CALIBRATION": record}
        engine_source.validate_population("CALIBRATION", games, bindings)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "c0"
            lines = []
            result = runner.generate(
                output,
                population_role="CALIBRATION",
                games=games,
                allocation_bindings=bindings,
                allocation_records=records,
                source_contract=source_contract(),
                log=lines.append,
            )
            verified = engine_source.verify_engine_focal_outcome_source(
                output / "source"
            )
            written = json.loads((output / "generation.json").read_text("utf-8"))
            with self.assertRaises(FileExistsError):
                runner.generate(
                    output,
                    population_role="CALIBRATION",
                    games=games,
                    allocation_bindings=bindings,
                    allocation_records=records,
                    source_contract=source_contract(),
                    log=lines.append,
                )
        self.assertEqual(written, json.loads(json.dumps(result)))
        self.assertEqual(result["source_identity"], verified.identity)
        self.assertEqual(verified.population_role, "CALIBRATION")
        self.assertEqual((result["hanchan"], result["workers"]), (1, 1))
        (game,) = result["games"]
        self.assertEqual(
            (game["seed"], game["split"], game["focal_seat"]),
            (SMOKE_SEED, "CALIBRATION", 0),
        )
        self.assertEqual(
            result["allocations"]["CALIBRATION"]["allocation_identity"],
            record["allocation_identity"],
        )
        self.assertTrue(lines[0].startswith("starting game_ordinal 0"))


def _summary(rows=100, canonical=0.5, kyokus=40, hanchan=16):
    return {
        "canonical_first_selected_rate": canonical if rows else None,
        "eligible_row_count": rows,
        "excluded": {"single_survivor": 3},
        "hanchan_count": hanchan,
        "non_canonical_first_selected_rate": 1 - canonical if rows else None,
        "unique_eligible_kyoku_count": kyokus,
    }


class ReadbackAssessmentTest(unittest.TestCase):
    def test_c1_sizing_uses_the_frozen_formula_exactly(self):
        # k = 10: 4 * ceil(4000 / 40) = 400, 4 * ceil(1000 / 40) = 100
        self.assertEqual(
            runner.c1_sizing(160, 16), {"k": 10.0, "N_SELECT": 100, "N_TRAIN": 400}
        )
        # k = 9.375: 4000 / 37.5 = 106.67 -> 107, 1000 / 37.5 = 26.67 -> 27
        self.assertEqual(
            runner.c1_sizing(150, 16), {"k": 9.375, "N_SELECT": 108, "N_TRAIN": 428}
        )
        # 4000 / (4 * 12.5) = 80 exactly: floatの誤差でceilが81にならない
        self.assertEqual(runner.c1_sizing(200, 16)["N_TRAIN"], 320)
        with self.assertRaisesRegex(runner.RunnerError, "k == 0"):
            runner.c1_sizing(0, 16)

    def test_calibration_assessment(self):
        splits = {"CALIBRATION": runner.split_facts(_summary())}
        report = runner.assess("CALIBRATION", splits)
        self.assertEqual(report["result"], runner.RESULT_C0_COMPLETE)
        self.assertEqual(report["c1"], runner.c1_sizing(40, 16))
        facts = splits["CALIBRATION"]
        self.assertNotIn("excluded", facts)
        self.assertEqual(facts["eligible_rows_per_hanchan"], 100 / 16)
        self.assertEqual(facts["eligible_rows_per_eligible_kyoku"], 100 / 40)

    def test_support_or_empty_support_stops(self):
        cases = {
            "canonical": (_summary(canonical=0.19), "canonical-first selected"),
            "non-canonical": (_summary(canonical=0.81), "non-canonical-first"),
            "empty": (_summary(rows=0, kyokus=0), "no eligible rows"),
        }
        for name, (summary, pattern) in cases.items():
            with self.subTest(name):
                report = runner.assess(
                    "CALIBRATION", {"CALIBRATION": runner.split_facts(summary)}
                )
                self.assertEqual(report["result"], runner.RESULT_STOP)
                self.assertRegex(" ".join(report["hard_stops"]), pattern)
        boundary = runner.assess(
            "CALIBRATION", {"CALIBRATION": runner.split_facts(_summary(canonical=0.2))}
        )
        self.assertEqual(boundary["hard_stops"], [])

    def test_scientific_checks_train_only_and_has_no_sizing(self):
        splits = {
            "SELECT": runner.split_facts(_summary(canonical=0.05)),
            "TRAIN": runner.split_facts(_summary()),
        }
        report = runner.assess("SCIENTIFIC", splits)
        self.assertEqual(report["result"], runner.RESULT_SCIENTIFIC_READY)
        self.assertNotIn("c1", report)
        splits["TRAIN"] = runner.split_facts(_summary(canonical=0.05))
        self.assertEqual(
            runner.assess("SCIENTIFIC", splits)["result"], runner.RESULT_STOP
        )

    def test_readback_requires_the_frozen_consumer_revision(self):
        # CI / 開発環境のlisjongはproducer pin（aed9c84）であり、consumerではない。
        with self.assertRaisesRegex(runner.RunnerError, "consumer lisjong"):
            runner.readback("does-not-matter")


if __name__ == "__main__":
    unittest.main()
