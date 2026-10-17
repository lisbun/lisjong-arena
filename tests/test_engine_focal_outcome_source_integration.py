"""Integration tests for the #370 lisjong-engine focal outcome source.

test専用seedの実engine入力は、#493の位置供給後は旧schemaで拒否する。
従来のreadback・改ざん検証は、位置field導入前を模したtest専用historical fixtureで維持する。
productionで位置を除去する変換や新しい保存schemaは導入しない。
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _engine_focal_outcome_source_fixtures import Tampered, read_lines, source_contract
from _focal_outcome_source_fixtures import historical_captures_fixture
from lisjong_engine.seat import Seat as ES

from lisjong_arena.durable_local_game_record import DurableLocalGameRecordError
from lisjong_arena.focal_outcome_source import engine_source
from lisjong_arena.focal_outcome_source.accounting import FocalOutcomeSourceError
from lisjong_arena.focal_outcome_source.exploration_token import exploration_token
from lisjong_arena.focal_outcome_source.source import _action_to_value

# test専用seed。focal seat 0で3 kyoku（bankruptcy）で終わることを確認済み。
SMOKE_SEED = 900203


class EngineFocalOutcomeSourceIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.path = cls.root / "source"
        from lisjong.policy_contract import Seat

        cls.current_execution = engine_source.run_engine_focal_game(
            seed=SMOKE_SEED, focal_seat=Seat(0)
        )
        # Historical v1 input fixture only. It emulates pre-#493 snapshots so the
        # existing reader/tamper cases remain covered; never used by production.
        cls.execution = replace(
            cls.current_execution,
            captures=historical_captures_fixture(cls.current_execution.captures),
        )

        def run_game(**kwargs):
            return cls.execution

        cls.source = engine_source.generate_engine_focal_outcome_source(
            cls.path,
            population_role="DIAGNOSTIC",
            games=[(SMOKE_SEED, "DIAGNOSTIC")],
            allocation_bindings={},
            source_contract=source_contract(),
            run_game=run_game,
        )
        cls.kyokus = read_lines(cls.path / "game-000" / "kyokus.jsonl")
        cls.decisions = read_lines(cls.path / "game-000" / "focal-decisions.jsonl")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _rejects(self, tampered, pattern):
        with self.assertRaisesRegex(FocalOutcomeSourceError, pattern):
            engine_source.verify_engine_focal_outcome_source(tampered.path)

    def test_current_engine_positions_are_rejected_without_a_partial_source(self):
        self.assertTrue(
            any(
                p.riichi_declaration_discard_order is not None
                for capture in self.current_execution.captures
                for p in capture.decision.input.players
            )
        )
        output = self.root / "current-source"
        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "riichi_declaration_discard_order"
        ):
            engine_source.generate_engine_focal_outcome_source(
                output,
                population_role="DIAGNOSTIC",
                games=[(SMOKE_SEED, "DIAGNOSTIC")],
                allocation_bindings={},
                source_contract=source_contract(),
                run_game=lambda **kwargs: self.current_execution,
            )
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(f".{output.name}.partial").exists())

    def test_historical_engine_source_passes_strict_readback(self):
        (game,) = self.source.games
        self.assertEqual(
            (game.seed, game.split, int(game.focal_seat)), (SMOKE_SEED, "DIAGNOSTIC", 0)
        )
        self.assertEqual(game.kyoku_count, len(self.execution.match.history))
        self.assertEqual(game.decision_count, len(self.execution.captures))
        self.assertGreater(game.decision_count, 0)

    def test_kyoku_rows_are_the_engine_settlement_boundary(self):
        for row, completed in zip(
            self.kyokus, self.execution.match.history, strict=True
        ):
            after = completed.scores_after_settlement
            deltas = completed.settlement.point_deltas
            self.assertEqual(row["points_after_kyoku"], [after[seat] for seat in ES])
            self.assertEqual(row["point_deltas"], [deltas[seat] for seat in ES])
            self.assertEqual(
                row["riichi_sticks_before"], completed.position_before.riichi_sticks
            )
            self.assertEqual(
                row["riichi_sticks_after"], completed.settlement.riichi_sticks_after
            )
            self.assertEqual(row["honba"], completed.position_before.honba)

    def test_focal_decisions_keep_the_exploration_contract(self):
        captures = self.execution.captures
        self.assertEqual(len(self.decisions), len(captures))
        for row, capture in zip(self.decisions, captures, strict=True):
            ordinal = row["focal_decision_ordinal"]
            self.assertEqual(ordinal, capture.focal_decision_ordinal)
            self.assertEqual(
                row["exploration_token"],
                exploration_token(
                    game_seed=SMOKE_SEED, focal_seat=0, focal_decision_ordinal=ordinal
                ),
            )
            self.assertEqual(row["exploration_token"], capture.exploration_token)
            self.assertEqual(
                row["selected_action"],
                _action_to_value(capture.selection.action, "selected"),
            )
            survivors = capture.selection.survivor_actions
            self.assertEqual(
                row["producer_survivor_actions"],
                None
                if survivors is None
                else [_action_to_value(a, "survivor") for a in survivors],
            )
        self.assertEqual(
            [row["focal_decision_ordinal"] for row in self.decisions],
            list(range(len(self.decisions))),
        )
        kyoku_ordinals = [row["kyoku_ordinal"] for row in self.decisions]
        self.assertEqual(kyoku_ordinals, sorted(kyoku_ordinals))
        self.assertTrue(any(row["producer_survivor_actions"] for row in self.decisions))
        self.assertTrue(
            any(row["producer_survivor_actions"] is None for row in self.decisions)
        )

    def test_exploration_token_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            decisions[0]["exploration_token"] = "0" * 64
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "token")

    def test_selected_action_tamper_fails_closed(self):
        index = next(
            i
            for i, row in enumerate(self.decisions)
            if row["producer_survivor_actions"]
            and len(row["producer_survivor_actions"]) >= 2
        )
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            row = decisions[index]
            row["selected_action"] = next(
                survivor
                for survivor in row["producer_survivor_actions"]
                if survivor != row["selected_action"]
            )
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "selected_action")

    def test_survivor_list_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            index = next(
                i for i, row in enumerate(decisions) if row["producer_survivor_actions"]
            )
            decisions[index]["producer_survivor_actions"] = None
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "survivors")

    def test_kyoku_binding_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            decisions[0]["kyoku_ordinal"] = 1
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "round identity")

    def test_decision_order_and_count_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            tampered.rewrite_game(0, decisions=decisions[:-1])
            self._rejects(tampered, "decision count")
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            decisions[0], decisions[1] = decisions[1], decisions[0]
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "ordinal")

    def test_execution_order_field_is_not_carried_over(self):
        self.assertNotIn("step_ordinal", self.decisions[0])
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Tampered(self.path, tmp)
            decisions = tampered.decisions()
            decisions[0]["step_ordinal"] = 0
            tampered.rewrite_game(0, decisions=decisions)
            self._rejects(tampered, "fields")


if __name__ == "__main__":
    unittest.main()
