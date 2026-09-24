"""Unit tests for the #370 lisjong-engine focal outcome source.

実engine対局は起動しない。engineの実value typeで組んだ``CompletedMatch``から
kyoku境界を作り、単一game実行境界（``run_game``）を差し替えてsourceを生成・
strict readbackする。focal decisionを含む実engine経路は
``test_engine_focal_outcome_source_integration.py``で検証する。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _engine_focal_outcome_source_fixtures import (
    Tampered,
    binding,
    build_match,
    leftover_riichi_match,
    read_lines,
    riichi,
    source_contract,
)
from lisjong.policy_contract import Seat, Wind
from lisjong_engine.match_state import CompletedMatch
from lisjong_engine.round_result import (
    AbortiveDrawReason,
    AbortiveDrawResult,
    ExhaustiveDrawResult,
)
from lisjong_engine.seat import Seat as ES

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import engine_source
from lisjong_arena.focal_outcome_source import source as riichienv_source
from lisjong_arena.focal_outcome_source.accounting import FocalOutcomeSourceError

SEEDS = (900300, 900301)


def _execution(match):
    def run_game(*, seed, focal_seat):
        return engine_source.EngineFocalGameExecution(
            seed=seed, focal_seat=focal_seat, match=match, captures=()
        )

    return run_game


class EngineKyokuFactsTest(unittest.TestCase):
    def setUp(self):
        self.match = leftover_riichi_match()
        self.facts = engine_source.engine_game_facts(self.match)

    def test_points_after_kyoku_is_the_engine_settlement_score(self):
        for kyoku, completed in zip(self.facts.kyokus, self.match.history, strict=True):
            after = completed.scores_after_settlement
            self.assertEqual(
                kyoku.points_after_kyoku,
                (after.east, after.south, after.west, after.north),
            )

    def test_terminal_kyoku_excludes_hanchan_final_redistribution(self):
        final = self.facts.kyokus[-1]
        self.assertEqual(final.riichi_sticks_after, 3)
        self.assertEqual(final.points_after_kyoku, (27000, 23000, 23000, 24000))
        # 残存供託3本はhanchan最終scoreだけを変え、最終kyoku境界は変えない。
        self.assertEqual(
            self.facts.hanchan_final_raw_scores, (30000, 23000, 23000, 24000)
        )
        self.assertEqual(self.facts.final_riichi_stick_awards, ((Seat(0), 3000),))
        self.assertNotEqual(
            final.points_after_kyoku, self.facts.hanchan_final_raw_scores
        )

    def test_points_before_plus_deltas_is_points_after_for_every_seat(self):
        for kyoku in self.facts.kyokus:
            self.assertEqual(
                [
                    before + delta
                    for before, delta in zip(
                        kyoku.points_before_kyoku, kyoku.point_deltas, strict=True
                    )
                ],
                list(kyoku.points_after_kyoku),
            )

    def test_kyoku_to_kyoku_score_and_riichi_stick_continuity_is_exact(self):
        kyokus = self.facts.kyokus
        self.assertEqual(kyokus[0].points_before_kyoku, (25000,) * 4)
        self.assertEqual(kyokus[0].riichi_sticks_before, 0)
        for previous, current in zip(kyokus, kyokus[1:]):
            self.assertEqual(current.points_before_kyoku, previous.points_after_kyoku)
            self.assertEqual(current.riichi_sticks_before, previous.riichi_sticks_after)
        self.assertEqual(
            [(k.riichi_sticks_before, k.riichi_sticks_after) for k in kyokus],
            [(0, 2), (2, 3), (3, 3)],
        )

    def test_all_four_tenpai_exhaustive_draw_is_taken_from_settlement(self):
        # RiichiEnv #247では供託が二重に計上されたpattern。engineでは
        # settlementのpoint_deltasが立直供託だけを持つ。
        first = self.facts.kyokus[0]
        self.assertEqual(first.end, {"kind": "draw", "draw_kind": "exhaustive"})
        self.assertEqual(first.point_deltas, (-1000, -1000, 0, 0))
        self.assertEqual(first.points_after_kyoku, (24000, 24000, 25000, 25000))

    def test_abortive_draw_uses_the_engine_reason(self):
        match = build_match(
            [
                (1, 0, AbortiveDrawResult(AbortiveDrawReason.FOUR_WINDS), ()),
                (1, 1, ExhaustiveDrawResult(tenpai_seats=(ES.EAST,)), ()),
            ]
        )
        facts = engine_source.engine_game_facts(match)
        self.assertEqual(
            facts.kyokus[0].end, {"kind": "draw", "draw_kind": "four_winds"}
        )

    def test_history_position_mismatch_fails_closed(self):
        history = list(self.match.history)
        history[1], history[2] = history[2], history[1]
        tampered = CompletedMatch(
            end_reason=self.match.end_reason,
            final_riichi_stick_awards=self.match.final_riichi_stick_awards,
            final_raw_scores=self.match.final_raw_scores,
            final_score=self.match.final_score,
            history=tuple(history),
        )
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.engine_game_facts(tampered)

    def test_final_raw_scores_beyond_awards_fail_closed(self):
        tampered = CompletedMatch(
            end_reason=self.match.end_reason,
            final_riichi_stick_awards=(),
            final_raw_scores=self.match.final_raw_scores,
            final_score=self.match.final_score,
            history=self.match.history,
        )
        with self.assertRaisesRegex(FocalOutcomeSourceError, "remaining sticks"):
            engine_source.engine_game_facts(tampered)

    def test_non_engine_match_is_rejected(self):
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.engine_game_facts(SimpleNamespace(history=()))


class KyokuBindingTest(unittest.TestCase):
    def setUp(self):
        self.kyokus = engine_source.engine_game_facts(leftover_riichi_match()).kyokus

    def _decision(self, honba, dealer=Seat(0)):
        state = SimpleNamespace(
            round_wind=Wind.EAST, hand_number=1, honba=honba, dealer_seat=dealer
        )
        return SimpleNamespace(input=SimpleNamespace(round=state))

    def test_round_identity_binds_exactly_one_kyoku(self):
        self.assertEqual(
            engine_source.bind_kyoku(self._decision(2), self.kyokus, "d"), 2
        )

    def test_zero_or_multiple_matches_fail_closed(self):
        with self.assertRaisesRegex(FocalOutcomeSourceError, "matches 0"):
            engine_source.bind_kyoku(self._decision(9), self.kyokus, "d")
        with self.assertRaisesRegex(FocalOutcomeSourceError, "matches 2"):
            engine_source.bind_kyoku(
                self._decision(0), self.kyokus + self.kyokus[:1], "d"
            )

    def test_dealer_seat_mismatch_fails_closed(self):
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.bind_kyoku(self._decision(0, Seat(1)), self.kyokus, "d")


class PopulationAndContractTest(unittest.TestCase):
    def test_diagnostic_role_has_no_allocation_bindings(self):
        games = [(910000, "DIAGNOSTIC")]
        self.assertEqual(
            engine_source.validate_population("DIAGNOSTIC", games, {}),
            ((910000, "DIAGNOSTIC"),),
        )
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.validate_population(
                "DIAGNOSTIC", games, {"DIAGNOSTIC": binding([910000])}
            )

    def test_scientific_role_requires_engine_seed_domain_bindings(self):
        games = [(1, "TRAIN"), (2, "SELECT")]
        bindings = {"TRAIN": binding([1]), "SELECT": binding([2])}
        engine_source.validate_population("SCIENTIFIC", games, bindings)
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.validate_population("SCIENTIFIC", games, {})
        riichienv = {
            split: binding(
                [seed], domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN
            )
            for seed, split in games
        }
        with self.assertRaisesRegex(FocalOutcomeSourceError, "seed_domain"):
            engine_source.validate_population("SCIENTIFIC", games, riichienv)
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.validate_population(
                "SCIENTIFIC", [(1, "DIAGNOSTIC")], bindings
            )

    def test_source_contract_pins_backend_revisions_and_rules(self):
        engine_source.validate_source_contract(source_contract())
        for field, value in (
            ("backend", "riichienv"),
            ("dependencies", {"lisjong": riichienv_source.PINNED_LISJONG_REVISION}),
            ("rules", {**engine_source.RULES, "version": 2}),
            ("arena_revision", "HEAD"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(FocalOutcomeSourceError):
                    engine_source.validate_source_contract(
                        {**source_contract(), field: value}
                    )
        stale = source_contract()
        stale["dependencies"]["lisjong-engine"] = (
            "8735e89e1aea000ab59368d0368d476787827741"
        )
        with self.assertRaises(FocalOutcomeSourceError):
            engine_source.validate_source_contract(stale)


class EngineSourceReadbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "source"
        self.source = engine_source.generate_engine_focal_outcome_source(
            self.path,
            population_role="DIAGNOSTIC",
            games=[(seed, "DIAGNOSTIC") for seed in SEEDS],
            allocation_bindings={},
            source_contract=source_contract(),
            run_game=_execution(leftover_riichi_match()),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _rejects(self, tampered, pattern=None):
        with self.assertRaisesRegex(FocalOutcomeSourceError, pattern or ""):
            engine_source.verify_engine_focal_outcome_source(tampered.path)

    def test_generated_source_passes_strict_readback(self):
        self.assertEqual(self.source.population_role, "DIAGNOSTIC")
        self.assertEqual(
            [(g.seed, g.focal_seat, g.kyoku_count) for g in self.source.games],
            [(900300, Seat(0), 3), (900301, Seat(1), 3)],
        )
        manifest = json.loads((self.path / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["schema"], engine_source.ENGINE_OUTCOME_SOURCE_SCHEMA)
        self.assertEqual(manifest["source_contract"]["backend"], "lisjong-engine")
        game = manifest["games"][0]
        self.assertEqual(game["hanchan_final_raw_scores"], [30000, 23000, 23000, 24000])
        self.assertEqual(
            game["final_riichi_stick_awards"], [{"amount": 3000, "recipient_seat": 0}]
        )
        final = read_lines(self.path / "game-000" / "kyokus.jsonl")[-1]
        self.assertEqual(final["points_after_kyoku"], [27000, 23000, 23000, 24000])
        self.assertTrue(final["is_final_kyoku"])

    def test_resealed_unchanged_copy_still_passes(self):
        # 改ざんcaseがhelper由来ではなく、改ざん内容だけで失敗することの対照。
        tampered = Tampered(self.path, self.root)
        tampered.rewrite_game(0, kyokus=tampered.kyokus())
        engine_source.verify_engine_focal_outcome_source(tampered.path)

    def test_generation_refuses_to_overwrite(self):
        with self.assertRaises(FileExistsError):
            engine_source.generate_engine_focal_outcome_source(
                self.path,
                population_role="DIAGNOSTIC",
                games=[(SEEDS[0], "DIAGNOSTIC")],
                allocation_bindings={},
                source_contract=source_contract(),
                run_game=_execution(leftover_riichi_match()),
            )

    def test_failed_game_publishes_nothing(self):
        calls = []

        def run_game(*, seed, focal_seat):
            calls.append(seed)
            if len(calls) == 2:
                raise RuntimeError("engine failure")
            return _execution(leftover_riichi_match())(seed=seed, focal_seat=focal_seat)

        destination = self.root / "failed"
        with self.assertRaises(RuntimeError):
            engine_source.generate_engine_focal_outcome_source(
                destination,
                population_role="DIAGNOSTIC",
                games=[(seed, "DIAGNOSTIC") for seed in SEEDS],
                allocation_bindings={},
                source_contract=source_contract(),
                run_game=run_game,
            )
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source"])

    def test_terminal_boundary_tamper_to_final_raw_scores_fails_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        kyokus[-1]["points_after_kyoku"] = [30000, 23000, 23000, 24000]
        kyokus[-1]["point_deltas"] = [3000, 0, 0, 0]
        kyokus[-1]["riichi_sticks_after"] = 0
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "final riichi stick awards")

    def test_before_delta_after_mismatch_fails_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        kyokus[1]["point_deltas"] = [2000, -1000, -1000, 0]
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "point_deltas")

    def test_score_continuity_break_fails_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        # kyoku内の逆算・保存則は保ったまま、直前kyokuとの連続だけを崩す。
        for field in ("points_before_kyoku", "points_after_kyoku"):
            kyokus[1][field][0] += 100
            kyokus[1][field][1] -= 100
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "do not continue")

    def test_riichi_stick_continuity_break_fails_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        kyokus[2]["riichi_sticks_before"] = 4
        kyokus[2]["riichi_sticks_after"] = 4
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "do not continue")

    def test_conservation_break_fails_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        kyokus[0]["riichi_sticks_after"] = 1
        kyokus[1]["riichi_sticks_before"] = 1
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "conservation")

    def test_row_order_and_final_marker_fail_closed(self):
        tampered = Tampered(self.path, self.root)
        kyokus = tampered.kyokus()
        tampered.rewrite_game(0, kyokus=[kyokus[1], kyokus[0], kyokus[2]])
        self._rejects(tampered, "ordinal")
        tampered = Tampered(self.path, self.root / "final")
        kyokus = tampered.kyokus()
        kyokus[1]["is_final_kyoku"] = True
        tampered.rewrite_game(0, kyokus=kyokus)
        self._rejects(tampered, "is_final_kyoku")

    def test_row_field_missing_or_extra_fails_closed(self):
        for mutate in (
            lambda row: row.pop("point_deltas"),
            lambda row: row.update(step_ordinal=0),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as tmp:
                tampered = Tampered(self.path, tmp)
                kyokus = tampered.kyokus()
                mutate(kyokus[0])
                tampered.rewrite_game(0, kyokus=kyokus)
                self._rejects(tampered, "fields")

    def test_payload_and_manifest_digest_tamper_fail_closed(self):
        tampered = Tampered(self.path, self.root)
        path = tampered.path / "game-000" / engine_source.KYOKU_PAYLOAD_FILENAME
        path.write_bytes(path.read_bytes() + b"\n")
        self._rejects(tampered, "digest")
        tampered = Tampered(self.path, self.root / "manifest")
        manifest = tampered.manifest()
        manifest["population_role"] = "CALIBRATION"
        tampered.write_manifest(manifest, reseal=False)
        self._rejects(tampered, "identity")

    def test_provenance_tamper_fails_closed(self):
        for field, value in (
            ("population_role", "SCIENTIFIC"),
            ("allocation_bindings", {"DIAGNOSTIC": binding(SEEDS)}),
            ("behavior", {**engine_source.BEHAVIOR, "focal_rotation_rule": "x"}),
            ("kind", "other"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                tampered = Tampered(self.path, tmp)
                manifest = tampered.manifest()
                manifest[field] = value
                tampered.write_manifest(manifest)
                self._rejects(tampered)

    def test_game_summary_tamper_fails_closed(self):
        for summary in (
            {"focal_seat": 2},
            {"kyoku_count": 2},
            {"match_end_reason": "manual"},
            {"final_riichi_stick_awards": []},
            {"split": ["DIAGNOSTIC"]},
            {"match_end_reason": {}},
        ):
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as tmp:
                tampered = Tampered(self.path, tmp)
                tampered.rewrite_game(1, summary=summary)
                self._rejects(tampered)


class HistoricalSchemaIsolationTest(unittest.TestCase):
    """RiichiEnv v1 schemaの意味とhandlingを変えない。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "source"
        engine_source.generate_engine_focal_outcome_source(
            self.path,
            population_role="DIAGNOSTIC",
            games=[(SEEDS[0], "DIAGNOSTIC")],
            allocation_bindings={},
            source_contract=source_contract(),
            run_game=_execution(leftover_riichi_match()),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_schema_identities_are_distinct(self):
        self.assertEqual(
            riichienv_source.OUTCOME_SOURCE_SCHEMA,
            "arena-offense-l0.3-focal-outcome-source-v1",
        )
        self.assertEqual(
            engine_source.ENGINE_OUTCOME_SOURCE_SCHEMA,
            "arena-offense-l0.3-lisjong-engine-focal-outcome-source-v1",
        )
        self.assertEqual(riichienv_source.BACKEND_NAME, "riichienv")

    def test_riichienv_verifier_rejects_the_engine_schema(self):
        with self.assertRaisesRegex(FocalOutcomeSourceError, "unsupported"):
            riichienv_source.verify_focal_outcome_source(self.path)

    def test_engine_verifier_rejects_the_riichienv_schema(self):
        tampered = Tampered(self.path, self.root)
        manifest = tampered.manifest()
        manifest["schema"] = riichienv_source.OUTCOME_SOURCE_SCHEMA
        tampered.write_manifest(manifest)
        with self.assertRaisesRegex(FocalOutcomeSourceError, "unsupported"):
            engine_source.verify_engine_focal_outcome_source(tampered.path)


class RiichiOnlyDrawTest(unittest.TestCase):
    def test_riichi_contribution_only_draw_keeps_sticks(self):
        match = build_match(
            [
                (1, 0, ExhaustiveDrawResult(), riichi(ES.NORTH)),
                (2, 1, ExhaustiveDrawResult(), ()),
            ]
        )
        facts = engine_source.engine_game_facts(match)
        self.assertEqual(facts.kyokus[0].point_deltas, (0, 0, 0, -1000))
        self.assertEqual(facts.kyokus[1].riichi_sticks_before, 1)


if __name__ == "__main__":
    unittest.main()
