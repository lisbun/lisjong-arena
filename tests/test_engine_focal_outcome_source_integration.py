"""Integration tests for the #370 lisjong-engine focal outcome source.

test専用seedの実lisjong-engine対局1 hanchanだけからsourceを生成し、Arenaの
strict readbackを通す。focal decisionの改ざんcaseは生成済みsourceのcopyで検証する。
"""

import tempfile
import unittest
from pathlib import Path

from _engine_focal_outcome_source_fixtures import Tampered, read_lines, source_contract
from lisjong_engine.seat import Seat as ES

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
        executions = []

        def run_game(**kwargs):
            executions.append(engine_source.run_engine_focal_game(**kwargs))
            return executions[-1]

        cls.source = engine_source.generate_engine_focal_outcome_source(
            cls.path,
            population_role="DIAGNOSTIC",
            games=[(SMOKE_SEED, "DIAGNOSTIC")],
            allocation_bindings={},
            source_contract=source_contract(),
            run_game=run_game,
        )
        (cls.execution,) = executions
        cls.kyokus = read_lines(cls.path / "game-000" / "kyokus.jsonl")
        cls.decisions = read_lines(cls.path / "game-000" / "focal-decisions.jsonl")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _rejects(self, tampered, pattern):
        with self.assertRaisesRegex(FocalOutcomeSourceError, pattern):
            engine_source.verify_engine_focal_outcome_source(tampered.path)

    def test_real_engine_source_passes_strict_readback(self):
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
